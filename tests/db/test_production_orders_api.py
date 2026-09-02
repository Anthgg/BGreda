"""Fase 009I — crear una orden de produccion desde una cotizacion confirmada.

Lo que se comprueba aqui es la mitad INOCENTE del flujo: crear la orden congela
que hay que fabricar, reserva el correlativo y **no toca ni un gramo de
inventario**. El consumo fisico vive en `test_production_start.py`.

La regresion que mas importa es la ultima: una cotizacion tiene como mucho una
orden, y pedirla dos veces devuelve la misma en vez de duplicar el compromiso
de material.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from app.models.production import ProductionOrder
from app.models.quotations import Quotation
from tests.db.test_firings_api import FACTORES_CHICO, crear_horno
from tests.db.test_quotation_builder_api import head
from tests.db.test_quotations_api import _finished_product_and_recipe

BUILDER = "/api/v1/quotation-builder"
ORDERS = "/api/v1/production-orders"
PARTNERS = "/api/v1/partners"
LOCATIONS = "/api/v1/inventory/locations"
ADJUSTMENTS = "/api/v1/inventory/adjustments"
RECIPES = "/api/v1/recipes"


async def crear_ubicacion(api: httpx.AsyncClient, csrf: str, nombre: str) -> int:
    respuesta = await api.post(LOCATIONS, json={"name": nombre}, headers=head(csrf))
    assert respuesta.status_code == 201, respuesta.text
    return int(respuesta.json()["id"])


async def dar_existencia(
    api: httpx.AsyncClient, csrf: str, *, product_id: int, location_id: int, cantidad: str
) -> None:
    respuesta = await api.post(
        ADJUSTMENTS,
        json={
            "product_id": product_id,
            "location_id": location_id,
            "quantity": cantidad,
            "reason": "Carga de prueba 009I",
        },
        headers=head(csrf),
    )
    assert respuesta.status_code == 201, respuesta.text


async def escenario(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str = "",
    gramos_por_pieza: str | None = "10",
    cantidad: int = 100,
    con_receta: bool = True,
    existencia_preparado: str | None = "5000",
) -> dict[str, Any]:
    """Una cotizacion CONFIRMADA lista para producir, con sus piezas movibles.

    Los parametros existen para poder romper una sola cosa cada vez: quitar la
    receta, quitar los gramos o dejar el preparado sin existencia. Cada uno de
    esos casos tiene que bloquear el arranque por su propio motivo, y solo se
    puede distinguir si el resto del escenario sigue intacto.
    """
    cliente = await api.post(
        PARTNERS,
        json={
            "name": f"Cliente Produccion{suffix}",
            "role": "CLIENT",
            "document_type": "RUC",
            "document_number": f"206{abs(hash(suffix)) % 100000000:08d}",
        },
        headers=head(csrf),
    )
    assert cliente.status_code == 201, cliente.text

    producto, receta = await _finished_product_and_recipe(api, csrf, f"_prod{suffix}")
    horno = await crear_horno(
        api,
        csrf,
        db_session,
        nombre=f"Horno Produccion{suffix}",
        capacidad="10000000",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )

    item: dict[str, Any] = {
        "product_id": producto["id"],
        "quantity": cantidad,
        "dimensions": {"width": "24", "length": "24", "height": "8"},
        "other_costs": [],
        "markup_percent": "100",
        "commercial_sale_unit_price": "8.50",
        "sort_order": 0,
    }
    if con_receta:
        item["recipe_id"] = receta["id"]
        item["recipe_version_id"] = receta["current_version"]["id"]
    if gramos_por_pieza is not None:
        item["material_grams_per_piece"] = gramos_por_pieza

    creada = await api.post(
        BUILDER,
        json={
            "name": f"Pedido produccion{suffix}",
            "customer_id": cliente.json()["id"],
            "kiln_id": horno["id"],
            "items": [item],
        },
        headers=head(csrf),
    )
    assert creada.status_code == 201, creada.text
    borrador = creada.json()

    location_id = await crear_ubicacion(api, csrf, f"Almacen Produccion{suffix}")
    if existencia_preparado is not None:
        await dar_existencia(
            api,
            csrf,
            product_id=receta["product_id"],
            location_id=location_id,
            cantidad=existencia_preparado,
        )

    return {
        "quotation": borrador,
        "producto": producto,
        "receta": receta,
        "prepared_product_id": receta["product_id"],
        "location_id": location_id,
    }


async def confirmar(api: httpx.AsyncClient, csrf: str, quotation: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(
        f"{BUILDER}/{quotation['id']}/confirm",
        json={"expected_updated_at": quotation["updated_at"]},
        headers=head(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text
    return dict(respuesta.json())


async def crear_orden(
    api: httpx.AsyncClient, csrf: str, *, quotation_id: int, location_id: int, **extra: Any
) -> httpx.Response:
    return await api.post(
        ORDERS,
        json={"quotation_id": quotation_id, "stock_location_id": location_id, **extra},
        headers=head(csrf),
    )


async def _inventario(db_session: AsyncSession) -> tuple[int, list[Decimal]]:
    movimientos = await db_session.scalar(select(func.count()).select_from(StockMovement))
    saldos = (
        (await db_session.execute(select(StockBalance.quantity).order_by(StockBalance.id)))
        .scalars()
        .all()
    )
    return int(movimientos or 0), list(saldos)


# ---------------------------------------------------------------------------
# CASOS 1 y 2 — la frontera comercial
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_borrador_no_puede_originar_una_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DRAFT_CANNOT_CREATE_PRODUCTION_ORDER.

    Un borrador todavia no es un compromiso con nadie: sus precios, sus
    cantidades y sus recetas siguen cambiando. Fabricar contra un documento que
    aun se edita es fabricar contra algo que manana dira otra cosa.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_draft")

    respuesta = await crear_orden(
        api,
        admin_csrf,
        quotation_id=datos["quotation"]["id"],
        location_id=datos["location_id"],
    )

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_QUOTATION_NOT_CONFIRMED"
    assert await db_session.scalar(select(func.count()).select_from(ProductionOrder)) == 0


@pytest.mark.asyncio
async def test_una_cotizacion_anulada_no_puede_originar_una_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCELLED_CANNOT_CREATE_PRODUCTION_ORDER."""
    datos = await escenario(api, admin_csrf, db_session, suffix="_cancel")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    anulada = await api.post(f"{BUILDER}/{confirmada['id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text

    respuesta = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )

    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_QUOTATION_NOT_CONFIRMED"


# ---------------------------------------------------------------------------
# CASOS 3, 4 y 5 — el pago NO es requisito
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cobrar", [False, True])
@pytest.mark.asyncio
async def test_producir_no_exige_haber_cobrado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession, cobrar: bool
) -> None:
    """PAYMENT_NOT_REQUIRED_FOR_PRODUCTION_ORDER.

    Cobrar (009H) y fabricar (009I) son ejes distintos. Atarlos aqui pararia el
    taller por una gestion administrativa, y el taller no es quien cobra.

    Se prueban los dos extremos del eje: sin cobrar y cobrada. El tercer caso
    —`payment_status` nulo, las historicas anteriores a 009H— no se puede
    montar por la API porque toda cotizacion nueva nace UNPAID; lo cubre
    `test_una_confirmada_sin_registro_de_pago_tambien_produce`.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix=f"_pago{cobrar}")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    if cobrar:
        pagada = await api.post(f"{BUILDER}/{confirmada['id']}/mark-paid", headers=head(admin_csrf))
        assert pagada.status_code == 200, pagada.text
        assert pagada.json()["payment_status"] == "PAID"
    else:
        assert confirmada["payment_status"] == "UNPAID"

    respuesta = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )

    assert respuesta.status_code == 201, respuesta.text
    assert respuesta.json()["status"] == "CREATED"


@pytest.mark.asyncio
async def test_una_confirmada_sin_registro_de_pago_tambien_produce(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El tercer caso del eje de pago: NULL, que no es UNPAID.

    Es el estado real de las 18 confirmadas anteriores a 009H. No saber si se
    cobraron no puede impedir fabricarlas, asi que el nulo se pone a mano
    porque la API ya no lo produce.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_nulo")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    await db_session.execute(
        Quotation.__table__.update()
        .where(Quotation.__table__.c.id == confirmada["id"])
        .values(payment_status=None, paid_at=None)
    )
    await db_session.commit()

    respuesta = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )

    assert respuesta.status_code == 201, respuesta.text


# ---------------------------------------------------------------------------
# CREATE no toca inventario
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crear_la_orden_no_mueve_ni_un_gramo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CREATE_ORDER_NO_INVENTORY_MUTATION.

    La frontera de la fase, escrita como prueba: el papeleo no gasta material.
    Crear tambien copia la cotizacion, resuelve el preparado y calcula cuanto
    hara falta, y nada de eso puede tocar un saldo.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_sinmov")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    movimientos_antes, saldos_antes = await _inventario(db_session)

    respuesta = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )
    assert respuesta.status_code == 201, respuesta.text

    db_session.expire_all()
    movimientos_despues, saldos_despues = await _inventario(db_session)
    assert movimientos_despues == movimientos_antes
    assert saldos_despues == saldos_antes

    orden = respuesta.json()
    assert orden["status"] == "CREATED"
    assert orden["started_at"] is None
    assert orden["code"].startswith("OP-")
    # El requerimiento ya esta calculado y congelado: 100 piezas x 10 g.
    assert Decimal(orden["lines"][0]["required_material_quantity"]) == Decimal("1000")
    assert orden["lines"][0]["required_material_uom"] == "g"
    assert orden["readiness"]["ready"] is True


@pytest.mark.asyncio
async def test_crear_la_orden_no_crea_quema_ni_preparacion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CREATE_ORDER_NO_FIRING / CREATE_ORDER_NO_PREPARATION."""
    from app.models.recipes import RecipePreparation

    datos = await escenario(api, admin_csrf, db_session, suffix="_nofiring")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    preparaciones_antes = await db_session.scalar(
        select(func.count()).select_from(RecipePreparation)
    )

    respuesta = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )
    assert respuesta.status_code == 201, respuesta.text

    db_session.expire_all()
    assert (
        await db_session.scalar(select(func.count()).select_from(RecipePreparation))
        == preparaciones_antes
    )


# ---------------------------------------------------------------------------
# CASO 6 — una sola orden por cotizacion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pedir_la_orden_dos_veces_no_crea_una_segunda(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DOUBLE_CREATE_NO_DUPLICATE_PRODUCTION_ORDER.

    Dos ordenes de la misma cotizacion serian dos compromisos de material para
    un unico pedido: al arrancar las dos, el taller descontaria el doble de
    barniz del que nadie pidio. La segunda peticion devuelve la orden que ya
    existe, y responde 200 en vez de 201 para que el cliente sepa que no acaba
    de crear nada.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_doble")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])

    primera = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )
    assert primera.status_code == 201, primera.text

    segunda = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["id"] == primera.json()["id"]
    assert segunda.json()["code"] == primera.json()["code"]

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(ProductionOrder)) == 1


@pytest.mark.asyncio
async def test_la_unicidad_por_cotizacion_la_impone_la_base(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ONE_ORDER_PER_QUOTATION_DB_ENFORCED.

    No basta con que el servicio compruebe antes de insertar: dos peticiones
    simultaneas pasan las dos esa comprobacion. Se prueba saltandose el
    servicio por completo e insertando a mano, que es lo que la concurrencia
    acaba haciendo.
    """
    from sqlalchemy.exc import IntegrityError

    datos = await escenario(api, admin_csrf, db_session, suffix="_unique")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    primera = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )
    assert primera.status_code == 201, primera.text

    db_session.add(
        ProductionOrder(
            code="OP-DUPLICADA-A-MANO",
            quotation_id=confirmada["id"],
            stock_location_id=datos["location_id"],
            qr_token="x" * 40,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Ubicacion explicita
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_ubicacion_es_obligatoria_y_no_se_resuelve_sola(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NO_SILENT_DEFAULT_STOCK_LOCATION.

    Hoy solo hay un almacen en produccion, y esa es exactamente la razon para
    exigirla: el dia que haya dos, un default silencioso descontaria del
    equivocado sin que nadie lo notara hasta el inventario.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_ubic")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])

    sin_ubicacion = await api.post(
        ORDERS, json={"quotation_id": confirmada["id"]}, headers=head(admin_csrf)
    )
    assert sin_ubicacion.status_code == 422, sin_ubicacion.text

    inexistente = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=999_999
    )
    assert inexistente.status_code == 422, inexistente.text
    assert inexistente.json()["error"]["code"] == "PRODUCTION_ORDER_LOCATION_INVALID"


# ---------------------------------------------------------------------------
# La orden copia la cotizacion confirmada, no el maestro vivo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_linea_copia_lo_confirmado_y_no_lo_que_diga_el_maestro_despues(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_LINES_USE_CONFIRMED_QUOTATION_SNAPSHOT.

    Entre crear la orden y arrancarla puede pasar cualquier cosa con el
    maestro. Lo que se fabrica es lo que se decidio al confirmar, no lo que
    diga la receta esa tarde: si la receta se reapunta a otro preparado, la
    orden ya creada sigue apuntando al que se resolvio en su momento.
    """
    from app.models.recipes import Recipe
    from tests.db.test_masters_api import create_category, create_product

    datos = await escenario(api, admin_csrf, db_session, suffix="_snap")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    creada = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
    )
    assert creada.status_code == 201, creada.text
    preparado_original = creada.json()["lines"][0]["prepared_product_id"]
    assert preparado_original == datos["prepared_product_id"]

    # Se reapunta la receta a OTRO preparado despues de crear la orden.
    categoria = await create_category(api, admin_csrf, "Otro preparado 009I")
    otro = await create_product(
        api,
        admin_csrf,
        product_category_id=categoria["id"],
        name="Barniz distinto 009I",
        product_type="PREPARED_MATERIAL",
        base_uom_code="g",
    )
    assert otro.status_code == 201, otro.text
    await db_session.execute(
        Recipe.__table__.update()
        .where(Recipe.__table__.c.id == datos["receta"]["id"])
        .values(product_id=otro.json()["id"])
    )
    await db_session.commit()

    releida = await api.get(f"{ORDERS}/{creada.json()['id']}")
    assert releida.status_code == 200, releida.text
    assert releida.json()["lines"][0]["prepared_product_id"] == preparado_original


# ---------------------------------------------------------------------------
# Sin backfill historico
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_confirmar_una_cotizacion_no_le_crea_orden_de_produccion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """HISTORICAL_PRODUCTION_ORDER_BACKFILL: NONE.

    Confirmar es una decision comercial; fabricar es otra decision, y la toma
    otra persona en otro momento. Que el sistema creara la orden solo porque
    hay una confirmada convertiria cada venta en un compromiso de material
    automatico.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_nobackfill")
    await confirmar(api, admin_csrf, datos["quotation"])

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(ProductionOrder)) == 0
    listado = await api.get(ORDERS)
    assert listado.status_code == 200, listado.text
    assert listado.json()["total"] == 0
