"""Fase 009I — la huella que deja cada transicion, y la identidad de la orden.

Este archivo existe porque el cierre de 009I sustituye el smoke mutante en
produccion por la cobertura contra PostgreSQL real. Esa sustitucion solo vale
si lo que se afirma esta probado, y habia dos cosas afirmadas y no probadas: que
las cuatro transiciones dejan auditoria, y que dos ordenes distintas reciben
correlativo y token distintos.

Escribir el `record_changes` en el servicio no demuestra que la fila llegue a la
base: entre la llamada y el commit hay una transaccion que puede deshacerse.
Aqui se lee `audit_events` despues del commit, que es donde el historial vive
de verdad.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditEvent
from app.models.production import ProductionOrder
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmar,
    crear_orden,
    escenario,
)
from tests.db.test_production_start import _preparada
from tests.db.test_quotation_builder_api import head

#: Tipo de entidad con el que el servicio firma sus eventos.
ENTIDAD = "production_order"


async def _eventos(db_session: AsyncSession, order_id: int) -> list[AuditEvent]:
    db_session.expire_all()
    filas = await db_session.execute(
        select(AuditEvent)
        .where(AuditEvent.entity_type == ENTIDAD, AuditEvent.entity_id == str(order_id))
        .order_by(AuditEvent.id)
    )
    return list(filas.scalars().all())


def _campos(eventos: list[AuditEvent]) -> dict[str, tuple[str | None, str | None]]:
    return {
        evento.field: (evento.old_value, evento.new_value)
        for evento in eventos
        if evento.field is not None
    }


@pytest.mark.asyncio
async def test_crear_la_orden_queda_auditado_con_su_origen(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_CREATE_AUDITED.

    No basta con saber que existe una orden: hace falta saber quien decidio
    fabricar, cuando, desde que cotizacion y contra que almacen. Sin el
    almacen, un descuento posterior en el sitio equivocado no tendria a quien
    preguntarle por que se eligio ese.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_audit_crear")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])

    creada = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert creada.status_code == 201, creada.text

    eventos = await _eventos(db_session, creada.json()["id"])
    altas = [evento for evento in eventos if evento.action is AuditAction.CREATE]
    assert len(altas) == 1, "crear la orden tiene que dejar exactamente un alta"

    alta = altas[0]
    assert alta.user_id is not None, "un alta sin autor no sirve para pedir explicaciones"
    assert alta.user_display_name
    metadatos = alta.event_metadata or {}
    assert metadatos.get("code") == creada.json()["code"]
    assert metadatos.get("quotation_id") == confirmada["id"]
    assert metadatos.get("quotation_code") == confirmada["code"]
    assert metadatos.get("stock_location_id") == datos["location_id"]
    assert metadatos.get("status") == "CREATED"


@pytest.mark.asyncio
async def test_arrancar_queda_auditado_con_el_antes_y_el_despues(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_START_AUDITED.

    Arrancar es la transicion que gasta material, asi que es la que mas falta
    hace poder reconstruir. Se exige el ANTES y el DESPUES del estado, no solo
    que «alguien arranco»: el valor anterior es lo que distingue una transicion
    legitima desde CREATED de cualquier otra cosa, y eso no se puede deducir
    despues mirando la fila, que ya solo dice STARTED.

    Y se exige la huella de lo consumido, que es la que ata el evento con el
    movimiento de inventario.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_audit_start")
    orden_id = datos["orden"]["id"]
    antes = len(await _eventos(db_session, orden_id))

    arrancada = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    eventos = await _eventos(db_session, orden_id)
    assert len(eventos) > antes

    campos = _campos(eventos)
    assert campos["status"] == ("CREATED", "STARTED"), (
        "sin el valor anterior no se distingue de que estado se venia"
    )
    assert campos["started_at"][0] is None
    assert campos["started_at"][1] is not None

    consumos = [
        evento.event_metadata
        for evento in eventos
        if (evento.event_metadata or {}).get("transition") == "START"
    ]
    assert len(consumos) == 1, "la transicion fisica deja su propia huella"
    consumido = (consumos[0] or {}).get("consumed")
    assert consumido, "hay que poder saber QUE se consumio, no solo que se arranco"
    assert consumido[0]["prepared_product_id"] == datos["prepared_product_id"]
    # Como Decimal y nunca como texto: "1000" y "1000.000000000000" son el
    # mismo numero y distinta cadena, y este proyecto ya se quemo una vez
    # comparando importes con ==.
    assert Decimal(consumido[0]["quantity"]) == Decimal("1000")
    assert consumido[0]["uom"] == "g"


@pytest.mark.asyncio
async def test_arrancar_dos_veces_no_duplica_la_auditoria(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La idempotencia tambien alcanza al historial.

    Si el segundo arranque anotara una segunda transicion, la auditoria diria
    que se arranco dos veces y que se consumio dos veces, contradiciendo al
    inventario, que solo tiene un movimiento. Un historial que contradice a los
    hechos es peor que no tenerlo.
    """
    datos = await _preparada(api, admin_csrf, db_session, suffix="_audit_doble")
    orden_id = datos["orden"]["id"]

    primera = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    assert primera.status_code == 200, primera.text
    tras_la_primera = len(await _eventos(db_session, orden_id))

    segunda = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    assert segunda.status_code == 200, segunda.text

    assert len(await _eventos(db_session, orden_id)) == tras_la_primera


@pytest.mark.asyncio
async def test_completar_queda_auditado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_COMPLETE_AUDITED."""
    datos = await _preparada(api, admin_csrf, db_session, suffix="_audit_fin")
    orden_id = datos["orden"]["id"]
    assert (
        await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    ).status_code == 200

    completada = await api.post(f"{ORDERS}/{orden_id}/complete", headers=head(admin_csrf))
    assert completada.status_code == 200, completada.text

    campos = _campos(await _eventos(db_session, orden_id))
    assert campos["status"] == ("STARTED", "COMPLETED")
    assert campos["completed_at"][0] is None
    assert campos["completed_at"][1] is not None


@pytest.mark.asyncio
async def test_anular_queda_auditado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_CANCEL_AUDITED."""
    datos = await _preparada(api, admin_csrf, db_session, suffix="_audit_anul")
    orden_id = datos["orden"]["id"]

    anulada = await api.post(f"{ORDERS}/{orden_id}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text

    campos = _campos(await _eventos(db_session, orden_id))
    assert campos["status"] == ("CREATED", "CANCELLED")
    assert campos["cancelled_at"][0] is None
    assert campos["cancelled_at"][1] is not None


@pytest.mark.asyncio
async def test_un_arranque_bloqueado_no_deja_rastro_de_transicion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Lo que no ocurrio no se audita.

    El arranque se rechaza por falta de material y la transaccion se deshace
    entera. Si el evento sobreviviera al rollback, el historial afirmaria un
    arranque que no existio y que el inventario desmiente.
    """
    datos = await _preparada(
        api, admin_csrf, db_session, suffix="_audit_bloq", existencia_preparado=None
    )
    orden_id = datos["orden"]["id"]
    antes = len(await _eventos(db_session, orden_id))

    respuesta = await api.post(f"{ORDERS}/{orden_id}/start", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text

    eventos = await _eventos(db_session, orden_id)
    assert len(eventos) == antes
    assert "status" not in _campos(eventos)


# ---------------------------------------------------------------------------
# Identidad de la orden: correlativo y token
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dos_ordenes_reciben_correlativo_y_token_distintos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_ORDER_CODE_BACKEND_AUTHORITY / QR_UNIQUE.

    El codigo lo emite el backend con la secuencia documental y avanza; el token
    del QR es opaco y no guarda ninguna relacion con el anterior.

    Lo segundo es lo que importa para la seguridad: si el token fuera
    correlativo, quien tiene el QR de una orden tendria el de todas cambiando un
    digito. Se comprueba que no comparten ni el prefijo largo.
    """
    ordenes = []
    for indice in range(2):
        datos = await escenario(api, admin_csrf, db_session, suffix=f"_ident{indice}")
        confirmada = await confirmar(api, admin_csrf, datos["quotation"])
        creada = await crear_orden(
            api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
        )
        assert creada.status_code == 201, creada.text
        ordenes.append(creada.json())

    primera, segunda = ordenes
    assert primera["code"] != segunda["code"]
    assert primera["code"].startswith("OP-") and segunda["code"].startswith("OP-")

    assert primera["qr_token"] != segunda["qr_token"]
    assert len(primera["qr_token"]) >= 32
    # Dos tokens opacos no se parecen ni en el arranque. Un correlativo
    # disfrazado compartiria casi todo menos el final.
    assert primera["qr_token"][:16] != segunda["qr_token"][:16]

    db_session.expire_all()
    distintos = await db_session.scalar(select(func.count(func.distinct(ProductionOrder.qr_token))))
    total = await db_session.scalar(select(func.count()).select_from(ProductionOrder))
    assert distintos == total == 2


@pytest.mark.asyncio
async def test_el_cliente_no_puede_elegir_el_codigo_ni_el_token(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El correlativo es autoridad del backend y no se acepta por el payload.

    Si el cliente pudiera proponerlo, dos instalaciones numerarian distinto y
    alguien podria reservarse un numero. El esquema rechaza el campo en vez de
    ignorarlo en silencio, que dejaria creer que se aplico.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_nocode")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])

    respuesta = await crear_orden(
        api,
        admin_csrf,
        quotation_id=confirmada["id"],
        location_id=datos["location_id"],
        code="OP-2026-999999",
        qr_token="token-elegido-por-el-cliente-0000000000",
    )

    assert respuesta.status_code == 422, respuesta.text
    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(ProductionOrder)) == 0
