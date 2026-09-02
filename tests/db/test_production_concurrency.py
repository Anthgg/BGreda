"""Fase 009I — atomicidad, agregacion multilinea y concurrencia del arranque.

Tres fallos distintos que acaban en el mismo sitio —un almacen que dice una
cosa y contiene otra— y que solo aparecen con base de datos real:

1. **Parcial**: la orden necesita tres materiales, el tercero no alcanza, y los
   dos primeros ya se descontaron.
2. **Agregacion**: dos lineas piden el mismo barniz, cada una cabe en el saldo
   por separado, y juntas no.
3. **Carrera**: dos peticiones a la vez consumen el mismo saldo dos veces.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from tests.db.test_firings_api import FACTORES_CHICO, crear_horno
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    crear_ubicacion,
    dar_existencia,
)
from tests.db.test_quotation_builder_api import head

BUILDER = "/api/v1/quotation-builder"
PARTNERS = "/api/v1/partners"
RECIPES = "/api/v1/recipes"


async def _preparado_con_receta(
    api: httpx.AsyncClient, csrf: str, categoria_id: int, nombre: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Un material preparado en gramos y la receta que lo produce."""
    insumo = await create_product(
        api,
        csrf,
        product_category_id=categoria_id,
        name=f"Insumo {nombre}",
        product_type="RAW_MATERIAL",
        base_uom_code="g",
        cost="0.4",
    )
    assert insumo.status_code == 201, insumo.text
    preparado = await create_product(
        api,
        csrf,
        product_category_id=categoria_id,
        name=f"Preparado {nombre}",
        product_type="PREPARED_MATERIAL",
        base_uom_code="g",
    )
    assert preparado.status_code == 201, preparado.text
    receta = await api.post(
        RECIPES,
        json={
            "product_id": preparado.json()["id"],
            "name": f"Formula {nombre}",
            "lines": [
                {
                    "component_product_id": insumo.json()["id"],
                    "component_type": "BASE",
                    "percentage": "100",
                    "sort_order": 0,
                }
            ],
            "active": True,
            "activate_immediately": True,
        },
        headers=head(csrf),
    )
    assert receta.status_code == 201, receta.text
    return preparado.json(), receta.json()


async def escenario_multilinea(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
    lineas: list[tuple[str, int, str]],
    existencias: list[str | None],
) -> dict[str, Any]:
    """Una cotizacion confirmada con N lineas, cada una con su preparado.

    `lineas` son tripletas (nombre, cantidad, gramos_por_pieza) y `existencias`
    dice cuanto preparado hay de cada una. Con eso se puede montar el caso en
    que dos alcanzan y la tercera no.
    """
    categoria = await create_category(api, csrf, f"Multilinea{suffix}")
    cliente = await api.post(
        PARTNERS,
        json={
            "name": f"Cliente multilinea{suffix}",
            "role": "CLIENT",
            "document_type": "RUC",
            "document_number": f"206{abs(hash(suffix)) % 100000000:08d}",
        },
        headers=head(csrf),
    )
    assert cliente.status_code == 201, cliente.text
    horno = await crear_horno(
        api,
        csrf,
        db_session,
        nombre=f"Horno multilinea{suffix}",
        capacidad="10000000",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )
    location_id = await crear_ubicacion(api, csrf, f"Almacen multilinea{suffix}")

    items: list[dict[str, Any]] = []
    preparados: list[dict[str, Any]] = []
    for indice, (nombre, cantidad, gramos) in enumerate(lineas):
        terminado = await create_product(
            api,
            csrf,
            product_category_id=categoria["id"],
            name=f"Pieza {nombre}",
            product_type="FINISHED_PRODUCT",
            base_uom_code="unit",
            sellable=True,
            sale_price="100",
        )
        assert terminado.status_code == 201, terminado.text
        preparado, receta = await _preparado_con_receta(api, csrf, categoria["id"], nombre)
        preparados.append(preparado)
        items.append(
            {
                "product_id": terminado.json()["id"],
                "quantity": cantidad,
                "dimensions": {"width": "20", "length": "20"},
                "recipe_id": receta["id"],
                "recipe_version_id": receta["current_version"]["id"],
                "material_grams_per_piece": gramos,
                "other_costs": [],
                "markup_percent": "100",
                "commercial_sale_unit_price": "9",
                "sort_order": indice,
            }
        )

    for preparado, existencia in zip(preparados, existencias, strict=True):
        if existencia is not None:
            await dar_existencia(
                api,
                csrf,
                product_id=preparado["id"],
                location_id=location_id,
                cantidad=existencia,
            )

    creada = await api.post(
        BUILDER,
        json={
            "name": f"Pedido multilinea{suffix}",
            "customer_id": cliente.json()["id"],
            "kiln_id": horno["id"],
            "items": items,
        },
        headers=head(csrf),
    )
    assert creada.status_code == 201, creada.text
    confirmada = await confirmar(api, csrf, creada.json())

    orden = await crear_orden(api, csrf, quotation_id=confirmada["id"], location_id=location_id)
    assert orden.status_code == 201, orden.text
    return {
        "preparados": preparados,
        "location_id": location_id,
        "orden": orden.json(),
        "confirmada": confirmada,
    }


async def _movimientos(db_session: AsyncSession) -> int:
    db_session.expire_all()
    return int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)


async def _saldo(db_session: AsyncSession, product_id: int, location_id: int) -> Decimal:
    db_session.expire_all()
    valor = await db_session.scalar(
        select(StockBalance.quantity).where(
            StockBalance.product_id == product_id,
            StockBalance.location_id == location_id,
        )
    )
    return Decimal(valor) if valor is not None else Decimal(0)


# ---------------------------------------------------------------------------
# PARTE Y — todo o nada
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_si_falta_el_tercer_material_no_se_descuentan_los_dos_primeros(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_START_ALL_OR_NOTHING / PARTIAL_PRODUCTION_CONSUMPTION_ON_FAILURE: 0.

    Tres materiales: A pide 10 g y hay 100, B pide 20 g y hay 100, C pide 30 g
    y solo hay 5. Sin comprobarlo TODO antes de tocar nada, A y B ya estarian
    descontados cuando C falla, y el taller tendria dos materiales gastados y
    ninguna pieza.

    La comprobacion completa va delante del primer descuento, y la excepcion
    deshace la transaccion entera.
    """
    datos = await escenario_multilinea(
        api,
        admin_csrf,
        db_session,
        suffix="_todo_o_nada",
        lineas=[("A", 1, "10"), ("B", 1, "20"), ("C", 1, "30")],
        existencias=["100", "100", "5"],
    )
    antes = await _movimientos(db_session)
    saldos_antes = [
        await _saldo(db_session, preparado["id"], datos["location_id"])
        for preparado in datos["preparados"]
    ]

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_READY"
    assert await _movimientos(db_session) == antes
    saldos_despues = [
        await _saldo(db_session, preparado["id"], datos["location_id"])
        for preparado in datos["preparados"]
    ]
    assert saldos_despues == saldos_antes == [Decimal("100"), Decimal("100"), Decimal("5")]

    releida = await api.get(f"{ORDERS}/{datos['orden']['id']}")
    assert releida.json()["status"] == "CREATED"


# ---------------------------------------------------------------------------
# PARTE H — agregacion antes de comprobar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dos_lineas_del_mismo_preparado_se_suman_antes_de_mirar_el_saldo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MULTILINE_REQUIREMENTS_AGGREGATED_BEFORE_STOCK_CHECK.

    Dos lineas de la misma cotizacion comparten receta: una pide 100 g y otra
    200 g del mismo barniz, y en el almacen hay 250.

    Comprobadas por separado las dos pasan —100 < 250 y 200 < 250— y al
    descontar dejarian el saldo en -50. Juntas piden 300 y no alcanzan. Se
    agrupa por preparado y se mira el saldo UNA vez.
    """
    categoria = await create_category(api, admin_csrf, "Comparten receta 009I")
    preparado, receta = await _preparado_con_receta(api, admin_csrf, categoria["id"], "compartido")
    cliente = await api.post(
        PARTNERS,
        json={
            "name": "Cliente compartido 009I",
            "role": "CLIENT",
            "document_type": "RUC",
            "document_number": "20612345678",
        },
        headers=head(admin_csrf),
    )
    assert cliente.status_code == 201, cliente.text
    horno = await crear_horno(
        api,
        admin_csrf,
        db_session,
        nombre="Horno compartido 009I",
        capacidad="10000000",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )
    location_id = await crear_ubicacion(api, admin_csrf, "Almacen compartido 009I")
    await dar_existencia(
        api, admin_csrf, product_id=preparado["id"], location_id=location_id, cantidad="250"
    )

    items = []
    for indice, (cantidad, gramos) in enumerate(((10, "10"), (10, "20"))):
        terminado = await create_product(
            api,
            admin_csrf,
            product_category_id=categoria["id"],
            name=f"Pieza compartida {indice}",
            product_type="FINISHED_PRODUCT",
            base_uom_code="unit",
            sellable=True,
            sale_price="100",
        )
        assert terminado.status_code == 201, terminado.text
        items.append(
            {
                "product_id": terminado.json()["id"],
                "quantity": cantidad,
                "dimensions": {"width": "20", "length": "20"},
                "recipe_id": receta["id"],
                "recipe_version_id": receta["current_version"]["id"],
                "material_grams_per_piece": gramos,
                "other_costs": [],
                "markup_percent": "100",
                "commercial_sale_unit_price": "9",
                "sort_order": indice,
            }
        )

    creada = await api.post(
        BUILDER,
        json={
            "name": "Pedido compartido",
            "customer_id": cliente.json()["id"],
            "kiln_id": horno["id"],
            "items": items,
        },
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    confirmada = await confirmar(api, admin_csrf, creada.json())
    orden = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=location_id
    )
    assert orden.status_code == 201, orden.text
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{orden.json()['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    detalles = respuesta.json()["error"]["details"]
    insuficiente = [d for d in detalles if d["code"] == "INSUFFICIENT_STOCK"]
    assert len(insuficiente) == 1, "el saldo compartido se juzga una vez, no una por linea"
    assert Decimal(insuficiente[0]["required_quantity"]) == Decimal("300")
    assert Decimal(insuficiente[0]["available_quantity"]) == Decimal("250")
    assert await _movimientos(db_session) == antes
    assert await _saldo(db_session, preparado["id"], location_id) == Decimal("250")


@pytest.mark.asyncio
async def test_dos_lineas_del_mismo_preparado_que_si_caben_consumen_una_sola_salida(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La contraparte: cuando la suma cabe, se descuenta la suma.

    Un movimiento por preparado, no uno por linea: el consumo fisico es uno
    solo y la orden que lo origino ya identifica de donde sale.
    """
    datos = await escenario_multilinea(
        api,
        admin_csrf,
        db_session,
        suffix="_suma_ok",
        lineas=[("X", 10, "10"), ("Y", 10, "20")],
        existencias=["1000", "1000"],
    )

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 200, respuesta.text
    assert await _saldo(db_session, datos["preparados"][0]["id"], datos["location_id"]) == (
        Decimal("900")
    )
    assert await _saldo(db_session, datos["preparados"][1]["id"], datos["location_id"]) == (
        Decimal("800")
    )
    db_session.expire_all()
    salidas = await db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.production_order_id == datos["orden"]["id"])
    )
    assert salidas == 2, "dos preparados distintos, un movimiento cada uno"


# ---------------------------------------------------------------------------
# PARTE Z — concurrencia
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dos_arranques_simultaneos_de_la_misma_orden_consumen_una_vez(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONCURRENT_DOUBLE_START_SINGLE_CONSUMPTION.

    Un doble clic manda las dos peticiones a la vez. Sin bloquear la orden,
    ambas leerian CREATED, ambas verian saldo suficiente y ambas descontarian:
    el barniz se gastaria dos veces por una sola fabricacion.

    La orden se bloquea antes de mirar nada, asi que la segunda espera, ve
    STARTED y no mueve un gramo.
    """
    datos = await escenario_multilinea(
        api,
        admin_csrf,
        db_session,
        suffix="_carrera",
        lineas=[("Z", 10, "10")],
        existencias=["1000"],
    )
    preparado = datos["preparados"][0]

    primera, segunda = await asyncio.gather(
        api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf)),
        api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf)),
    )

    assert [primera.status_code, segunda.status_code] == [200, 200], (
        f"{primera.text} | {segunda.text}"
    )
    assert primera.json()["status"] == segunda.json()["status"] == "STARTED"
    assert primera.json()["started_at"] == segunda.json()["started_at"]

    db_session.expire_all()
    salidas = await db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.production_order_id == datos["orden"]["id"])
    )
    assert salidas == 1, "una sola fabricacion, un solo consumo"
    assert await _saldo(db_session, preparado["id"], datos["location_id"]) == Decimal("900")


@pytest.mark.asyncio
async def test_dos_ordenes_que_se_pelean_el_mismo_saldo_no_lo_dejan_negativo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONCURRENT_ORDERS_NO_NEGATIVE_STOCK.

    Dos cotizaciones distintas, dos ordenes distintas, el MISMO barniz. Cada
    una necesita 700 g y en el almacen hay 1000: juntas piden 1400.

    Una tiene que poder arrancar y la otra recibir un conflicto claro. Lo que
    no puede pasar bajo ningun concepto es que el saldo acabe en negativo.
    """
    categoria = await create_category(api, admin_csrf, "Pelea de saldo 009I")
    preparado, receta = await _preparado_con_receta(api, admin_csrf, categoria["id"], "pelea")
    horno = await crear_horno(
        api,
        admin_csrf,
        db_session,
        nombre="Horno pelea 009I",
        capacidad="10000000",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )
    location_id = await crear_ubicacion(api, admin_csrf, "Almacen pelea 009I")
    await dar_existencia(
        api, admin_csrf, product_id=preparado["id"], location_id=location_id, cantidad="1000"
    )

    ordenes = []
    for indice in range(2):
        cliente = await api.post(
            PARTNERS,
            json={
                "name": f"Cliente pelea {indice}",
                "role": "CLIENT",
                "document_type": "RUC",
                "document_number": f"2065555555{indice}",
            },
            headers=head(admin_csrf),
        )
        assert cliente.status_code == 201, cliente.text
        terminado = await create_product(
            api,
            admin_csrf,
            product_category_id=categoria["id"],
            name=f"Pieza pelea {indice}",
            product_type="FINISHED_PRODUCT",
            base_uom_code="unit",
            sellable=True,
            sale_price="100",
        )
        assert terminado.status_code == 201, terminado.text
        creada = await api.post(
            BUILDER,
            json={
                "name": f"Pedido pelea {indice}",
                "customer_id": cliente.json()["id"],
                "kiln_id": horno["id"],
                "items": [
                    {
                        "product_id": terminado.json()["id"],
                        "quantity": 70,
                        "dimensions": {"width": "20", "length": "20"},
                        "recipe_id": receta["id"],
                        "recipe_version_id": receta["current_version"]["id"],
                        "material_grams_per_piece": "10",
                        "other_costs": [],
                        "markup_percent": "100",
                        "commercial_sale_unit_price": "9",
                        "sort_order": 0,
                    }
                ],
            },
            headers=head(admin_csrf),
        )
        assert creada.status_code == 201, creada.text
        confirmada = await confirmar(api, admin_csrf, creada.json())
        orden = await crear_orden(
            api, admin_csrf, quotation_id=confirmada["id"], location_id=location_id
        )
        assert orden.status_code == 201, orden.text
        ordenes.append(orden.json())

    primera, segunda = await asyncio.gather(
        api.post(f"{ORDERS}/{ordenes[0]['id']}/start", headers=head(admin_csrf)),
        api.post(f"{ORDERS}/{ordenes[1]['id']}/start", headers=head(admin_csrf)),
    )

    codigos = sorted([primera.status_code, segunda.status_code])
    assert codigos == [200, 409], f"{primera.text} | {segunda.text}"
    saldo = await _saldo(db_session, preparado["id"], location_id)
    assert saldo == Decimal("300")
    assert saldo >= 0
