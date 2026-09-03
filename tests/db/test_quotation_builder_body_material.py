"""Material base de la pieza: que material forma el cuerpo, y cuanto lleva.

El Cotizador pedia «receta + gramos». Aqui pasa a pedir MATERIAL + CANTIDAD, que
es lo que el taller decide de verdad, y la receta queda donde le corresponde:
como procedencia del preparado, no como algo que haya que elegir para cotizar.

Todo vive en `QuotationItem.production_snapshot["body_material"]`. No hay tabla
nueva ni migracion, igual que con `glaze_plan` en 009D.

Las dos cosas que mas se comprueban aqui son las que mas caro costarian si
fallaran:

1. Que el camino LEGACY no se ha movido un centimo. Hay cotizaciones emitidas
   con receta y gramos, y esta fase no puede cambiarles el importe.
2. Que el cuerpo y el esmalte se calculan por separado. Un esmalte no sustituye
   ni multiplica el material del cuerpo: se suma como lo que es, un adicional.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from app.models.masters import Product
from app.models.production import ProductionOrderLine
from app.models.quotations import Quotation, QuotationItem
from tests.db.test_firings_api import FACTORES_CHICO, crear_horno
from tests.db.test_glaze_estimate_api import _lote
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmada_y_pagada,
    confirmar,
    crear_orden,
    crear_ubicacion,
    dar_existencia,
)
from tests.db.test_quotation_builder_api import BUILDER, head
from tests.db.test_quotations_api import _finished_product_and_recipe

#: Peso de pieza del maestro. Con el 15 % por omision da 75 g de esmalte por
#: pieza, un numero que no se parece a ninguna cantidad de cuerpo de estas
#: pruebas: si algun dia el esmalte se colara en el cuerpo, se veria.
PIECE_WEIGHT_G = Decimal(500)

#: Lo que cuesta un gramo tanto de la materia prima como del preparado que la
#: lleva al 100 %. Que coincidan es lo que permite comparar los dos caminos.
COST_PER_GRAM = Decimal("0.5")


async def _materials(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
) -> dict[str, Any]:
    """Escenario minimo: una pieza, una materia prima, un preparado y un horno.

    Se apoya en `_finished_product_and_recipe`, que ya deja exactamente lo que
    hace falta: la pieza terminada, una `Pasta local` (RAW_MATERIAL, g, 0,5 por
    gramo) y un `Barniz local` (PREPARED_MATERIAL, g, SIN costo propio) con su
    receta al 100 % de la pasta. Que el preparado no tenga costo es
    deliberado: obliga a costearlo por su receta, que es el camino que hay que
    probar.
    """
    producto, receta = await _finished_product_and_recipe(api, csrf, f"_bm{suffix}")
    prepared_id = int(receta["product_id"])

    raw_id = await db_session.scalar(
        select(Product.id).where(Product.name == f"Pasta local QA_bm{suffix}")
    )
    assert raw_id is not None

    await db_session.execute(
        update(Product).where(Product.id == producto["id"]).values(grammage=PIECE_WEIGHT_G)
    )
    await db_session.commit()

    horno = await crear_horno(
        api,
        csrf,
        db_session,
        nombre=f"Horno material base{suffix}",
        capacidad="10000000",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )
    return {
        "producto": producto,
        "receta": receta,
        "prepared_id": prepared_id,
        "raw_id": int(raw_id),
        "kiln_id": horno["id"],
    }


def _payload(
    escena: dict[str, Any],
    *,
    quantity: int,
    body_material: dict[str, Any] | None = None,
    legacy: bool = False,
    glazes: list[dict[str, Any]] | None = None,
    suffix: str = "",
) -> dict[str, Any]:
    """Cotizacion de una linea, por el camino nuevo o por el legacy."""
    item: dict[str, Any] = {
        "product_id": escena["producto"]["id"],
        "quantity": quantity,
        "dimensions": {"width": "24", "length": "24", "height": "8"},
        "other_costs": [],
        "markup_percent": "100",
        "commercial_sale_unit_price": "8.50",
        "sort_order": 0,
    }
    if body_material is not None:
        item["body_material"] = body_material
    if legacy:
        item["recipe_id"] = escena["receta"]["id"]
        item["recipe_version_id"] = escena["receta"]["current_version"]["id"]
        item["material_grams_per_piece"] = "10"
    if glazes is not None:
        item["glazes"] = glazes
        item["glaze_selection_touched"] = True
    return {
        "name": f"Pedido material base{suffix}",
        "kiln_id": escena["kiln_id"],
        "items": [item],
    }


async def _preview(api: httpx.AsyncClient, csrf: str, payload: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(f"{BUILDER}/preview", json=payload, headers=head(csrf))
    assert respuesta.status_code == 200, respuesta.text
    return dict(respuesta.json())


async def _crear(api: httpx.AsyncClient, csrf: str, payload: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(BUILDER, json=payload, headers=head(csrf))
    assert respuesta.status_code == 201, respuesta.text
    return dict(respuesta.json())


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
# 1 y 2 — los dos tipos de material que pueden ser el cuerpo de una pieza
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_material_preparado_en_gramos_se_costea_por_su_receta(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PREPARED_BODY_MATERIAL_SUPPORTED + BODY_MATERIAL_QUANTITY_PER_PIECE.

    300 g/pieza x 10 piezas = 3000 g a 0,5 el gramo = 1500. El preparado no
    tiene costo propio, asi que ese 0,5 sale de su receta: es el mismo
    resolvedor que ya usaban los componentes, no un segundo motor.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_prep")
    preview = await _preview(
        api,
        admin_csrf,
        _payload(
            escena,
            quantity=10,
            body_material={"product_id": escena["prepared_id"], "quantity_per_piece": "300"},
        ),
    )

    linea = preview["items"][0]
    material = linea["body_material"]
    assert material["product_id"] == escena["prepared_id"]
    assert material["source"] == "PREPARED"
    assert material["uom"] == "g"
    assert Decimal(material["quantity_per_piece"]) == Decimal(300)
    assert Decimal(material["required_quantity"]) == Decimal(3000)
    assert Decimal(material["unit_cost_snapshot"]) == COST_PER_GRAM
    assert Decimal(linea["materials_calculated"]) == Decimal(1500)
    # La receta viaja como PROCEDENCIA, no como eleccion.
    assert material["recipe_id_used"] == escena["receta"]["id"]
    assert material["recipe_name_snapshot"] == escena["receta"]["name"]
    # Y ya no falta nada que impida confirmar.
    assert "RECIPE_REQUIRED" not in linea["warnings"]
    assert "MATERIAL_GRAMS_PER_PIECE_REQUIRED" not in linea["warnings"]
    assert linea["complete"] is True


@pytest.mark.asyncio
async def test_materia_prima_directa_no_necesita_receta_ninguna(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """RAW_BODY_MATERIAL_SUPPORTED.

    Es el caso que el modelo anterior no podia representar: una pieza hecha
    directamente con pasta, sin preparado de por medio. 500 g/pieza x 2 = 1000 g
    a 0,5 = 500, y ni una receta anotada, porque no existe.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_raw")
    preview = await _preview(
        api,
        admin_csrf,
        _payload(
            escena,
            quantity=2,
            body_material={"product_id": escena["raw_id"], "quantity_per_piece": "500"},
        ),
    )

    linea = preview["items"][0]
    material = linea["body_material"]
    assert material["source"] == "RAW"
    assert material["recipe_id_used"] is None
    assert Decimal(material["required_quantity"]) == Decimal(1000)
    assert Decimal(linea["materials_calculated"]) == Decimal(500)
    assert "RECIPE_REQUIRED" not in linea["warnings"]
    assert linea["complete"] is True


# ---------------------------------------------------------------------------
# 3 y 4 — el esmalte es un adicional, no el cuerpo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_pieza_sin_esmalte_es_perfectamente_cotizable(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCT_WITHOUT_GLAZE_VALID."""
    escena = await _materials(api, admin_csrf, db_session, suffix="_noglaze")
    preview = await _preview(
        api,
        admin_csrf,
        _payload(
            escena,
            quantity=10,
            body_material={"product_id": escena["prepared_id"], "quantity_per_piece": "100"},
            glazes=[],
        ),
    )

    linea = preview["items"][0]
    assert linea["glaze_plan"] is None
    assert Decimal(linea["body_material"]["required_quantity"]) == Decimal(1000)
    assert linea["complete"] is True


@pytest.mark.asyncio
async def test_anadir_esmalte_no_toca_el_material_del_cuerpo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """GLAZE_DOES_NOT_REPLACE_BODY_MATERIAL + GLAZE_DOES_NOT_MULTIPLY_BODY_MATERIAL.

    La misma linea, con y sin esmalte. El cuerpo tiene que salir identico en
    las dos: cantidad, costo unitario y costo total. El esmalte aparece aparte,
    en su propio plan, y con su propia cifra en gramos.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_glaze")
    lote = await _lote(api, admin_csrf, suffix="_bm_glaze", final_yield_ml="5000")

    base = _payload(
        escena,
        quantity=10,
        body_material={"product_id": escena["prepared_id"], "quantity_per_piece": "300"},
        glazes=[],
    )
    sin_esmalte = (await _preview(api, admin_csrf, base))["items"][0]

    con = _payload(
        escena,
        quantity=10,
        body_material={"product_id": escena["prepared_id"], "quantity_per_piece": "300"},
        glazes=[{"preparation_id": lote["id"], "share": "1"}],
    )
    con_esmalte = (await _preview(api, admin_csrf, con))["items"][0]

    assert con_esmalte["body_material"] == sin_esmalte["body_material"]
    assert Decimal(con_esmalte["materials_calculated"]) == Decimal(
        sin_esmalte["materials_calculated"]
    )
    # El esmalte existe y es OTRA cifra: 500 g de pieza x 10 x 15 % = 750 g.
    plan = con_esmalte["glaze_plan"]
    assert plan is not None
    assert Decimal(plan["total_estimated_solids_g"]) == Decimal(750)
    assert Decimal(con_esmalte["body_material"]["required_quantity"]) == Decimal(3000)


# ---------------------------------------------------------------------------
# 5 y 6 — guardar, reabrir y confirmar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_material_base_sobrevive_guardar_y_reabrir(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BODY_MATERIAL_SAVE_REOPEN."""
    escena = await _materials(api, admin_csrf, db_session, suffix="_reopen")
    creada = await _crear(
        api,
        admin_csrf,
        _payload(
            escena,
            quantity=7,
            body_material={"product_id": escena["prepared_id"], "quantity_per_piece": "250.5"},
        ),
    )

    reabierta = await api.get(f"{BUILDER}/{creada['id']}", headers=head(admin_csrf))
    assert reabierta.status_code == 200, reabierta.text
    material = reabierta.json()["items"][0]["body_material"]
    assert material["product_id"] == escena["prepared_id"]
    assert Decimal(material["quantity_per_piece"]) == Decimal("250.5")
    assert material["uom"] == "g"
    assert Decimal(material["required_quantity"]) == Decimal("1753.5")


@pytest.mark.asyncio
async def test_confirmada_conserva_el_material_aunque_cambie_el_maestro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_BODY_MATERIAL_IMMUTABLE.

    Se confirma, y DESPUES se le cambia el costo al material y se le pone otro
    nombre. La confirmada tiene que seguir contando la historia de entonces:
    un documento ya entregado no puede cambiar de importe porque alguien
    edite un maestro.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_frozen")
    creada = await _crear(
        api,
        admin_csrf,
        _payload(
            escena,
            quantity=10,
            body_material={"product_id": escena["prepared_id"], "quantity_per_piece": "300"},
        ),
    )
    confirmada = await confirmar(api, admin_csrf, creada)
    antes = confirmada["items"][0]["body_material"]
    assert Decimal(antes["unit_cost_snapshot"]) == COST_PER_GRAM

    await db_session.execute(
        update(Product)
        .where(Product.id == escena["prepared_id"])
        .values(cost=Decimal("99"), name="Barniz renombrado despues")
    )
    await db_session.commit()

    despues = await api.get(f"{BUILDER}/{confirmada['id']}", headers=head(admin_csrf))
    assert despues.status_code == 200, despues.text
    material = despues.json()["items"][0]["body_material"]
    assert Decimal(material["unit_cost_snapshot"]) == COST_PER_GRAM
    assert Decimal(material["material_cost"]) == Decimal(1500)
    assert material["product_name"] != "Barniz renombrado despues"


# ---------------------------------------------------------------------------
# 7, 8 y 9 — produccion
# ---------------------------------------------------------------------------
async def _orden_lista(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
    product_id_key: str = "prepared_id",
    quantity_per_piece: str = "300",
    quantity: int = 10,
    existencia: str = "5000",
) -> dict[str, Any]:
    """Cotizacion con material base confirmada, cobrada y con la orden creada."""
    escena = await _materials(api, csrf, db_session, suffix=suffix)
    material_id = escena[product_id_key]
    creada = await _crear(
        api,
        csrf,
        _payload(
            escena,
            quantity=quantity,
            body_material={"product_id": material_id, "quantity_per_piece": quantity_per_piece},
        ),
    )
    confirmada = await confirmada_y_pagada(api, csrf, creada)
    location_id = await crear_ubicacion(api, csrf, f"Almacen material base{suffix}")
    await dar_existencia(
        api, csrf, product_id=material_id, location_id=location_id, cantidad=existencia
    )
    orden = await crear_orden(api, csrf, quotation_id=confirmada["id"], location_id=location_id)
    assert orden.status_code == 201, orden.text
    return {
        **escena,
        "material_id": material_id,
        "confirmada": confirmada,
        "location_id": location_id,
        "orden": orden.json(),
    }


@pytest.mark.asyncio
async def test_la_orden_deriva_el_material_del_snapshot_confirmado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """FINAL_OP_MATERIAL_BACKEND_DERIVED.

    Nada de esto lo elige quien crea la orden: el material, la cantidad y la
    unidad ya venian congelados de la cotizacion.
    """
    datos = await _orden_lista(api, admin_csrf, db_session, suffix="_op")
    linea = (
        await db_session.execute(
            select(ProductionOrderLine).where(
                ProductionOrderLine.production_order_id == datos["orden"]["id"]
            )
        )
    ).scalar_one()

    assert linea.prepared_product_id == datos["material_id"]
    assert linea.required_material_quantity == Decimal(3000)
    assert linea.required_material_uom == "g"


@pytest.mark.asyncio
async def test_arrancar_descuenta_el_material_base_y_solo_ese(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BODY_MATERIAL_INVENTORY_CONSUMPTION + PREPARED_COMPONENT_DOUBLE_CONSUMPTION = 0.

    Se descuentan 3000 g del PREPARADO y **nada** de la pasta que lo compone:
    esa ya se gasto cuando se preparo el barniz. Volver a descontarla aqui
    seria cobrarle al almacen dos veces el mismo material.
    """
    datos = await _orden_lista(api, admin_csrf, db_session, suffix="_start")
    movimientos_antes = await _movimientos(db_session)
    pasta_antes = await _saldo(db_session, datos["raw_id"], datos["location_id"])

    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    assert arranque.status_code == 200, arranque.text

    assert await _saldo(db_session, datos["material_id"], datos["location_id"]) == Decimal(2000)
    assert await _saldo(db_session, datos["raw_id"], datos["location_id"]) == pasta_antes
    assert await _movimientos(db_session) == movimientos_antes + 1


@pytest.mark.asyncio
async def test_una_pieza_de_materia_prima_consume_esa_materia_prima(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El camino RAW llega hasta el almacen, no solo hasta el precio."""
    datos = await _orden_lista(
        api,
        admin_csrf,
        db_session,
        suffix="_startraw",
        product_id_key="raw_id",
        quantity_per_piece="500",
        quantity=2,
    )
    arranque = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    assert arranque.status_code == 200, arranque.text
    assert await _saldo(db_session, datos["raw_id"], datos["location_id"]) == Decimal(4000)


# ---------------------------------------------------------------------------
# 10 y 12 — el pasado no se toca
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_linea_legacy_sigue_costando_exactamente_lo_mismo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LEGACY_RECIPE_QUOTATION_AMOUNT_UNCHANGED.

    Receta + 10 g/pieza x 100 piezas = 1000 g de base a 0,5 = 500. Es el
    numero que daba antes de esta fase y el que tiene que seguir dando: hay
    cotizaciones emitidas con el.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_legacy")
    preview = await _preview(api, admin_csrf, _payload(escena, quantity=100, legacy=True))

    linea = preview["items"][0]
    assert Decimal(linea["materials_calculated"]) == Decimal(500)
    assert linea["body_material"] is None
    assert linea["recipe_id"] == escena["receta"]["id"]
    assert Decimal(linea["material_grams_per_piece"]) == Decimal(10)


@pytest.mark.asyncio
async def test_una_cotizacion_sin_material_base_se_lee_por_el_camino_de_siempre(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """HISTORICAL_QUOTATION_READS_LEGACY.

    Una linea confirmada sin `body_material` no se rellena con el maestro de
    hoy: se queda sin el, y su orden de produccion resuelve el preparado por
    la receta, como se ha hecho siempre.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_hist")
    creada = await _crear(api, admin_csrf, _payload(escena, quantity=100, legacy=True))
    confirmada = await confirmada_y_pagada(api, admin_csrf, creada)

    item = (
        await db_session.execute(
            select(QuotationItem)
            .join(Quotation, Quotation.id == QuotationItem.quotation_id)
            .where(Quotation.id == confirmada["id"])
        )
    ).scalar_one()
    assert "body_material" not in item.production_snapshot

    location_id = await crear_ubicacion(api, admin_csrf, "Almacen historico")
    await dar_existencia(
        api,
        admin_csrf,
        product_id=escena["prepared_id"],
        location_id=location_id,
        cantidad="5000",
    )
    orden = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=location_id
    )
    assert orden.status_code == 201, orden.text

    linea = (
        await db_session.execute(
            select(ProductionOrderLine).where(
                ProductionOrderLine.production_order_id == orden.json()["id"]
            )
        )
    ).scalar_one()
    assert linea.prepared_product_id == escena["prepared_id"]
    assert linea.required_material_quantity == Decimal(1000)
    assert linea.required_material_uom == "g"


# ---------------------------------------------------------------------------
# 11 — antes bloquear que costear mal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_preparado_en_mililitros_sin_costo_propio_no_se_inventa_uno(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """SILENT_ML_AS_GRAM_COSTING = 0.

    De la receta solo sale un costo por GRAMO. Pasarlo a mililitros exigiria
    una densidad que el producto no tiene —`solids_g_per_ml` es de un lote, no
    del material—, asi que se bloquea en vez de devolver una cifra creible.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_ml")
    categoria = await create_category(api, admin_csrf, "Material base ml")
    liquido = await create_product(
        api,
        admin_csrf,
        product_category_id=categoria["id"],
        name="Barbotina liquida sin costo",
        product_type="PREPARED_MATERIAL",
        base_uom_code="ml",
    )
    assert liquido.status_code == 201, liquido.text

    preview = await _preview(
        api,
        admin_csrf,
        _payload(
            escena,
            quantity=10,
            body_material={
                "product_id": liquido.json()["id"],
                "quantity_per_piece": "250",
            },
        ),
    )

    linea = preview["items"][0]
    assert "BODY_MATERIAL_UNSUPPORTED_UOM_COSTING" in linea["warnings"]
    assert linea["complete"] is False
    assert Decimal(linea["materials_calculated"]) == Decimal(0)


@pytest.mark.asyncio
async def test_un_producto_terminado_no_puede_ser_el_cuerpo_de_otra_pieza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BODY_MATERIAL_PRODUCT_INVALID.

    El selector solo ofrece materia prima y preparados, pero la regla vive en
    el backend: un cliente que mande otra cosa se topa con el mismo limite.
    """
    escena = await _materials(api, admin_csrf, db_session, suffix="_invalid")
    preview = await _preview(
        api,
        admin_csrf,
        _payload(
            escena,
            quantity=10,
            body_material={
                "product_id": escena["producto"]["id"],
                "quantity_per_piece": "300",
            },
        ),
    )

    linea = preview["items"][0]
    assert "BODY_MATERIAL_PRODUCT_INVALID" in linea["warnings"]
    assert linea["complete"] is False


@pytest.mark.asyncio
async def test_el_catalogo_de_materiales_solo_ofrece_materia_y_preparados(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BODY_MATERIAL_SELECT: la lista la decide el backend, no el navegador."""
    escena = await _materials(api, admin_csrf, db_session, suffix="_catalog")
    respuesta = await api.get(f"{BUILDER}/body-materials", params={"limit": 200})
    assert respuesta.status_code == 200, respuesta.text

    items = respuesta.json()["items"]
    tipos = {item["product_type"] for item in items}
    assert tipos <= {"RAW_MATERIAL", "PREPARED_MATERIAL"}

    por_id = {item["product_id"]: item for item in items}
    assert escena["producto"]["id"] not in por_id

    preparado = por_id[escena["prepared_id"]]
    assert preparado["source"] == "PREPARED"
    assert preparado["uom"] == "g"
    # La receta se muestra como procedencia, nunca como selector principal.
    assert preparado["recipe_name"] == escena["receta"]["name"]
    assert preparado["costable"] is True

    assert por_id[escena["raw_id"]]["source"] == "RAW"
    assert por_id[escena["raw_id"]]["recipe_name"] is None
