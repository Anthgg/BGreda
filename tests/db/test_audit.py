"""Auditoria de los cambios de configuracion."""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent

AUDIT = "/api/v1/settings/audit"
COMPANY = "/api/v1/settings/company"
COMMERCIAL = "/api/v1/settings/commercial"
LOGO = "/api/v1/settings/company/logo"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ---------------------------------------------------------------------------
# Registro de cambios
# ---------------------------------------------------------------------------
async def test_cambiar_un_campo_deja_historial(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    await api.put(
        COMPANY,
        json={"version": 1, "legal_name": "Taller Greda SAC"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    eventos = list((await db_session.execute(select(AuditEvent))).scalars().all())

    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.entity_type == "company_settings"
    assert evento.field == "legal_name"
    assert evento.old_value is None
    assert evento.new_value == "Taller Greda SAC"
    assert evento.action == "UPDATE"


async def test_el_historial_registra_quien_hizo_el_cambio(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    from tests.conftest import TEST_USER_ID

    await api.put(
        COMPANY, json={"version": 1, "phone": "987654321"}, headers={"X-CSRF-Token": admin_csrf}
    )

    evento = (await db_session.execute(select(AuditEvent))).scalars().one()

    assert evento.user_id == TEST_USER_ID
    assert evento.user_display_name == "Administrador"
    assert evento.created_at is not None


async def test_se_registra_el_valor_anterior_y_el_nuevo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    await api.put(
        COMPANY, json={"version": 1, "legal_name": "Primero"}, headers={"X-CSRF-Token": admin_csrf}
    )
    await api.put(
        COMPANY, json={"version": 2, "legal_name": "Segundo"}, headers={"X-CSRF-Token": admin_csrf}
    )

    eventos = list(
        (await db_session.execute(select(AuditEvent).order_by(AuditEvent.id))).scalars().all()
    )

    assert [(e.old_value, e.new_value) for e in eventos] == [
        (None, "Primero"),
        ("Primero", "Segundo"),
    ]


async def test_cada_campo_genera_su_propio_evento(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    await api.put(
        COMPANY,
        json={
            "version": 1,
            "legal_name": "Greda",
            "phone": "987654321",
            "ubigeo_code": "150122",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )

    campos = {e.field for e in (await db_session.execute(select(AuditEvent))).scalars().all()}

    assert campos == {
        "legal_name",
        "phone",
        "ubigeo_code",
        "district",
        "province",
        "department",
        "country",
    }


async def test_guardar_sin_cambios_no_genera_historial(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    await api.put(COMPANY, json={"version": 1}, headers={"X-CSRF-Token": admin_csrf})

    eventos = list((await db_session.execute(select(AuditEvent))).scalars().all())

    assert eventos == []


async def test_un_intento_rechazado_no_deja_historial(
    api: httpx.AsyncClient, operator_csrf: str, db_session: AsyncSession
) -> None:
    await api.put(
        COMPANY,
        json={"version": 1, "legal_name": "Intento"},
        headers={"X-CSRF-Token": operator_csrf},
    )

    assert list((await db_session.execute(select(AuditEvent))).scalars().all()) == []


async def test_el_igv_se_audita_con_su_valor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    await api.put(
        COMMERCIAL, json={"version": 1, "tax_percent": 18}, headers={"X-CSRF-Token": admin_csrf}
    )

    evento = (
        (await db_session.execute(select(AuditEvent).where(AuditEvent.field == "tax_percent")))
        .scalars()
        .one()
    )

    assert evento.new_value == "18"


async def test_el_logo_se_audita_sin_guardar_el_binario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    await api.post(
        LOGO,
        files={"file": ("logo.png", PNG, "image/png")},
        headers={"X-CSRF-Token": admin_csrf},
    )

    evento = (await db_session.execute(select(AuditEvent))).scalars().one()

    assert evento.entity_type == "company_settings"
    assert evento.event_metadata is not None
    assert evento.event_metadata["field"] == "logo"
    assert evento.event_metadata["content_type"] == "image/png"
    # El contenido del archivo no aparece por ningun lado.
    assert "PNG" not in str(evento.event_metadata)
    assert evento.old_value is None and evento.new_value is None


async def test_el_historial_no_contiene_secretos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    await api.put(
        COMPANY,
        json={"version": 1, "legal_name": "Greda", "email": "admin@empresa.com"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    volcado = " ".join(
        f"{e.field} {e.old_value} {e.new_value}"
        for e in (await db_session.execute(select(AuditEvent))).scalars().all()
    )

    for prohibido in ("password", "token", "sb_secret", "postgresql://", "cookie"):
        assert prohibido not in volcado.lower()


# ---------------------------------------------------------------------------
# Endpoint de consulta
# ---------------------------------------------------------------------------
async def test_admin_consulta_el_historial(api: httpx.AsyncClient, admin_csrf: str) -> None:
    await api.put(
        COMPANY, json={"version": 1, "legal_name": "Greda"}, headers={"X-CSRF-Token": admin_csrf}
    )

    response = await api.get(AUDIT)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["field"] == "legal_name"


async def test_operator_no_accede_al_historial(api: httpx.AsyncClient, operator_csrf: str) -> None:
    assert (await api.get(AUDIT)).status_code == 403


async def test_sin_sesion_no_hay_historial(api: httpx.AsyncClient) -> None:
    assert (await api.get(AUDIT)).status_code == 401


async def test_el_historial_se_puede_filtrar_por_entidad(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    await api.put(
        COMPANY, json={"version": 1, "legal_name": "Greda"}, headers={"X-CSRF-Token": admin_csrf}
    )
    await api.put(
        COMMERCIAL, json={"version": 1, "tax_percent": 18}, headers={"X-CSRF-Token": admin_csrf}
    )

    body = (await api.get(AUDIT, params={"entity_type": "commercial_settings"})).json()

    assert body["total"] == 1
    assert body["items"][0]["field"] == "tax_percent"


async def test_el_historial_llega_ordenado_del_mas_reciente(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    await api.put(
        COMPANY, json={"version": 1, "legal_name": "Primero"}, headers={"X-CSRF-Token": admin_csrf}
    )
    await api.put(
        COMPANY, json={"version": 2, "legal_name": "Segundo"}, headers={"X-CSRF-Token": admin_csrf}
    )

    items = (await api.get(AUDIT)).json()["items"]

    assert items[0]["new_value"] == "Segundo"
