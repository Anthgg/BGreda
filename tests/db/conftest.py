"""Fixtures de las pruebas que necesitan PostgreSQL real.

Se ejecutan solo si existe ``TEST_DATABASE_URL``; en caso contrario se omiten,
de modo que ``pytest`` sigue siendo verde sin base de datos. La CI levanta un
PostgreSQL de servicio y las ejecuta siempre.

Todo ocurre dentro de un esquema propio que se crea y se destruye en cada
sesion de pruebas: nunca se tocan las tablas reales de la aplicacion.
"""

from __future__ import annotations

import csv
import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import (
    get_db_session,
    get_object_storage,
    get_profile_repository,
    get_supabase_auth_client,
)
from app.db.session import normalize_database_url
from app.main import create_app
from app.models import Base
from app.models.catalog import CurrencyCatalog, SequencePatternPreset, UbigeoDistrict
from app.models.masters import UnitOfMeasure
from app.models.profile import Profile, UserRole
from app.models.settings import SINGLETON_ID
from tests.conftest import TEST_EMAIL, TEST_PASSWORD, TEST_USER_ID
from tests.db.fakes import FakeObjectStorage
from tests.fakes import FakeProfileRepository, FakeSupabaseAuthClient

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
TEST_SCHEMA = os.getenv("TEST_SCHEMA", "greda_test")

#: Segundo usuario, con rol OPERATOR, para comprobar la autorizacion.
OPERATOR_ID = uuid.UUID("22222222-3333-4444-5555-666666666666")
OPERATOR_EMAIL = "operario@empresa.com"
OPERATOR_PASSWORD = "clave-operario-de-prueba"

#: Marca de omision. ``pytestmark`` no sirve aqui: pytest solo lo lee en los
#: modulos de prueba, no en un conftest, de modo que sin base de datos las
#: pruebas llegaban a la fixture ``db_engine`` y reventaban con ERROR en vez de
#: omitirse. El hook de coleccion si es un mecanismo valido a nivel de conftest.
_SKIP_SIN_BASE_DE_DATOS = pytest.mark.skip(
    reason="TEST_DATABASE_URL no definida: se omiten las pruebas con base de datos",
)
_DB_TESTS_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Omite las pruebas de este directorio cuando no hay PostgreSQL.

    En la CI ``TEST_DATABASE_URL`` siempre esta definida, asi que alli no se
    omite nada y un fallo de conexion sigue apareciendo como error real.
    """
    if TEST_DATABASE_URL:
        return
    for item in items:
        if item.path is not None and _DB_TESTS_DIR in item.path.parents:
            item.add_marker(_SKIP_SIN_BASE_DE_DATOS)


DEFAULT_PATTERN = "{PREFIX}-{YYYY}-{NUMBER}"
DATA_DIR = Path(__file__).resolve().parents[2] / "alembic" / "data"


def _catalog_rows(name: str) -> list[dict[str, object]]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _engine_url() -> str:
    return normalize_database_url(TEST_DATABASE_URL)


@pytest.fixture(scope="session")
async def db_engine() -> AsyncIterator[object]:
    """Crea un esquema aislado con todas las tablas y lo destruye al final."""
    admin = create_async_engine(_engine_url(), connect_args={"statement_cache_size": 0})
    async with admin.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
        await connection.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))
    await admin.dispose()

    # NullPool: sin el, asyncpg reutilizaria conexiones entre bucles de evento
    # distintos y fallaria con "another operation is in progress".
    engine = create_async_engine(
        _engine_url(),
        poolclass=NullPool,
        connect_args={
            "statement_cache_size": 0,
            "server_settings": {"search_path": TEST_SCHEMA},
        },
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        currencies = _catalog_rows("iso_4217_2026.csv")
        for row in currencies:
            row["minor_units"] = int(str(row["minor_units"])) if row["minor_units"] else None
        await connection.execute(CurrencyCatalog.__table__.insert(), currencies)
        await connection.execute(
            UbigeoDistrict.__table__.insert(), _catalog_rows("ubigeo_inei_2022.csv")
        )
        await connection.execute(
            UnitOfMeasure.__table__.insert(),
            [
                {
                    "code": "g",
                    "name": "Gramo",
                    "symbol": "g",
                    "dimension": "MASS",
                    "factor_to_base": Decimal(1),
                    "is_base": True,
                },
                {
                    "code": "kg",
                    "name": "Kilogramo",
                    "symbol": "kg",
                    "dimension": "MASS",
                    "factor_to_base": Decimal(1000),
                    "is_base": False,
                },
                {
                    "code": "unit",
                    "name": "Unidad",
                    "symbol": "u",
                    "dimension": "COUNT",
                    "factor_to_base": Decimal(1),
                    "is_base": True,
                },
            ],
        )
        await connection.execute(
            SequencePatternPreset.__table__.insert(),
            [
                {
                    "name": "Prefijo - año - número",
                    "pattern": DEFAULT_PATTERN,
                    "is_system": True,
                },
                {
                    "name": "Prefijo - número",
                    "pattern": "{PREFIX}-{NUMBER}",
                    "is_system": True,
                },
            ],
        )

    yield engine
    await engine.dispose()

    cleanup = create_async_engine(_engine_url(), connect_args={"statement_cache_size": 0})
    async with cleanup.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
    await cleanup.dispose()


@pytest.fixture
def sessionmaker_for_tests(db_engine: object) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
async def reset_database(
    db_engine: object,
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Deja la base en el estado que produce la migracion, antes de cada prueba."""
    async with sessionmaker_for_tests() as session:
        await session.execute(
            text(
                "TRUNCATE audit_events, bank_accounts, document_sequence_issues, "
                "quotation_product_price_updates, quotation_other_costs, "
                "quotation_additionals, quotation_techniques, quotations, "
                "other_costs, additionals, techniques, "
                "document_sequences, commercial_settings, company_settings, profiles, "
                "recipe_lines, recipe_versions, recipes, "
                "firing_lines, firing_kiln_sessions, firings, "
                "kiln_occupancy_factors, kiln_rates, kilns, "
                "import_rows, import_batches, stock_movements, stock_balances, "
                "stock_locations, products, partners, product_categories, pos_categories, "
                "identity_lookup_audit_events, identity_lookup_provider_metrics, "
                "identity_lookup_daily_stats, identity_lookup_cache "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.execute(text("DELETE FROM sequence_pattern_presets WHERE NOT is_system"))
        await session.execute(
            text("INSERT INTO company_settings (id) VALUES (:id)"), {"id": SINGLETON_ID}
        )
        await session.execute(
            text("INSERT INTO commercial_settings (id) VALUES (:id)"), {"id": SINGLETON_ID}
        )
        for sequence_type, prefix, pattern, padding, reset_policy in (
            ("QUOTE", "CTZ", DEFAULT_PATTERN, 6, "YEARLY"),
            ("FIRING", "HR", DEFAULT_PATTERN, 6, "YEARLY"),
            ("PRODUCT_50", "LAB50", "{PREFIX}{NUMBER}", 3, "NEVER"),
            ("PRODUCT_70", "LAB70", "{PREFIX}{NUMBER}", 3, "NEVER"),
        ):
            await session.execute(
                text(
                    "INSERT INTO document_sequences "
                    "(sequence_type, prefix, pattern, padding, reset_policy, "
                    " current_value, period_key, active) "
                    "VALUES (:t, :p, :pat, :pad, :rp, 0, '', true)"
                ),
                {
                    "t": sequence_type,
                    "p": prefix,
                    "pat": pattern,
                    "pad": padding,
                    "rp": reset_policy,
                },
            )
        await session.commit()

    yield


@pytest.fixture
async def db_session(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker_for_tests() as session:
        yield session


# ---------------------------------------------------------------------------
# Aplicacion con base de datos real y autenticacion simulada
# ---------------------------------------------------------------------------
def _profile(user_id: uuid.UUID, name: str, role: UserRole) -> Profile:
    profile = Profile()
    profile.id = user_id
    profile.display_name = name
    profile.role = role
    profile.active = True
    return profile


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


@pytest.fixture
def api_app(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
    storage: FakeObjectStorage,
) -> FastAPI:
    """Aplicacion con PostgreSQL real y Supabase simulado."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    application = create_app(get_settings())

    supabase = FakeSupabaseAuthClient()
    supabase.register(email=TEST_EMAIL, password=TEST_PASSWORD, user_id=TEST_USER_ID)
    supabase.register(email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD, user_id=OPERATOR_ID)

    profiles = FakeProfileRepository(
        {
            TEST_USER_ID: _profile(TEST_USER_ID, "Administrador", UserRole.ADMIN),
            OPERATOR_ID: _profile(OPERATOR_ID, "Operario", UserRole.OPERATOR),
        }
    )

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with sessionmaker_for_tests() as session:
            yield session

    application.dependency_overrides[get_db_session] = _session_override
    application.dependency_overrides[get_supabase_auth_client] = lambda: supabase
    application.dependency_overrides[get_profile_repository] = lambda: profiles
    application.dependency_overrides[get_object_storage] = lambda: storage
    return application


@pytest.fixture
async def api(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def authenticate(client: httpx.AsyncClient, *, email: str, password: str) -> str:
    """Inicia sesion y devuelve el token CSRF vigente."""
    token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return str(client.cookies.get("greda_csrf"))


@pytest.fixture
async def admin_csrf(api: httpx.AsyncClient) -> str:
    return await authenticate(api, email=TEST_EMAIL, password=TEST_PASSWORD)


@pytest.fixture
async def operator_csrf(api: httpx.AsyncClient) -> str:
    return await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
