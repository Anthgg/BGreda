"""Flujo de login."""

from __future__ import annotations

import httpx

from app.auth.cookies import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME
from tests.conftest import (
    TEST_EMAIL,
    TEST_PASSWORD,
    TEST_USER_ID,
    csrf_headers,
    login,
    obtain_csrf_token,
)
from tests.fakes import FakeProfileRepository


def _set_cookie_headers(response: httpx.Response) -> dict[str, str]:
    """Indexa las cabeceras Set-Cookie por nombre de cookie."""
    return {raw.split("=", 1)[0].strip(): raw for raw in response.headers.get_list("set-cookie")}


async def test_login_valido_devuelve_usuario_y_abre_sesion(client: httpx.AsyncClient) -> None:
    response = await login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["user"] == {
        "id": str(TEST_USER_ID),
        "email": TEST_EMAIL,
        "display_name": "Administrador",
        "role": "ADMIN",
    }


async def test_login_nunca_expone_tokens_al_javascript(client: httpx.AsyncClient) -> None:
    response = await login(client)

    # Ningun token aparece en el cuerpo de la respuesta.
    assert "access_token" not in response.text
    assert "refresh_token" not in response.text

    cookies = _set_cookie_headers(response)
    assert ACCESS_COOKIE_NAME in cookies
    assert REFRESH_COOKIE_NAME in cookies
    for name in (ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME):
        atributos = cookies[name].lower()
        assert "httponly" in atributos
        assert "path=/" in atributos
        assert "samesite=lax" in atributos


async def test_login_con_password_incorrecta_falla(client: httpx.AsyncClient) -> None:
    response = await login(client, password="password-equivocada")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
    assert ACCESS_COOKIE_NAME not in client.cookies


async def test_login_con_email_desconocido_no_permite_enumerar_cuentas(
    client: httpx.AsyncClient,
) -> None:
    desconocido = await login(client, email="nadie@empresa.com", password="lo-que-sea")
    invalido = await login(client, password="password-equivocada")

    assert desconocido.status_code == invalido.status_code == 401
    assert desconocido.json() == invalido.json()


async def test_login_sin_perfil_aprovisionado_es_rechazado(
    client: httpx.AsyncClient,
    profiles: FakeProfileRepository,
) -> None:
    # El usuario existe en Supabase pero no fue habilitado en la aplicacion.
    profiles.profiles.clear()

    response = await login(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_PROFILE_NOT_PROVISIONED"
    # No se abre sesion alguna.
    assert ACCESS_COOKIE_NAME not in client.cookies
    assert REFRESH_COOKIE_NAME not in client.cookies


async def test_login_con_perfil_inactivo_es_rechazado(
    client: httpx.AsyncClient,
    profiles: FakeProfileRepository,
) -> None:
    profiles.profiles[TEST_USER_ID].active = False

    response = await login(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_ACCOUNT_INACTIVE"
    assert ACCESS_COOKIE_NAME not in client.cookies


async def test_login_con_email_invalido_devuelve_error_de_validacion(
    client: httpx.AsyncClient,
) -> None:
    token = await obtain_csrf_token(client)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "no-es-un-email", "password": TEST_PASSWORD},
        headers=csrf_headers(token),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"][0]["field"] == "email"


async def test_error_de_validacion_no_filtra_la_password(client: httpx.AsyncClient) -> None:
    token = await obtain_csrf_token(client)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "no-es-un-email", "password": "SuperSecreta123"},
        headers=csrf_headers(token),
    )

    assert response.status_code == 422
    assert "SuperSecreta123" not in response.text


async def test_login_rota_el_token_csrf(client: httpx.AsyncClient) -> None:
    token_previo = await obtain_csrf_token(client)

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        headers=csrf_headers(token_previo),
    )

    assert response.status_code == 200
    # La cookie CSRF cambia al abrir sesion: anula cualquier token fijado antes.
    assert client.cookies.get("greda_csrf") != token_previo
