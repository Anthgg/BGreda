"""Cliente HTTP de Supabase Auth, con la red simulada por respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.config import Settings
from app.core.errors import (
    AuthInvalidCredentialsError,
    AuthSessionExpiredError,
    ServiceUnavailableError,
    UpstreamAuthError,
)
from app.services.supabase_auth import HttpSupabaseAuthClient

BASE = "https://proyecto-de-prueba.supabase.co"
TOKEN_URL = f"{BASE}/auth/v1/token"
USER_URL = f"{BASE}/auth/v1/user"
LOGOUT_URL = f"{BASE}/auth/v1/logout"

USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

SESION_VALIDA = {
    "access_token": "access-abc",
    "refresh_token": "refresh-abc",
    "expires_in": 3600,
    "user": {"id": USER_ID, "email": "usuario@empresa.com"},
}


@pytest.fixture
def settings() -> Settings:
    return Settings(SUPABASE_URL=BASE, SUPABASE_PUBLISHABLE_KEY="clave-publicable")


@pytest.fixture
def client(settings: Settings) -> HttpSupabaseAuthClient:
    return HttpSupabaseAuthClient(settings)


def test_sin_configuracion_no_se_puede_construir() -> None:
    with pytest.raises(ServiceUnavailableError):
        HttpSupabaseAuthClient(Settings(SUPABASE_URL="", SUPABASE_PUBLISHABLE_KEY=""))


@respx.mock
async def test_login_correcto_devuelve_la_sesion(client: HttpSupabaseAuthClient) -> None:
    ruta = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=SESION_VALIDA))

    sesion = await client.sign_in_with_password("usuario@empresa.com", "clave")

    assert sesion.access_token == "access-abc"
    assert sesion.refresh_token == "refresh-abc"
    assert str(sesion.user_id) == USER_ID
    peticion = ruta.calls.last.request
    assert peticion.url.params["grant_type"] == "password"
    assert peticion.headers["apikey"] == "clave-publicable"


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_credenciales_rechazadas_por_supabase(
    client: HttpSupabaseAuthClient, status: int
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(status, json={"error": "x"}))

    with pytest.raises(AuthInvalidCredentialsError):
        await client.sign_in_with_password("usuario@empresa.com", "mala")


@respx.mock
async def test_error_de_red_se_traduce_a_error_de_upstream(
    client: HttpSupabaseAuthClient,
) -> None:
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("sin conexion"))

    with pytest.raises(UpstreamAuthError):
        await client.sign_in_with_password("usuario@empresa.com", "clave")


@respx.mock
async def test_respuesta_incompleta_se_traduce_a_error_de_upstream(
    client: HttpSupabaseAuthClient,
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "solo-uno"}))

    with pytest.raises(UpstreamAuthError):
        await client.sign_in_with_password("usuario@empresa.com", "clave")


@respx.mock
async def test_error_500_de_supabase_no_se_confunde_con_credenciales(
    client: HttpSupabaseAuthClient,
) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(UpstreamAuthError):
        await client.sign_in_with_password("usuario@empresa.com", "clave")


@respx.mock
async def test_refresh_correcto(client: HttpSupabaseAuthClient) -> None:
    ruta = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=SESION_VALIDA))

    sesion = await client.refresh_session("refresh-anterior")

    assert sesion.access_token == "access-abc"
    assert ruta.calls.last.request.url.params["grant_type"] == "refresh_token"


@respx.mock
async def test_refresh_rechazado_indica_sesion_expirada(client: HttpSupabaseAuthClient) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid"}))

    with pytest.raises(AuthSessionExpiredError):
        await client.refresh_session("refresh-caducado")


@respx.mock
async def test_get_user_devuelve_la_identidad(client: HttpSupabaseAuthClient) -> None:
    ruta = respx.get(USER_URL).mock(
        return_value=httpx.Response(200, json={"id": USER_ID, "email": "usuario@empresa.com"})
    )

    usuario = await client.get_user("access-abc")

    assert str(usuario.id) == USER_ID
    assert ruta.calls.last.request.headers["authorization"] == "Bearer access-abc"


@respx.mock
async def test_get_user_con_token_invalido_indica_sesion_expirada(
    client: HttpSupabaseAuthClient,
) -> None:
    respx.get(USER_URL).mock(return_value=httpx.Response(401, json={"error": "invalid"}))

    with pytest.raises(AuthSessionExpiredError):
        await client.get_user("token-invalido")


@respx.mock
async def test_get_user_con_identificador_corrupto_falla_de_forma_controlada(
    client: HttpSupabaseAuthClient,
) -> None:
    respx.get(USER_URL).mock(return_value=httpx.Response(200, json={"id": "no-es-un-uuid"}))

    with pytest.raises(UpstreamAuthError):
        await client.get_user("access-abc")


@respx.mock
async def test_logout_absorbe_los_fallos_de_supabase(client: HttpSupabaseAuthClient) -> None:
    respx.post(LOGOUT_URL).mock(return_value=httpx.Response(500))

    # No debe lanzar: el cierre de sesion local ocurre igualmente.
    await client.sign_out("access-abc")


@respx.mock
async def test_logout_absorbe_los_errores_de_red(client: HttpSupabaseAuthClient) -> None:
    respx.post(LOGOUT_URL).mock(side_effect=httpx.ConnectError("sin conexion"))

    await client.sign_out("access-abc")
