"""Fase 009I — arrancar la orden: el unico punto que mueve inventario.

Todo lo de aqui gira alrededor de una sola pregunta: **cuando se descuenta el
material, y cuando no se descuenta nada.** Las pruebas negativas importan mas
que las positivas, porque el fallo caro no es que la orden no arranque sino que
arranque a medias y deje el almacen contando una cosa distinta de la que hay.

Por eso casi todas comprueban lo mismo al final: cero movimientos nuevos, saldo
intacto y la orden todavia en CREATED.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import MovementType, StockBalance, StockMovement
from app.models.masters import Product
from app.models.production import ProductionOrder, ProductionOrderStatus
from app.models.recipes import Recipe
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    dar_existencia,
    escenario,
)
from tests.db.test_quotation_builder_api import head


async def _preparada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, **kwargs: Any
) -> dict[str, Any]:
    """Escenario confirmado + orden creada, lista para arrancar (o no)."""
    datos = await escenario(api, csrf, db_session, **kwargs)
    confirmada = await confirmar(api, csrf, datos["quotation"])
    creada = await crear_orden(
        api, csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    return {**datos, "confirmada": confirmada, "orden": creada.json()}


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


def _codigos(respuesta: httpx.Response) -> set[str]:
    detalles = respuesta.json()["error"].get("details") or []
    return {str(detalle["code"]) for detalle in detalles}


# ---------------------------------------------------------------------------
# CASO A — el camino feliz
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_arrancar_descuenta_el_material_y_deja_el_movimiento(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El unico caso de toda la fase en el que el inventario cambia.

    100 piezas x 10 g = 1000 g de preparado, descontados de los 5000 que
    habia. El movimiento no es un adorno: es la evidencia de por que el saldo
    bajo, y lleva la orden que lo origino para poder rehacer la historia.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_ok")
    preparado, ubicacion = datos["prepared_product_id"], datos["location_id"]
    assert await _saldo(db_session, preparado, ubicacion) == Decimal("5000")

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "STARTED"
    assert respuesta.json()["started_at"] is not None
    assert await _saldo(db_session, preparado, ubicacion) == Decimal("4000")

    movimiento = (
        await db_session.execute(
            select(StockMovement).where(StockMovement.production_order_id == datos["orden"]["id"])
        )
    ).scalar_one()
    assert movimiento.movement_type is MovementType.PRODUCTION_OUT
    assert movimiento.quantity == Decimal("-1000")
    assert movimiento.balance_after == Decimal("4000")
    assert movimiento.product_id == preparado
    assert movimiento.location_id == ubicacion


@pytest.mark.asyncio
async def test_producir_no_vuelve_a_consumir_los_componentes_de_la_receta(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_DOES_NOT_DOUBLE_CONSUME_RECIPE_COMPONENTS.

    La materia prima ya se gasto al PREPARAR el barniz. Descontarla otra vez al
    producir la contaria dos veces: una al convertirla en barniz y otra al
    usarlo. Produccion solo toca el preparado.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_comp")
    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text

    db_session.expire_all()
    tipos = (
        (
            await db_session.execute(
                select(StockMovement.movement_type).where(
                    StockMovement.production_order_id == datos["orden"]["id"]
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(tipos) == {MovementType.PRODUCTION_OUT}
    assert MovementType.PREPARATION_OUT not in set(tipos)


# ---------------------------------------------------------------------------
# CASOS B, C, D — falta un dato tecnico
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_receta_no_arranca_y_no_mueve_nada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MISSING_RECIPE. Es el caso de 25 de las 29 lineas confirmadas reales.

    Sin receta no hay forma de saber que material lleva la pieza, y no se
    inventa: no se deriva de su precio, de sus medidas ni del costo manual de
    materiales que llevara la cotizacion.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_norec", con_receta=False)
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_READY"
    assert "MISSING_RECIPE" in _codigos(respuesta)
    assert await _movimientos(db_session) == antes
    releida = await api.get(f"{ORDERS}/{datos['orden']['id']}")
    assert releida.json()["status"] == "CREATED"


@pytest.mark.asyncio
async def test_sin_gramos_por_pieza_no_arranca_y_no_mueve_nada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MISSING_MATERIAL_GRAMS.

    Hay receta, pero nadie dijo cuanto material lleva cada pieza. El peso NO se
    deduce del gramaje del producto ni del importe de materiales: serian
    numeros presentables y falsos, y descontarian del almacen una cantidad que
    nadie decidio.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_nogr", gramos_por_pieza=None)
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert "MISSING_MATERIAL_GRAMS" in _codigos(respuesta)
    assert await _movimientos(db_session) == antes


@pytest.mark.asyncio
async def test_una_receta_que_no_produce_preparado_no_arranca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PREPARED_PRODUCT_NOT_RESOLVABLE.

    La receta apunta a un producto que no es material preparado. Antes de
    resolver la orden no se sabe; al resolverla, se prefiere no descontar de un
    producto cualquiera solo porque la receta lo senale.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_nopre")
    categoria = await create_category(api, admin_csrf, "Terminado 009I")
    terminado = await create_product(
        api,
        admin_csrf,
        product_category_id=categoria["id"],
        name="Pieza terminada 009I",
        product_type="FINISHED_PRODUCT",
        base_uom_code="unit",
    )
    assert terminado.status_code == 201, terminado.text
    await db_session.execute(
        update(Recipe)
        .where(Recipe.id == datos["receta"]["id"])
        .values(product_id=terminado.json()["id"])
    )
    await db_session.commit()

    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    creada = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    assert creada.json()["lines"][0]["prepared_product_id"] is None
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{creada.json()['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert "PREPARED_PRODUCT_NOT_RESOLVABLE" in _codigos(respuesta)
    assert await _movimientos(db_session) == antes


# ---------------------------------------------------------------------------
# CASOS E y F — el material no esta
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_existencia_del_preparado_no_arranca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PREPARED_STOCK_MISSING.

    Es el estado real de produccion hoy: la receta existe, el preparado existe
    como producto, y de ese preparado no se ha hecho nunca un lote. Se
    distingue de «hay pero no alcanza» porque quien lo lee necesita saber que
    ese barniz no se ha preparado jamas.
    """
    datos = await _preparada(
        api, admin_csrf, db_session, suffix="_sinstock", existencia_preparado=None
    )
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert "PREPARED_STOCK_MISSING" in _codigos(respuesta)
    assert await _movimientos(db_session) == antes


@pytest.mark.asyncio
async def test_con_existencia_insuficiente_no_arranca_ni_a_medias(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """INSUFFICIENT_STOCK. Hacen falta 1000 g y solo hay 400.

    Lo que se comprueba no es el rechazo sino que NO se descuenten los 400 que
    si habia. Un consumo parcial dejaria el almacen vacio y la orden sin
    arrancar: lo peor de las dos opciones.
    """
    datos = await _preparada(
        api, admin_csrf, db_session, suffix="_poco", existencia_preparado="400"
    )
    preparado, ubicacion = datos["prepared_product_id"], datos["location_id"]
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert "INSUFFICIENT_STOCK" in _codigos(respuesta)
    assert await _movimientos(db_session) == antes
    assert await _saldo(db_session, preparado, ubicacion) == Decimal("400")


# ---------------------------------------------------------------------------
# CASO G — gramos a mililitros: la conversion que NO existe
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_preparado_en_mililitros_bloquea_el_arranque(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """G_TO_ML_WITHOUT_PREPARATION_LOT_BLOCKED.

    La receta pide GRAMOS y el preparado se lleva en MILILITROS. El puente
    entre los dos es `solids_g_per_ml`, y esa cifra es de UN lote concreto, no
    del producto: dos lotes del mismo barniz con distinta agua tienen
    concentraciones distintas.

    Como la orden todavia no elige lote, no hay conversion legitima. Suponer
    1 g = 1 ml, o promediar los lotes, daria un numero impecable y falso. Se
    bloquea, y se dice por que.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_ml")
    await db_session.execute(
        update(Product).where(Product.id == datos["prepared_product_id"]).values(base_uom_code="ml")
    )
    await db_session.commit()

    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    creada = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{creada.json()['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert "UNSUPPORTED_UOM_CONVERSION" in _codigos(respuesta)
    assert await _movimientos(db_session) == antes


@pytest.mark.asyncio
async def test_un_preparado_en_kilos_si_convierte_con_el_maestro_de_unidades(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La contraparte de la anterior: dentro de la MASA la conversion si existe.

    Gramos a kilos es un factor fijo del maestro de unidades —1 kg = 1000 g— y
    no depende de ningun lote. 1000 g pedidos salen del saldo en kilos como
    1 kg exacto, con `Decimal` y sin flotantes.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_kg", existencia_preparado=None)
    await db_session.execute(
        update(Product).where(Product.id == datos["prepared_product_id"]).values(base_uom_code="kg")
    )
    await db_session.commit()
    await dar_existencia(
        api,
        admin_csrf,
        product_id=datos["prepared_product_id"],
        location_id=datos["location_id"],
        cantidad="5",
    )

    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    creada = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text

    respuesta = await api.post(f"{ORDERS}/{creada.json()['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 200, respuesta.text
    assert await _saldo(db_session, datos["prepared_product_id"], datos["location_id"]) == Decimal(
        "4"
    )


# ---------------------------------------------------------------------------
# CASO H — arrancar dos veces
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_arrancar_dos_veces_consume_una_sola_vez(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DOUBLE_START_NO_NEW_MOVEMENTS / DOUBLE_START_STARTED_AT_STABLE.

    Idempotencia por el HECHO, no por la peticion: da igual con que clave
    llegue el segundo intento, la orden ya consumio y no vuelve a hacerlo.

    `started_at` tampoco se mueve. Una fecha de arranque que se desplaza cada
    vez que alguien pulsa el boton deja de ser la fecha en que se arranco.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_dos")
    preparado, ubicacion = datos["prepared_product_id"], datos["location_id"]

    primera = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    assert primera.status_code == 200, primera.text
    movimientos_tras_la_primera = await _movimientos(db_session)
    saldo_tras_la_primera = await _saldo(db_session, preparado, ubicacion)

    segunda = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["status"] == "STARTED"
    assert segunda.json()["started_at"] == primera.json()["started_at"]
    assert await _movimientos(db_session) == movimientos_tras_la_primera
    assert await _saldo(db_session, preparado, ubicacion) == saldo_tras_la_primera


# ---------------------------------------------------------------------------
# Completar y anular
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_completar_no_da_de_alta_producto_terminado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMPLETE_NO_FINISHED_GOODS_STOCK / COMPLETE_NO_EXTRA_MATERIAL_CONSUMPTION.

    Cerrar la orden no crea existencia de la pieza acabada. No hay reglas
    acordadas sobre en que almacen entraria, con que merma ni con que
    valoracion, y una entrada inventada seria peor que ninguna.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_fin")
    arrancada = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text
    movimientos = await _movimientos(db_session)

    respuesta = await api.post(
        f"{ORDERS}/{datos['orden']['id']}/complete", headers=head(admin_csrf)
    )

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "COMPLETED"
    assert respuesta.json()["completed_at"] is not None
    assert await _movimientos(db_session) == movimientos
    # Ni un saldo del producto terminado.
    db_session.expire_all()
    saldo_terminado = await db_session.scalar(
        select(func.count())
        .select_from(StockBalance)
        .where(StockBalance.product_id == datos["producto"]["id"])
    )
    assert saldo_terminado == 0


@pytest.mark.asyncio
async def test_anular_una_orden_creada_no_toca_el_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCEL_CREATED_NO_STOCK_EFFECT."""
    datos = await _preparada(api, admin_csrf, db_session, suffix="_anul")
    antes = await _movimientos(db_session)
    saldo = await _saldo(db_session, datos["prepared_product_id"], datos["location_id"])

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/cancel", headers=head(admin_csrf))

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["status"] == "CANCELLED"
    assert await _movimientos(db_session) == antes
    assert await _saldo(db_session, datos["prepared_product_id"], datos["location_id"]) == saldo


@pytest.mark.asyncio
async def test_una_orden_arrancada_no_se_puede_anular(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCEL_STARTED_REJECTED.

    Anular no devuelve a los sacos el barniz que ya se uso. Permitirlo dejaria
    el inventario contando una cosa y el documento diciendo otra; si hubo un
    error, se corrige con un ajuste, que deja su propia evidencia y su propio
    responsable.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_anularr")
    arrancada = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/cancel", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_CANCELLABLE"


@pytest.mark.asyncio
async def test_una_orden_completada_no_se_puede_anular(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCEL_COMPLETED_REJECTED."""
    datos = await _preparada(api, admin_csrf, db_session, suffix="_anulcomp")
    assert (
        await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))
    ).status_code == 200
    assert (
        await api.post(f"{ORDERS}/{datos['orden']['id']}/complete", headers=head(admin_csrf))
    ).status_code == 200

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/cancel", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_CANCELLABLE"


@pytest.mark.asyncio
async def test_una_orden_anulada_no_puede_arrancar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _preparada(api, admin_csrf, db_session, suffix="_anulstart")
    assert (
        await api.post(f"{ORDERS}/{datos['orden']['id']}/cancel", headers=head(admin_csrf))
    ).status_code == 200
    antes = await _movimientos(db_session)

    respuesta = await api.post(f"{ORDERS}/{datos['orden']['id']}/start", headers=head(admin_csrf))

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_NOT_STARTABLE"
    assert await _movimientos(db_session) == antes


@pytest.mark.asyncio
async def test_anular_la_cotizacion_no_anula_la_orden_por_su_cuenta(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Sin cascada automatica en 009I.

    Que un comercial anule el documento no puede parar solo una fabricacion que
    quiza ya esta en el horno. Si hay que pararla, alguien la para a mano y
    queda registrado quien fue.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_casc")
    anulada = await api.post(
        f"/api/v1/quotation-builder/{datos['confirmada']['id']}/cancel",
        headers=head(admin_csrf),
    )
    assert anulada.status_code == 200, anulada.text

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, datos["orden"]["id"])
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED
