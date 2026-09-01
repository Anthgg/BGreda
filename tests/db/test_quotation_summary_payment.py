"""Fase 009H — el listado dice si una cotizacion se cobro.

Sin este campo el listado solo podria deducir el pago del estado comercial
—confirmada, luego impaga— que es exactamente lo que 009H separo: son dos
hechos distintos y uno no implica el otro. Una confirmada puede seguir sin
cobrarse y una anulada puede estar pagada.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head

QUOTATIONS = "/api/v1/quotations"


async def _fila(api: httpx.AsyncClient, quotation_id: int) -> dict[str, Any]:
    """La cotizacion tal y como la ve el LISTADO, no el detalle."""
    respuesta = await api.get(f"{QUOTATIONS}?limit=100")
    assert respuesta.status_code == 200, respuesta.text
    fila = next((item for item in respuesta.json()["items"] if item["id"] == quotation_id), None)
    assert fila is not None, f"la cotizacion {quotation_id} no aparece en el listado"
    return dict(fila)


async def _preparar(api: httpx.AsyncClient, csrf: str, db_session: AsyncSession) -> dict[str, Any]:
    payload, _ = await _complete_payload(api, csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(csrf))
    assert creada.status_code == 201, creada.text
    return dict(creada.json())


@pytest.mark.asyncio
async def test_el_listado_muestra_una_cotizacion_nueva_como_impaga(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """SUMMARY_EXPOSES_PAYMENT_STATUS."""
    borrador = await _preparar(api, admin_csrf, db_session)
    fila = await _fila(api, borrador["id"])
    assert fila["payment_status"] == "UNPAID"


@pytest.mark.asyncio
async def test_el_listado_conserva_el_hueco_de_las_historicas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LIST_NULL_PAYMENT_PRESERVES_LEGACY_SEMANTICS.

    Nulo tiene que llegar nulo hasta la pantalla. Si el backend lo convirtiera
    a UNPAID por comodidad, el listado afirmaria que 347 cotizaciones no se
    cobraron cuando de casi todas no se sabe nada.
    """
    borrador = await _preparar(api, admin_csrf, db_session)
    await db_session.execute(
        text("UPDATE quotations SET payment_status = NULL, paid_at = NULL WHERE id = :qid"),
        {"qid": borrador["id"]},
    )
    await db_session.commit()

    fila = await _fila(api, borrador["id"])
    assert fila["payment_status"] is None, "el hueco historico no se rellena en el camino"


@pytest.mark.asyncio
async def test_el_listado_muestra_una_pagada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    borrador = await _preparar(api, admin_csrf, db_session)
    confirmada = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    pagada = await api.post(f"{BUILDER}/{borrador['id']}/mark-paid", headers=head(admin_csrf))
    assert pagada.status_code == 200, pagada.text

    fila = await _fila(api, borrador["id"])
    assert fila["status"] == "CONFIRMED"
    assert fila["payment_status"] == "PAID"


@pytest.mark.asyncio
async def test_el_listado_muestra_los_dos_ejes_de_una_anulada_pagada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LIST_CANCELLED_PAID_SHOWS_BOTH_AXES.

    El caso que justifica que el pago sea un eje aparte: el dinero entro y
    despues se anulo. Si el pago fuera un cuarto estado, uno de los dos hechos
    tendria que desaparecer.
    """
    borrador = await _preparar(api, admin_csrf, db_session)
    await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    await api.post(f"{BUILDER}/{borrador['id']}/mark-paid", headers=head(admin_csrf))
    anulada = await api.post(f"{BUILDER}/{borrador['id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text

    fila = await _fila(api, borrador["id"])
    assert fila["status"] == "CANCELLED"
    assert fila["payment_status"] == "PAID"


@pytest.mark.asyncio
async def test_el_estado_comercial_del_listado_no_cambia_con_el_pago(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTATION_STATUS_UNCHANGED, visto desde el listado."""
    borrador = await _preparar(api, admin_csrf, db_session)
    await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    antes = await _fila(api, borrador["id"])
    await api.post(f"{BUILDER}/{borrador['id']}/mark-paid", headers=head(admin_csrf))
    despues = await _fila(api, borrador["id"])

    assert antes["status"] == despues["status"] == "CONFIRMED"
    assert antes["total"] == despues["total"], "cobrar no mueve el importe del listado"
