"""Fase 009G — la vigencia se congela al confirmar y ya no se mueve.

Las pruebas del documento (`tests/unit/test_quotation_pdf_validity.py`) fijan
que el PDF lee el snapshot. Aqui se comprueba lo que solo se ve de punta a
punta: que el snapshot se escribe en la transicion real a CONFIRMED, que un
borrador no lo tiene, que un duplicado no lo hereda, y que cambiar la
configuracion despues no lo toca.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head

COMMERCIAL = "/api/v1/settings/commercial"


async def _snapshot(db_session: AsyncSession, quotation_id: int) -> int | None:
    """Lee el snapshot tal y como quedo en la base, sin pasar por el servicio."""
    valor = await db_session.scalar(
        text("SELECT validity_days_snapshot FROM quotations WHERE id = :qid"),
        {"qid": quotation_id},
    )
    return None if valor is None else int(valor)


async def _fijar_vigencia(api: httpx.AsyncClient, csrf: str, dias: int) -> None:
    actual = (await api.get(COMMERCIAL)).json()
    respuesta = await api.put(
        COMMERCIAL,
        json={
            **{k: v for k, v in actual.items() if k != "updated_at"},
            "quote_validity_days": dias,
            "expected_version": actual["version"],
        },
        headers=head(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text


async def _confirmar(api: httpx.AsyncClient, csrf: str, borrador: dict[str, Any]) -> None:
    respuesta = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text


@pytest.mark.asyncio
async def test_confirmar_congela_la_vigencia_vigente(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El contrato central: 20 al confirmar, 20 para siempre."""
    await _fijar_vigencia(api, admin_csrf, 20)
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    borrador = creada.json()

    # Un borrador todavia no ha emitido nada: no tiene vigencia congelada.
    assert await _snapshot(db_session, borrador["id"]) is None

    await _confirmar(api, admin_csrf, borrador)
    assert await _snapshot(db_session, borrador["id"]) == 20


@pytest.mark.asyncio
async def test_cambiar_la_configuracion_no_mueve_una_confirmada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_PDF_VALIDITY_IMMUTABLE, visto desde la base.

    Este era el defecto: el numero vivia en la configuracion, asi que editarla
    reescribia la vigencia de todos los documentos ya entregados.
    """
    await _fijar_vigencia(api, admin_csrf, 20)
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    await _confirmar(api, admin_csrf, creada.json())
    quotation_id = creada.json()["id"]

    await _fijar_vigencia(api, admin_csrf, 30)

    await db_session.commit()
    assert await _snapshot(db_session, quotation_id) == 20


@pytest.mark.asyncio
async def test_una_confirmada_no_admite_ediciones_que_reescriban_su_vigencia(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Guardar sobre una confirmada se rechaza, y el snapshot no se toca.

    La inmutabilidad no depende de que nadie lo intente: se comprueba que el
    intento falla y que ademas no deja rastro.
    """
    await _fijar_vigencia(api, admin_csrf, 20)
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    borrador = creada.json()
    await _confirmar(api, admin_csrf, borrador)

    await _fijar_vigencia(api, admin_csrf, 30)
    rechazada = await api.put(
        f"{BUILDER}/{borrador['id']}",
        json={**payload, "expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    assert rechazada.status_code >= 400, "una confirmada no se edita"

    await db_session.commit()
    assert await _snapshot(db_session, borrador["id"]) == 20


@pytest.mark.asyncio
async def test_el_duplicado_nace_sin_vigencia_congelada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Duplicar produce un borrador, y un borrador no ha emitido nada.

    Heredar el snapshot le daria a una cotizacion nueva la vigencia de otra,
    fechada en un dia distinto y quiza con otra politica.
    """
    await _fijar_vigencia(api, admin_csrf, 20)
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    original = creada.json()
    await _confirmar(api, admin_csrf, original)

    copia = await api.post(f"{BUILDER}/{original['id']}/duplicate", headers=head(admin_csrf))
    assert copia.status_code == 200, copia.text

    await db_session.commit()
    assert await _snapshot(db_session, copia.json()["id"]) is None
    assert await _snapshot(db_session, original["id"]) == 20


@pytest.mark.asyncio
async def test_sin_vigencia_configurada_la_confirmada_queda_en_nulo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Si el ajuste esta vacio no se inventa un plazo por omision.

    Un valor supuesto aqui seria indistinguible de uno acordado, que es
    exactamente lo que 009G prohibe.
    """
    await _fijar_vigencia(api, admin_csrf, None)  # type: ignore[arg-type]
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    await _confirmar(api, admin_csrf, creada.json())

    await db_session.commit()
    assert await _snapshot(db_session, creada.json()["id"]) is None
