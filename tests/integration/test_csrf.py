"""Proteccion CSRF sobre operaciones mutadoras."""

from __future__ import annotations

import httpx
import pytest

from app.auth.cookies import CSRF_COOKIE_NAME
from tests.conftest import TEST_EMAIL, TEST_PASSWORD, csrf_headers, obtain_csrf_token

CREDENCIALES = {"email": TEST_EMAIL, "password": TEST_PASSWORD}


async def test_csrf_emite_token_y_cookie_httponly(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/csrf")

    assert response.status_code == 200
    body = response.json()
    assert body["csrf_token"]
    assert body["expires_in"] > 0

    cookie_header = next(
        raw for raw in response.headers.get_list("set-cookie") if raw.startswith(CSRF_COOKIE_NAME)
    )
    # HttpOnly tambien en la cookie CSRF: el frontend usa el valor del cuerpo.
    assert "httponly" in cookie_header.lower()


async def test_operacion_mutadora_sin_token_csrf_falla(client: httpx.AsyncClient) -> None:
    await obtain_csrf_token(client)  # deja la cookie, pero no se envia cabecera

    response = await client.post("/api/v1/auth/login", json=CREDENCIALES)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_MISSING"


async def test_operacion_mutadora_sin_cookie_csrf_falla(client: httpx.AsyncClient) -> None:
    token = await obtain_csrf_token(client)
    client.cookies.clear()

    response = await client.post(
        "/api/v1/auth/login", json=CREDENCIALES, headers=csrf_headers(token)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_MISSING"


async def test_token_csrf_que_no_coincide_con_la_cookie_falla(client: httpx.AsyncClient) -> None:
    await obtain_csrf_token(client)

    response = await client.post(
        "/api/v1/auth/login",
        json=CREDENCIALES,
        headers=csrf_headers("token-de-otro-origen"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_INVALID"


async def test_token_csrf_falsificado_falla_aunque_coincida(client: httpx.AsyncClient) -> None:
    """Coincidir cookie y cabecera no basta: la firma HMAC tambien se valida."""
    falsificado = "nonce-falso.9999999999.firma-invalida"

    response = await client.post(
        "/api/v1/auth/login",
        json=CREDENCIALES,
        headers={**csrf_headers(falsificado), "Cookie": f"{CSRF_COOKIE_NAME}={falsificado}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_INVALID"


async def test_token_csrf_valido_permite_la_operacion(client: httpx.AsyncClient) -> None:
    token = await obtain_csrf_token(client)

    response = await client.post(
        "/api/v1/auth/login", json=CREDENCIALES, headers=csrf_headers(token)
    )

    assert response.status_code == 200


@pytest.mark.parametrize("metodo", ["put", "patch", "delete"])
async def test_todos_los_metodos_mutadores_estan_protegidos(
    client: httpx.AsyncClient, metodo: str
) -> None:
    # La ruta no existe: si el CSRF se validara despues del enrutado se
    # obtendria 404. El 403 demuestra que el control es previo y global.
    response = await getattr(client, metodo)("/api/v1/recurso-inexistente")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_MISSING"


async def test_las_lecturas_no_requieren_csrf(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")

    # 401 (sin sesion), nunca 403 por CSRF.
    assert response.status_code == 401
