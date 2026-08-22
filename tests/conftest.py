"""Fixtures compartidas.

La suite es hermetica: no requiere Internet, ni Supabase, ni PostgreSQL. El
proveedor de identidad y el repositorio de perfiles se sustituyen por dobles
mediante ``dependency_overrides``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator

# Las variables se fijan antes de importar la configuracion para que ningun
# fichero .env local del desarrollador altere el resultado de las pruebas.
os.environ.update(
    {
        "APP_ENV": "local",
        "APP_NAME": "Cotizador Greda API (test)",
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_PUBLISHABLE_KEY": "test-publishable-key",
        "DATABASE_URL": "",
        "FRONTEND_ORIGINS": "http://localhost:5173,https://app.example.com",
        "COOKIE_SECURE": "false",
        "COOKIE_SAMESITE": "lax",
        "COOKIE_DOMAIN": "",
        "CSRF_SECRET": "clave-de-pruebas-suficientemente-larga-0123456789",
        "LOG_LEVEL": "WARNING",
    }
)

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import get_profile_repository, get_supabase_auth_client
from app.auth.cookies import CSRF_COOKIE_NAME
from app.auth.csrf import CSRF_HEADER_NAME
from app.core.config import Settings, get_settings
from app.main import create_app
from app.models.profile import Profile, UserRole
from tests.fakes import FakeProfileRepository, FakeSupabaseAuthClient

TEST_PASSWORD = "clave-valida-de-prueba"
TEST_EMAIL = "admin@empresa.com"
TEST_USER_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def admin_profile() -> Profile:
    profile = Profile()
    profile.id = TEST_USER_ID
    profile.display_name = "Administrador"
    profile.role = UserRole.ADMIN
    profile.active = True
    return profile


@pytest.fixture
def supabase(admin_profile: Profile) -> FakeSupabaseAuthClient:
    client = FakeSupabaseAuthClient()
    client.register(email=TEST_EMAIL, password=TEST_PASSWORD, user_id=admin_profile.id)
    return client


@pytest.fixture
def profiles(admin_profile: Profile) -> FakeProfileRepository:
    return FakeProfileRepository({admin_profile.id: admin_profile})


@pytest.fixture
def app(
    settings: Settings,
    supabase: FakeSupabaseAuthClient,
    profiles: FakeProfileRepository,
) -> Iterator[FastAPI]:
    application = create_app(settings)
    application.dependency_overrides[get_supabase_auth_client] = lambda: supabase
    application.dependency_overrides[get_profile_repository] = lambda: profiles
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


async def obtain_csrf_token(client: httpx.AsyncClient) -> str:
    """Pide un token CSRF y lo devuelve, dejando la cookie en el cliente."""
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def csrf_headers(token: str) -> dict[str, str]:
    return {CSRF_HEADER_NAME: token}


async def login(
    client: httpx.AsyncClient,
    *,
    email: str = TEST_EMAIL,
    password: str = TEST_PASSWORD,
) -> httpx.Response:
    """Realiza el flujo completo: token CSRF y despues login."""
    token = await obtain_csrf_token(client)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers=csrf_headers(token),
    )
    return response


async def current_csrf_token(client: httpx.AsyncClient) -> str:
    """Lee el token CSRF vigente desde el frasco de cookies del cliente de test.

    Solo es posible en pruebas: la cookie es HttpOnly y el navegador jamas la
    expone a JavaScript.
    """
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value is not None
    return str(value)
