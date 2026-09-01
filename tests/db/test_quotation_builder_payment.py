"""Fase 009H — el eje de cobro, separado del estado comercial.

Cobrar no es un cuarto estado. Una anulada puede estar pagada y una confirmada
puede seguir sin cobrarse, asi que el pago vive en su propio eje y ninguno de
los dos pisa al otro.

Lo que se fija aqui: desde donde se puede marcar, que la fecha de cobro no se
mueve nunca despues, que anular no borra el pago, que duplicar no lo hereda, y
que registrar un cobro no toca ni un precio ni un gramo de inventario.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head


async def _crear(api: httpx.AsyncClient, csrf: str, payload: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(BUILDER, json=payload, headers=head(csrf))
    assert respuesta.status_code == 201, respuesta.text
    return dict(respuesta.json())


async def _confirmar(api: httpx.AsyncClient, csrf: str, borrador: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text
    return dict(respuesta.json())


async def _marcar_pagada(api: httpx.AsyncClient, csrf: str, quotation_id: int) -> httpx.Response:
    return await api.post(f"{BUILDER}/{quotation_id}/mark-paid", headers=head(csrf))


async def _volver_legacy(db_session: AsyncSession, quotation_id: int) -> None:
    """Deja la fila como una anterior a 009H: sin registro de pago.

    Se hace por SQL porque el flujo nuevo ya no produce ese estado, y es
    precisamente el que hay en las 18 confirmadas de produccion.
    """
    await db_session.execute(
        text("UPDATE quotations SET payment_status = NULL, paid_at = NULL WHERE id = :qid"),
        {"qid": quotation_id},
    )
    await db_session.commit()


async def _pago_en_base(db_session: AsyncSession, quotation_id: int) -> tuple[str | None, Any]:
    fila = (
        await db_session.execute(
            text("SELECT payment_status, paid_at FROM quotations WHERE id = :qid"),
            {"qid": quotation_id},
        )
    ).one()
    return fila[0], fila[1]


# ---------------------------------------------------------------------------
# Desde donde se puede cobrar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_confirmada_se_puede_marcar_pagada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_CAN_BE_MARKED_PAID."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    assert borrador["payment_status"] == "UNPAID", "una cotizacion nueva nace impaga"
    assert borrador["paid_at"] is None

    confirmada = await _confirmar(api, admin_csrf, borrador)
    assert confirmada["payment_status"] == "UNPAID"

    respuesta = await _marcar_pagada(api, admin_csrf, borrador["id"])
    assert respuesta.status_code == 200, respuesta.text
    pagada = respuesta.json()

    assert pagada["status"] == "CONFIRMED", "cobrar no cambia el estado comercial"
    assert pagada["payment_status"] == "PAID"
    assert pagada["paid_at"] is not None


@pytest.mark.asyncio
async def test_un_borrador_no_se_puede_marcar_pagada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DRAFT_CANNOT_BE_MARKED_PAID.

    Un borrador todavia no es un compromiso: cobrarlo seria registrar el pago
    de algo que nadie ha aceptado.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)

    respuesta = await _marcar_pagada(api, admin_csrf, borrador["id"])
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "QUOTATION_BUILDER_NOT_PAYABLE"

    estado, fecha = await _pago_en_base(db_session, borrador["id"])
    assert estado == "UNPAID"
    assert fecha is None


@pytest.mark.asyncio
async def test_una_anulada_no_se_puede_marcar_pagada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCELLED_CANNOT_BE_MARKED_PAID."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    anulada = await api.post(f"{BUILDER}/{borrador['id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text

    respuesta = await _marcar_pagada(api, admin_csrf, borrador["id"])
    assert respuesta.status_code == 409, respuesta.text

    _, fecha = await _pago_en_base(db_session, borrador["id"])
    assert fecha is None, "una anulada no puede acabar con fecha de cobro"


@pytest.mark.asyncio
async def test_una_confirmada_sin_registro_de_pago_si_se_puede_marcar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LEGACY_CONFIRMED_NULL_CAN_BE_MARKED_PAID.

    Es el caso de las 18 confirmadas que ya existen en produccion. Que el
    sistema no supiera si se cobraron no puede impedir registrarlo ahora.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    await _confirmar(api, admin_csrf, borrador)
    await _volver_legacy(db_session, borrador["id"])

    reabierta = await api.get(f"{BUILDER}/{borrador['id']}")
    assert reabierta.json()["payment_status"] is None

    respuesta = await _marcar_pagada(api, admin_csrf, borrador["id"])
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["payment_status"] == "PAID"


@pytest.mark.asyncio
async def test_confirmar_normaliza_un_borrador_sin_registro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRM_NORMALIZES_LEGACY_PAYMENT_TO_UNPAID.

    Un borrador anterior a 009H entra al flujo nuevo al confirmarse, y desde
    ese momento si se sabe que no se ha cobrado.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    await _volver_legacy(db_session, borrador["id"])

    vuelta = await api.get(f"{BUILDER}/{borrador['id']}")
    confirmada = await _confirmar(api, admin_csrf, vuelta.json())

    assert confirmada["payment_status"] == "UNPAID"
    assert confirmada["paid_at"] is None


# ---------------------------------------------------------------------------
# La fecha de cobro no se mueve
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_marcar_pagada_dos_veces_no_mueve_la_fecha(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DOUBLE_MARK_PAID_NO_TIMESTAMP_CHANGE y DOUBLE_MARK_PAID_NO_DUPLICATE_AUDIT.

    Una fecha de cobro que se desplaza cada vez que alguien pulsa el boton deja
    de ser la fecha en que se cobro.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    await _confirmar(api, admin_csrf, borrador)

    primera = await _marcar_pagada(api, admin_csrf, borrador["id"])
    assert primera.status_code == 200, primera.text
    fecha_original = primera.json()["paid_at"]

    eventos_antes = await db_session.scalar(
        text(
            "SELECT count(*) FROM audit_events "
            "WHERE entity_type = 'quotation_builder' AND entity_id = :qid "
            "AND field = 'payment_status'"
        ),
        {"qid": str(borrador["id"])},
    )

    segunda = await _marcar_pagada(api, admin_csrf, borrador["id"])
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["paid_at"] == fecha_original

    await db_session.commit()
    eventos_despues = await db_session.scalar(
        text(
            "SELECT count(*) FROM audit_events "
            "WHERE entity_type = 'quotation_builder' AND entity_id = :qid "
            "AND field = 'payment_status'"
        ),
        {"qid": str(borrador["id"])},
    )
    assert eventos_despues == eventos_antes, "el segundo cobro no es un hecho nuevo"


@pytest.mark.asyncio
async def test_el_cobro_queda_auditado_con_el_antes_y_el_despues(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PAYMENT_AUDIT_HAS_BEFORE_AFTER.

    El valor anterior distingue «se cobro una que sabiamos impaga» de «se cobro
    una de la que no habia registro», y esa diferencia no se puede reconstruir
    despues.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    await _confirmar(api, admin_csrf, borrador)
    await _marcar_pagada(api, admin_csrf, borrador["id"])
    await db_session.commit()

    evento = (
        await db_session.execute(
            text(
                "SELECT field, old_value, new_value, user_display_name, created_at "
                "FROM audit_events WHERE entity_type = 'quotation_builder' "
                "AND entity_id = :qid AND field = 'payment_status'"
            ),
            {"qid": str(borrador["id"])},
        )
    ).one()
    assert evento[1] == "UNPAID"
    assert evento[2] == "PAID"
    assert evento[3], "el evento tiene que decir quien"
    assert evento[4], "y cuando"


# ---------------------------------------------------------------------------
# Lo que el cobro NO toca
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cobrar_no_cambia_ni_un_precio_ni_una_fecha_del_documento(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MARK_PAID_DOES_NOT_CHANGE_PRICING / _DAYS / _FIRING / _PRESERVES_CONFIRMED_AT."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    antes = await _confirmar(api, admin_csrf, borrador)

    await _marcar_pagada(api, admin_csrf, borrador["id"])
    despues = (await api.get(f"{BUILDER}/{borrador['id']}")).json()

    assert despues["confirmed_at"] == antes["confirmed_at"]
    assert despues["source_fingerprint"] == antes["source_fingerprint"], (
        "el pago ocurre despues de congelar: no puede mover la huella"
    )
    for campo in ("commercial_subtotal", "tax_amount", "total_with_tax"):
        assert Decimal(despues[campo]) == Decimal(antes[campo]), campo
    for indice, linea in enumerate(despues["items"]):
        previa = antes["items"][indice]
        assert linea["total_days"] == previa["total_days"]
        assert Decimal(linea["firing_cost"]) == Decimal(previa["firing_cost"])
        assert Decimal(linea["commercial_total"]) == Decimal(previa["commercial_total"])


@pytest.mark.asyncio
async def test_el_flujo_de_pago_no_mueve_el_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PAYMENT_FLOW_NO_STOCK_MOVEMENT.

    Cobrar es un hecho comercial. El material sale cuando se produce, no cuando
    se cobra, y eso pertenece a otra fase.
    """

    async def _inventario() -> tuple[int, int, int]:
        return (
            int(await db_session.scalar(text("SELECT count(*) FROM stock_movements")) or 0),
            int(await db_session.scalar(text("SELECT count(*) FROM stock_balances")) or 0),
            int(await db_session.scalar(text("SELECT count(*) FROM recipe_preparations")) or 0),
        )

    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    antes = await _inventario()

    await _confirmar(api, admin_csrf, borrador)
    await _marcar_pagada(api, admin_csrf, borrador["id"])
    await _marcar_pagada(api, admin_csrf, borrador["id"])
    await api.post(f"{BUILDER}/{borrador['id']}/cancel", headers=head(admin_csrf))

    await db_session.commit()
    assert await _inventario() == antes


# ---------------------------------------------------------------------------
# Anular y duplicar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_anular_una_pagada_no_borra_el_cobro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CANCEL_PAID_PRESERVES_PAYMENT_HISTORY.

    Anular es una decision comercial posterior; el dinero se cobro igual.
    Borrar el pago al anular haria desaparecer un hecho ocurrido.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    await _confirmar(api, admin_csrf, borrador)
    pagada = (await _marcar_pagada(api, admin_csrf, borrador["id"])).json()

    anulada = await api.post(f"{BUILDER}/{borrador['id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text
    cuerpo = anulada.json()

    assert cuerpo["status"] == "CANCELLED"
    assert cuerpo["payment_status"] == "PAID"
    assert cuerpo["paid_at"] == pagada["paid_at"]


@pytest.mark.asyncio
async def test_duplicar_no_hereda_el_cobro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DUPLICATE_RESETS_PAYMENT.

    La copia es una cotizacion nueva que nadie ha pagado. Heredar el cobro le
    daria por saldado un dinero que nunca entro.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, payload)
    await _confirmar(api, admin_csrf, borrador)
    await _marcar_pagada(api, admin_csrf, borrador["id"])

    copia = await api.post(f"{BUILDER}/{borrador['id']}/duplicate", headers=head(admin_csrf))
    assert copia.status_code == 200, copia.text
    nueva = copia.json()

    assert nueva["status"] == "DRAFT"
    assert nueva["payment_status"] == "UNPAID"
    assert nueva["paid_at"] is None
    # Y el original conserva el suyo.
    original = (await api.get(f"{BUILDER}/{borrador['id']}")).json()
    assert original["payment_status"] == "PAID"
