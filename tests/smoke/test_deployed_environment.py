"""Smoke tests contra un entorno real desplegado.

Estan separados del resto de la suite a proposito: requieren red, un backend
desplegado y un usuario de prueba autorizado. Se omiten salvo que se declaren
las variables correspondientes, de modo que la CI nunca depende de Internet.

Ejecucion::

    SMOKE_BASE_URL=https://... SMOKE_EMAIL=... SMOKE_PASSWORD=... \
        uv run pytest tests/smoke -m smoke

Si no existe un usuario de prueba en Supabase, la parte autenticada se omite
reportando BLOCKED_TEST_USER_REQUIRED. No se crea ningun usuario: esta fase no
habilita registro publico.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.smoke

BASE_URL = os.getenv("SMOKE_BASE_URL", "").rstrip("/")
EMAIL = os.getenv("SMOKE_EMAIL", "")
PASSWORD = os.getenv("SMOKE_PASSWORD", "")

requiere_despliegue = pytest.mark.skipif(
    not BASE_URL,
    reason="SMOKE_BASE_URL no definida: no hay entorno desplegado que probar",
)
requiere_usuario = pytest.mark.skipif(
    not (EMAIL and PASSWORD),
    reason="BLOCKED_TEST_USER_REQUIRED: falta un usuario de prueba autorizado en Supabase",
)


@pytest.fixture
async def client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as http_client:
        yield http_client


@requiere_despliegue
async def test_live(client: httpx.AsyncClient) -> None:
    response = await client.get("/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"


@requiere_despliegue
async def test_ready(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"


@requiere_despliegue
async def test_csrf_emite_cookie_segura(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/csrf")

    assert response.status_code == 200
    cookie = next(
        raw for raw in response.headers.get_list("set-cookie") if raw.startswith("greda_csrf")
    )
    assert "HttpOnly" in cookie
    assert "Secure" in cookie


@requiere_despliegue
@requiere_usuario
async def test_flujo_autenticado_completo(client: httpx.AsyncClient) -> None:
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert login.status_code == 200, login.text
    assert "access_token" not in login.text

    cookies_emitidas = login.headers.get_list("set-cookie")
    assert any("greda_access" in raw and "HttpOnly" in raw for raw in cookies_emitidas)

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True

    token_actual = client.cookies.get("greda_csrf")
    refresh = await client.post(
        "/api/v1/auth/refresh", headers={"X-CSRF-Token": token_actual or token}
    )
    assert refresh.status_code == 200, refresh.text

    token_actual = client.cookies.get("greda_csrf")
    logout = await client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": token_actual or token}
    )
    assert logout.status_code == 200

    assert (await client.get("/api/v1/auth/me")).status_code == 401
