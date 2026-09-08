"""Fase 009K.4 — la muestra se fabrica desde una orden de produccion.

Hasta aqui habia dos sistemas para el mismo hecho fisico: la orden servia a las
cotizaciones y la muestra tenia su propio arranque, su propio consumo y su
propia pantalla. Dos caminos para lo mismo acaban discrepando, y el taller
tenia que aprenderse los dos.

Lo que estas pruebas fijan, por orden de importancia:

1. que cobrar una cotizacion de prototipo deje TRES cosas y no mas: producto,
   muestra y orden, una de cada, tambien si se cobra dos veces;
2. que el almacen sea una decision explicita de quien cobra. No se hereda, no
   se deduce y no se toma «el unico que hay»: el dia que haya dos, un default
   silencioso descontaria del equivocado sin avisar;
3. que la orden sepa de donde viene —cotizacion o muestra, nunca las dos ni
   ninguna— y que eso lo imponga la BASE, no la buena voluntad del servicio;
4. que arrancar una muestra siga saliendo del almacen como `PROTOTYPE_OUT`.
   Cambiarlo a `PRODUCTION_OUT` porque ahora se arranca desde una orden habria
   reescrito el significado de todo el historico de inventario;
5. que a una muestra no se le exija estar aprobada para poder fabricarla. Seria
   pedirle que se apruebe antes de existir: se aprueba despues, mirandola.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import MovementType, StockBalance, StockMovement
from app.models.production import ProductionOrder, ProductionOrderStatus
from app.models.prototypes import Prototype, PrototypeMaterialLine
from tests.db.test_production_orders_api import crear_ubicacion, dar_existencia
from tests.db.test_prototype_quotations import (
    COTIZADOR,
    _caso_referencia,
    _payload,
    cobrar,
)
from tests.db.test_quotation_builder_api import head

ORDENES = "/api/v1/production-orders"


async def _cpr_confirmada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, sufijo: str
) -> dict[str, Any]:
    """Una cotizacion de prototipo emitida y lista para cobrar."""
    caso = await _caso_referencia(api, csrf, db_session, sufijo)
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(csrf))
    assert confirmada.status_code == 200, confirmada.text
    return {"documento": confirmada.json(), "caso": caso}


async def _saldo(db_session: AsyncSession, product_id: int, location_id: int) -> Decimal:
    valor = await db_session.scalar(
        select(StockBalance.quantity).where(
            StockBalance.product_id == product_id,
            StockBalance.location_id == location_id,
        )
    )
    return valor if valor is not None else Decimal(0)


# ---------------------------------------------------------------------------
# B01-B04, B44-B50: cobrar materializa, y una sola vez
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cobrar_deja_producto_muestra_y_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B01. Las tres cosas nacen del mismo cobro, y la orden trae su almacen."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_alta")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 alta")

    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)
    assert pagada.status_code == 200, pagada.text
    cuerpo = pagada.json()

    assert cuerpo["payment_status"] == "PAID"
    assert cuerpo["product_id"] is not None
    assert cuerpo["prototype_id"] is not None
    assert cuerpo["production_order_id"] is not None
    assert cuerpo["production_order_code"]

    orden = await db_session.get(ProductionOrder, cuerpo["production_order_id"])
    assert orden is not None
    assert orden.prototype_id == cuerpo["prototype_id"]
    assert orden.quotation_id is None, "una orden de muestra no tiene cotizacion"
    assert orden.stock_location_id == almacen, "el almacen es el que se pidio"
    assert orden.status is ProductionOrderStatus.CREATED


@pytest.mark.asyncio
async def test_cobrar_dos_veces_devuelve_las_mismas_tres_cosas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B02 + B03 + B04. Un reintento del navegador no duplica nada."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_idem")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 idem")

    primera = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)
    segunda = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)
    assert primera.status_code == 200 and segunda.status_code == 200, segunda.text

    for campo in ("product_id", "prototype_id", "production_order_id"):
        assert primera.json()[campo] == segunda.json()[campo], campo

    ordenes = await db_session.scalar(
        select(func.count())
        .select_from(ProductionOrder)
        .where(ProductionOrder.prototype_id == primera.json()["prototype_id"])
    )
    assert ordenes == 1


@pytest.mark.asyncio
async def test_un_segundo_cobro_con_otro_almacen_no_mueve_el_de_la_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B50. IDEMPOTENT_MARK_PAID_CHANGES_EXISTING_ORDER_WAREHOUSE: NO.

    Cambiarlo en silencio movería de sitio el material de algo ya decidido, y
    quien lo pidió no se enteraría hasta ver el saldo del almacén equivocado.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_dos")
    primero = await crear_ubicacion(api, admin_csrf, "Almacen K4 primero")
    otro = await crear_ubicacion(api, admin_csrf, "Almacen K4 otro")

    inicial = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=primero)
    repetido = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=otro)
    assert repetido.status_code == 200, repetido.text
    assert repetido.json()["production_order_id"] == inicial.json()["production_order_id"]

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, inicial.json()["production_order_id"])
    assert orden is not None
    assert orden.stock_location_id == primero


@pytest.mark.asyncio
async def test_cobrar_sin_almacen_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B44. El almacen no es opcional."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_sin")

    respuesta = await api.post(
        f"{COTIZADOR}/{escenario['documento']['id']}/mark-paid",
        json={},
        headers=head(admin_csrf),
    )

    assert respuesta.status_code == 422, respuesta.text


@pytest.mark.asyncio
async def test_cobrar_con_un_almacen_inexistente_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B45. Y no deja el cobro a medias."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_fantasma")

    respuesta = await cobrar(
        api, admin_csrf, escenario["documento"]["id"], stock_location_id=999_999
    )

    assert respuesta.status_code == 422, respuesta.text
    db_session.expire_all()
    documento = await api.get(
        f"{COTIZADOR}/{escenario['documento']['id']}", headers=head(admin_csrf)
    )
    assert documento.json()["payment_status"] != "PAID", "un cobro rechazado no cobra"
    assert documento.json()["prototype_id"] is None, "ni materializa media muestra"


@pytest.mark.asyncio
async def test_cobrar_con_un_almacen_desactivado_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B46. Un almacen apagado no puede recibir una orden."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_apagado")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 apagado")
    await db_session.execute(
        text("UPDATE stock_locations SET active = false WHERE id = :id"), {"id": almacen}
    )
    await db_session.commit()

    respuesta = await cobrar(
        api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen
    )

    assert respuesta.status_code == 422, respuesta.text


# ---------------------------------------------------------------------------
# B06-B12: el origen y sus restricciones
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_orden_no_puede_quedarse_sin_origen(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B09. Sin origen no sabria que fabricar, y la base no la deja existir."""
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO production_orders (code, stock_location_id, status, qr_token)"
                " VALUES ('OP-SIN-ORIGEN', 1, 'CREATED', :token)"
            ),
            {"token": "t" * 40},
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_una_orden_no_puede_tener_los_dos_origenes(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B10. Con los dos tendria dos modelos de material contradictorios."""
    from tests.db.test_production_orders_api import confirmar
    from tests.db.test_production_orders_api import escenario as escenario_ctz

    # Hace falta una cotizacion DE VERDAD: con una subconsulta vacia el UPDATE
    # dejaria `quotation_id` en nulo y la restriccion no llegaria a mirarse.
    datos = await escenario_ctz(api, admin_csrf, db_session, suffix="_k4_ctz_doble")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])

    caso = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_dos_origenes")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 doble")
    pagada = await cobrar(api, admin_csrf, caso["documento"]["id"], stock_location_id=almacen)
    orden_id = pagada.json()["production_order_id"]

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("UPDATE production_orders SET quotation_id = :ctz WHERE id = :id"),
            {"ctz": confirmada["id"], "id": orden_id},
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_la_linea_de_la_orden_de_muestra_apunta_al_producto_sin_item(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B12 + B13. Una sola linea, con la pieza, y sin item de cotizacion."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_linea")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 linea")
    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)

    detalle = await api.get(
        f"{ORDENES}/{pagada.json()['production_order_id']}", headers=head(admin_csrf)
    )
    assert detalle.status_code == 200, detalle.text
    cuerpo = detalle.json()

    assert len(cuerpo["lines"]) == 1
    linea = cuerpo["lines"][0]
    assert linea["quotation_item_id"] is None
    assert linea["product_id"] == pagada.json()["product_id"]
    # No se sintetiza receta: el material de una muestra se eligio a mano.
    assert linea["recipe_id"] is None
    assert linea["prepared_product_id"] is None


@pytest.mark.asyncio
async def test_el_detalle_dice_de_donde_viene_la_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B17. PRODUCTION_ORDER_ORIGIN_BACKEND_AUTHORITY.

    El origen viaja explicito. Que el navegador lo dedujera de que campo venga
    relleno convertiria una regla del dominio en una heuristica de pantalla.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_origen")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 origen")
    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)

    detalle = (
        await api.get(f"{ORDENES}/{pagada.json()['production_order_id']}", headers=head(admin_csrf))
    ).json()

    assert detalle["origin_type"] == "PROTOTYPE"
    assert detalle["quotation_id"] is None
    assert detalle["quotation_code"] is None
    assert detalle["prototype_id"] == pagada.json()["prototype_id"]
    assert detalle["prototype_code"] == pagada.json()["prototype_code"]
    assert detalle["prototype_quotation_code"] == escenario["documento"]["code"]


@pytest.mark.asyncio
async def test_la_orden_de_muestra_sale_en_la_lista_general(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_ORDER_IN_MAIN_PRODUCTION_LIST: no hay una lista aparte."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_lista")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 lista")
    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)

    listado = await api.get(f"{ORDENES}?limit=50", headers=head(admin_csrf))
    assert listado.status_code == 200, listado.text
    filas = {fila["id"]: fila for fila in listado.json()["items"]}

    fila = filas[pagada.json()["production_order_id"]]
    assert fila["origin_type"] == "PROTOTYPE"
    assert fila["prototype_code"] == pagada.json()["prototype_code"]
    assert fila["quotation_code"] is None


# ---------------------------------------------------------------------------
# B22-B27: arrancar desde la orden
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_arrancar_la_orden_consume_como_prototype_out(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B22 + B23 + B24 + B25. El arranque visible es el de la orden.

    Y sale del almacen como `PROTOTYPE_OUT`, no como `PRODUCTION_OUT`: el tipo
    de movimiento describe QUE se fabrico, y eso no cambia porque haya cambiado
    la pantalla desde la que se pulsa.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_start")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 start")
    pasta = escenario["caso"]["_pasta"]
    await dar_existencia(
        api, admin_csrf, product_id=pasta["id"], location_id=almacen, cantidad="10000"
    )
    antes = await _saldo(db_session, pasta["id"], almacen)

    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)
    orden_id = pagada.json()["production_order_id"]
    muestra_id = pagada.json()["prototype_id"]

    arrancada = await api.post(f"{ORDENES}/{orden_id}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text
    assert arrancada.json()["status"] == "STARTED"

    db_session.expire_all()
    despues = await _saldo(db_session, pasta["id"], almacen)
    assert despues < antes, "el arranque descuenta de verdad"

    movimientos = list(
        (
            await db_session.execute(
                select(StockMovement).where(StockMovement.prototype_id == muestra_id)
            )
        )
        .scalars()
        .all()
    )
    assert movimientos, "el consumo deja rastro ligado a la muestra"
    assert all(m.movement_type is MovementType.PROTOTYPE_OUT for m in movimientos)

    # Y lo REAL queda escrito en la linea de la muestra, como antes.
    lineas = list(
        (
            await db_session.execute(
                select(PrototypeMaterialLine).where(
                    PrototypeMaterialLine.prototype_id == muestra_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert lineas and all(linea.quantity_actual is not None for linea in lineas)


@pytest.mark.asyncio
async def test_arrancar_dos_veces_no_consume_dos_veces(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B26. START_DOUBLE_CONSUMPTION: NO."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_doble")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 doble start")
    pasta = escenario["caso"]["_pasta"]
    await dar_existencia(
        api, admin_csrf, product_id=pasta["id"], location_id=almacen, cantidad="10000"
    )

    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)
    orden_id = pagada.json()["production_order_id"]

    await api.post(f"{ORDENES}/{orden_id}/start", headers=head(admin_csrf))
    db_session.expire_all()
    tras_uno = await _saldo(db_session, pasta["id"], almacen)

    segunda = await api.post(f"{ORDENES}/{orden_id}/start", headers=head(admin_csrf))
    assert segunda.status_code == 200, segunda.text
    db_session.expire_all()
    assert await _saldo(db_session, pasta["id"], almacen) == tras_uno

    movimientos = await db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.prototype_id == pagada.json()["prototype_id"])
    )
    assert movimientos == 1, "un solo movimiento por linea, no dos"


@pytest.mark.asyncio
async def test_a_la_muestra_no_se_le_exige_estar_aprobada_para_fabricarla(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B27. PROTOTYPE_ORDER_START_REQUIRES_OWN_APPROVAL: NO.

    Seria pedirle que se apruebe antes de existir. La aprobacion viene despues,
    mirando la pieza terminada.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_aprob")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 aprobacion")
    pasta = escenario["caso"]["_pasta"]
    await dar_existencia(
        api, admin_csrf, product_id=pasta["id"], location_id=almacen, cantidad="10000"
    )
    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)

    muestra = await db_session.get(Prototype, pagada.json()["prototype_id"])
    assert muestra is not None
    assert muestra.approval.value == "PENDING", "sin aprobar, que es lo normal al nacer"

    arrancada = await api.post(
        f"{ORDENES}/{pagada.json()['production_order_id']}/start", headers=head(admin_csrf)
    )
    assert arrancada.status_code == 200, arrancada.text


@pytest.mark.asyncio
async def test_completar_no_vuelve_a_consumir_ni_aprueba_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B29 + B30. Completar es un hecho de taller, no un veredicto."""
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_fin")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen K4 completar")
    pasta = escenario["caso"]["_pasta"]
    await dar_existencia(
        api, admin_csrf, product_id=pasta["id"], location_id=almacen, cantidad="10000"
    )
    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen)
    orden_id = pagada.json()["production_order_id"]
    await api.post(f"{ORDENES}/{orden_id}/start", headers=head(admin_csrf))
    db_session.expire_all()
    tras_arranque = await _saldo(db_session, pasta["id"], almacen)

    completada = await api.post(f"{ORDENES}/{orden_id}/complete", headers=head(admin_csrf))
    assert completada.status_code == 200, completada.text
    assert completada.json()["status"] == "COMPLETED"

    db_session.expire_all()
    assert await _saldo(db_session, pasta["id"], almacen) == tras_arranque
    muestra = await db_session.get(Prototype, pagada.json()["prototype_id"])
    assert muestra is not None
    assert muestra.approval.value == "PENDING", "completar no decide si la muestra sirve"


# ---------------------------------------------------------------------------
# B08 + B36: la cotizacion sigue igual
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_orden_de_cotizacion_no_tiene_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B08 + B36. CTZ_PRODUCTION_ORDER_CREATION_REGRESSION."""
    from tests.db.test_production_orders_api import confirmar, crear_orden, escenario

    datos = await escenario(
        api, admin_csrf, db_session, suffix="_k4_ctz", existencia_preparado="10000"
    )
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    creada = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )
    assert creada.status_code == 201, creada.text
    orden = creada.json()

    fila = await db_session.get(ProductionOrder, orden["id"])
    assert fila is not None
    assert fila.prototype_id is None
    assert fila.quotation_id is not None

    detalle = (await api.get(f"{ORDENES}/{orden['id']}", headers=head(admin_csrf))).json()
    assert detalle["origin_type"] == "QUOTATION"
    assert detalle["prototype_id"] is None
    assert detalle["quotation_code"]
    # Y su linea sigue copiando el item confirmado.
    assert all(linea["quotation_item_id"] is not None for linea in detalle["lines"])
