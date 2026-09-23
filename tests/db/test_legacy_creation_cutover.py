"""Fase 010J — el corte de la creacion Legacy, contra PostgreSQL.

Con `LEGACY_QUOTATION_CREATION_ENABLED` apagado (el valor por defecto):

1. crear una cotizacion Legacy por las rutas generales se rechaza con un error
   de dominio, y no se crea nada;
2. lo historico se sigue leyendo y su PDF se sigue emitiendo;
3. la cotizacion final de una muestra aprobada SIGUE funcionando: es la
   excepcion temporal aprobada para 010J, porque las muestras aun no tienen
   camino V2;
4. y esa excepcion no es una puerta: sin una muestra real, aprobada y vigente,
   no crea ninguna Legacy;
5. el Cotizador V2 sigue creando.

Las demas suites encienden el interruptor (`tests/conftest.py`) porque prueban
lo Legacy existente; aqui se apaga a proposito.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.quotations import Quotation
from tests.db.test_prototype_quotation_bridge import _aprobada, _final
from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head
from tests.db.test_quotations_api import QUOTATIONS

CODIGO = "LEGACY_QUOTATION_CREATION_DISABLED"


@pytest.fixture
def cortado(api_app: FastAPI) -> Iterator[None]:
    """El servicio tal como queda en produccion tras el corte."""
    ajustes = get_settings().model_copy(update={"LEGACY_QUOTATION_CREATION_ENABLED": False})
    api_app.dependency_overrides[get_settings] = lambda: ajustes
    yield
    api_app.dependency_overrides.pop(get_settings, None)


async def _legacy(db: AsyncSession) -> int:
    db.expire_all()
    return int(await db.scalar(select(func.count()).select_from(Quotation)) or 0)


async def _historica_confirmada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Una Legacy creada y confirmada ANTES del corte, como las 529 de produccion."""
    payload, _ = await _complete_payload(api, csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(
        f"{BUILDER}/{creada.json()['id']}/confirm",
        json={"expected_updated_at": creada.json()["updated_at"]},
        headers=head(csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    return dict(confirmada.json()), payload


def test_el_interruptor_esta_apagado_por_defecto() -> None:
    """Sin configurar nada, la creacion Legacy queda cortada."""
    from app.core.config import Settings

    campo = Settings.model_fields["LEGACY_QUOTATION_CREATION_ENABLED"]
    assert campo.default is False


@pytest.mark.asyncio
async def test_las_rutas_generales_de_creacion_legacy_quedan_cortadas(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    api_app: FastAPI,
) -> None:
    historica, payload = await _historica_confirmada(api, admin_csrf, db_session)
    ajustes = get_settings().model_copy(update={"LEGACY_QUOTATION_CREATION_ENABLED": False})
    api_app.dependency_overrides[get_settings] = lambda: ajustes
    try:
        antes = await _legacy(db_session)

        nueva = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
        duplicada = await api.post(
            f"{BUILDER}/{historica['id']}/duplicate", headers=head(admin_csrf)
        )
        clasica = await api.post(
            QUOTATIONS, json={"name": "Legacy tras el corte"}, headers=head(admin_csrf)
        )

        for respuesta in (nueva, duplicada, clasica):
            assert respuesta.status_code == 409, respuesta.text
            assert respuesta.json()["error"]["code"] == CODIGO
        assert await _legacy(db_session) == antes
    finally:
        api_app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_lo_historico_se_lee_y_su_pdf_se_emite_tras_el_corte(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    api_app: FastAPI,
) -> None:
    historica, _ = await _historica_confirmada(api, admin_csrf, db_session)
    ajustes = get_settings().model_copy(update={"LEGACY_QUOTATION_CREATION_ENABLED": False})
    api_app.dependency_overrides[get_settings] = lambda: ajustes
    try:
        leida = await api.get(f"{BUILDER}/{historica['id']}")
        pdf = await api.get(f"{QUOTATIONS}/{historica['id']}/pdf")

        assert leida.status_code == 200, leida.text
        # Congelada: lo que se lee es exactamente lo que se confirmo.
        assert leida.json() == historica
        assert pdf.status_code == 200, pdf.text
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content[:5] == b"%PDF-"
    finally:
        api_app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_la_excepcion_de_muestras_sigue_creando_su_cotizacion_final(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    api_app: FastAPI,
) -> None:
    """La excepcion temporal aprobada: una muestra aprobada y vigente cotiza."""
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_corte_ok")
    ajustes = get_settings().model_copy(update={"LEGACY_QUOTATION_CREATION_ENABLED": False})
    api_app.dependency_overrides[get_settings] = lambda: ajustes
    try:
        respuesta = await api.post(_final(datos["prototipo"]["id"]), headers=head(admin_csrf))

        assert respuesta.status_code == 201, respuesta.text
        fila = await db_session.get(Quotation, respuesta.json()["id"])
        assert fila is not None
        assert fila.origin_prototype_id == datos["prototipo"]["id"]
    finally:
        api_app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_la_excepcion_no_es_una_puerta_para_crear_legacy_arbitrarias(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    api_app: FastAPI,
) -> None:
    """Sin una muestra real, aprobada y vigente —o sin ser ADMIN— no se crea nada."""
    from tests.db.test_prototype_quotation_bridge import _muestra_lista

    pendiente = await _muestra_lista(api, admin_csrf, db_session, suffix="_corte_pendiente")
    ajustes = get_settings().model_copy(update={"LEGACY_QUOTATION_CREATION_ENABLED": False})
    api_app.dependency_overrides[get_settings] = lambda: ajustes
    try:
        antes = await _legacy(db_session)

        inexistente = await api.post(_final(999_999), headers=head(admin_csrf))
        sin_aprobar = await api.post(_final(pendiente["prototipo"]["id"]), headers=head(admin_csrf))

        assert inexistente.status_code == 404, inexistente.text
        assert sin_aprobar.status_code == 409, sin_aprobar.text
        assert sin_aprobar.json()["error"]["code"] == "PROTOTYPE_NOT_APPROVED_FOR_QUOTATION"
        assert await _legacy(db_session) == antes
    finally:
        api_app.dependency_overrides.pop(get_settings, None)

    # RBAC intacto: el operario no cotiza desde una muestra (ADMIN, como antes).
    aprobada = await _aprobada(api, admin_csrf, db_session, suffix="_corte_rbac")
    api_app.dependency_overrides[get_settings] = lambda: ajustes
    try:
        from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

        operario = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        prohibido = await api.post(_final(aprobada["prototipo"]["id"]), headers=head(operario))
        assert prohibido.status_code == 403, prohibido.text
    finally:
        api_app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_el_cotizador_v2_sigue_creando_tras_el_corte(
    api: httpx.AsyncClient,
    admin_csrf: str,
    cortado: None,
) -> None:
    creada = await api.post(
        "/api/v1/quotations-v2", json={"name": "V2 tras el corte"}, headers=head(admin_csrf)
    )
    assert creada.status_code == 201, creada.text
