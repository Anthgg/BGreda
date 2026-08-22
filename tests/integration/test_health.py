"""Health checks."""

from __future__ import annotations

import httpx
import pytest

from app.api import health
from app.core.config import Settings
from app.schemas.common import HealthComponent


async def test_live_responde_sin_dependencias(client: httpx.AsyncClient) -> None:
    response = await client.get("/live")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["version"]


async def test_ready_reporta_degradado_sin_base_de_datos(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    componentes = {item["name"]: item["status"] for item in body["components"]}
    assert componentes["database"] == "not_configured"
    assert componentes["supabase_auth"] == "ok"
    assert componentes["security_config"] == "ok"


async def test_ready_responde_ok_con_todas_las_dependencias(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _database_ok(_: Settings) -> HealthComponent:
        return HealthComponent(name="database", status="ok", required=True)

    monkeypatch.setattr(health, "_check_database", _database_ok)

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_ready_no_revela_configuracion_sensible(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")

    cuerpo = response.text
    assert "supabase.co" not in cuerpo
    assert "test-publishable-key" not in cuerpo
    assert "clave-de-pruebas" not in cuerpo
