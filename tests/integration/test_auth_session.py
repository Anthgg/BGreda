"""Sesion: /auth/me, refresh y logout."""

from __future__ import annotations

import httpx

from app.auth.cookies import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME
from tests.conftest import (
    TEST_EMAIL,
    TEST_USER_ID,
    csrf_headers,
    current_csrf_token,
    login,
    obtain_csrf_token,
)
from tests.fakes import FakeProfileRepository, FakeSupabaseAuthClient


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------
async def test_me_sin_sesion_devuelve_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_NOT_AUTHENTICATED"


async def test_me_con_sesion_devuelve_el_usuario(client: httpx.AsyncClient) -> None:
    await login(client)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["user"]["id"] == str(TEST_USER_ID)
    assert body["user"]["email"] == TEST_EMAIL
    assert body["user"]["role"] == "ADMIN"


async def test_una_cookie_cualquiera_no_autentica(client: httpx.AsyncClient) -> None:
    """La sola presencia de la cookie no es evidencia de sesion."""
    response = await client.get(
        "/api/v1/auth/me",
        headers={"Cookie": f"{ACCESS_COOKIE_NAME}=token-inventado"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_SESSION_EXPIRED"


async def test_me_rechaza_al_usuario_desactivado_despues_del_login(
    client: httpx.AsyncClient,
    profiles: FakeProfileRepository,
) -> None:
    await login(client)
    profiles.profiles[TEST_USER_ID].active = False

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_ACCOUNT_INACTIVE"


# ---------------------------------------------------------------------------
# /auth/refresh
# ---------------------------------------------------------------------------
async def test_refresh_renueva_las_cookies(client: httpx.AsyncClient) -> None:
    await login(client)
    access_previo = client.cookies.get(ACCESS_COOKIE_NAME)
    refresh_previo = client.cookies.get(REFRESH_COOKIE_NAME)

    response = await client.post(
        "/api/v1/auth/refresh",
        headers=csrf_headers(await current_csrf_token(client)),
    )

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert client.cookies.get(ACCESS_COOKIE_NAME) != access_previo
    assert client.cookies.get(REFRESH_COOKIE_NAME) != refresh_previo
    # El refresh token sigue sin salir del backend.
    assert "refresh_token" not in response.text


async def test_refresh_sin_cookie_devuelve_401(client: httpx.AsyncClient) -> None:
    token = await obtain_csrf_token(client)

    response = await client.post("/api/v1/auth/refresh", headers=csrf_headers(token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_NOT_AUTHENTICATED"


async def test_refresh_invalido_limpia_las_cookies(
    client: httpx.AsyncClient,
    supabase: FakeSupabaseAuthClient,
) -> None:
    await login(client)
    supabase.revoke_all()

    response = await client.post(
        "/api/v1/auth/refresh",
        headers=csrf_headers(await current_csrf_token(client)),
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_SESSION_EXPIRED"
    # Las cookies se borran para que el cliente no reintente en bucle.
    assert ACCESS_COOKIE_NAME not in client.cookies
    assert REFRESH_COOKIE_NAME not in client.cookies


# ---------------------------------------------------------------------------
# /auth/logout
# ---------------------------------------------------------------------------
async def test_logout_borra_las_cookies_y_revoca_en_supabase(
    client: httpx.AsyncClient,
    supabase: FakeSupabaseAuthClient,
) -> None:
    await login(client)

    response = await client.post(
        "/api/v1/auth/logout",
        headers=csrf_headers(await current_csrf_token(client)),
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert supabase.sign_out_calls, "se debe intentar revocar la sesion en Supabase"
    assert ACCESS_COOKIE_NAME not in client.cookies
    assert REFRESH_COOKIE_NAME not in client.cookies


async def test_despues_del_logout_me_deja_de_autenticar(client: httpx.AsyncClient) -> None:
    await login(client)
    await client.post(
        "/api/v1/auth/logout",
        headers=csrf_headers(await current_csrf_token(client)),
    )

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_logout_sin_sesion_no_falla(client: httpx.AsyncClient) -> None:
    token = await obtain_csrf_token(client)

    response = await client.post("/api/v1/auth/logout", headers=csrf_headers(token))

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
