"""Integracion de la consulta de identidad contra PostgreSQL real.

Los proveedores externos se sustituyen por dobles controlados: estas pruebas
verifican la orquestacion (cache, fallback, cuotas, congelamiento de TTL), no
la red. El contrato de cada proveedor real se prueba aparte en
``tests/unit/test_identity_providers.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    DbSessionDep,
    get_decolecta_provider,
    get_identity_lookup_service,
    get_peru_api_provider,
)
from app.core.identity import LookupStatus, ProviderName
from app.services.identity import IdentityLookupService, _InMemoryRateLimiter
from app.services.identity import _circuit_breaker as circuit_breaker
from app.services.identity_providers import IdentityProvider, ProviderLookupResult
from tests.conftest import TEST_EMAIL, TEST_PASSWORD
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

IDENTITY = "/api/v1/identity"
TEST_HASH_SECRET = "clave-de-prueba-no-es-la-real"

DNI_ACTIVE = {
    "full_name": "Ana Torres Diaz",
    "first_names": "Ana",
    "paternal_surname": "Torres",
    "maternal_surname": "Diaz",
}
RUC_ACTIVE = {
    "business_name": "Ceramica Greda SAC",
    "status": "ACTIVO",
    "condition": "HABIDO",
    "address": "Av. Siempre Viva 123",
    "ubigeo": "150101",
}


class FakeProvider(IdentityProvider):
    """Doble controlado: cada llamada devuelve la siguiente respuesta de la cola."""

    def __init__(self, name: ProviderName) -> None:
        self.name = name
        self.dni_queue: list[ProviderLookupResult] = []
        self.ruc_queue: list[ProviderLookupResult] = []
        self.dni_calls = 0
        self.ruc_calls = 0

    async def lookup_dni(self, document: str) -> ProviderLookupResult:
        self.dni_calls += 1
        return self.dni_queue.pop(0)

    async def lookup_ruc(self, document: str) -> ProviderLookupResult:
        self.ruc_calls += 1
        return self.ruc_queue.pop(0)


def ok(provider: ProviderName, data: dict) -> ProviderLookupResult:
    return ProviderLookupResult(LookupStatus.SUCCESS, provider, dict(data))


def fail(provider: ProviderName, status: LookupStatus) -> ProviderLookupResult:
    return ProviderLookupResult(status, provider)


@pytest.fixture(autouse=True)
def _reset_process_wide_guards() -> Iterator[None]:
    """El cortocircuito y el limitador viven en memoria del proceso, no del test.

    Sin este reinicio, tres fallos provocados en una prueba dejarian el
    proveedor "abierto" durante sesenta segundos reales para las siguientes.
    """
    circuit_breaker._state.clear()
    import app.api.deps as deps_module

    deps_module._rate_limiter = None
    yield
    circuit_breaker._state.clear()
    deps_module._rate_limiter = None


@pytest.fixture
def primary() -> FakeProvider:
    return FakeProvider(ProviderName.PERU_API)


@pytest.fixture
def secondary() -> FakeProvider:
    return FakeProvider(ProviderName.DECOLECTA)


@pytest.fixture
def identity_app(api_app: FastAPI, primary: FakeProvider, secondary: FakeProvider) -> FastAPI:
    api_app.dependency_overrides[get_peru_api_provider] = lambda: primary
    api_app.dependency_overrides[get_decolecta_provider] = lambda: secondary
    return api_app


@pytest.fixture
async def api(identity_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente ya autenticado como ADMIN: toda ruta de identidad exige sesion.

    Un ADMIN puede hacer todo lo que un OPERATOR puede (consultar), asi que
    sirve de sesion por omision; las pruebas que necesitan otro rol o ninguna
    sesion inician su propia sesion sobre este mismo cliente o usan uno aparte.
    """
    transport = httpx.ASGITransport(app=identity_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await authenticate(client, email=TEST_EMAIL, password=TEST_PASSWORD)
        yield client


@pytest.fixture
async def anon_api(identity_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente sin sesion, para la unica prueba que la necesita."""
    transport = httpx.ASGITransport(app=identity_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def override_service(
    app: FastAPI,
    primary: FakeProvider,
    secondary: FakeProvider,
    *,
    fallback_on_not_found: bool,
) -> None:
    """Sustituye el servicio entero para variar una politica sin tocar Settings.

    Se define como funcion interna para capturar ``fallback_on_not_found`` del
    cierre lexico; FastAPI resuelve igual el ``Depends`` anidado en el override.
    """
    limiter = _InMemoryRateLimiter(1000, 60)

    async def _override(session: DbSessionDep) -> IdentityLookupService:
        return IdentityLookupService(
            session,
            primary=primary,
            secondary=secondary,
            dni_ttl_days=30,
            ruc_ttl_days=7,
            fallback_on_not_found=fallback_on_not_found,
            rate_limiter=limiter,
            hash_secret=TEST_HASH_SECRET,
        )

    app.dependency_overrides[get_identity_lookup_service] = _override


# ---------------------------------------------------------------------------
# Validacion antes de tocar la red
# ---------------------------------------------------------------------------
async def test_un_dni_invalido_se_rechaza_sin_llamar_al_proveedor(
    api: httpx.AsyncClient, primary: FakeProvider
) -> None:
    respuesta = await api.get(f"{IDENTITY}/dni/1234567")
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "INVALID_DNI"
    assert primary.dni_calls == 0


async def test_un_ruc_invalido_se_rechaza_sin_llamar_al_proveedor(
    api: httpx.AsyncClient, primary: FakeProvider
) -> None:
    respuesta = await api.get(f"{IDENTITY}/ruc/12345")
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "INVALID_RUC"
    assert primary.ruc_calls == 0


# ---------------------------------------------------------------------------
# Primario, fallback y fallo total
# ---------------------------------------------------------------------------
async def test_el_primario_responde_y_no_se_llama_al_secundario(
    api: httpx.AsyncClient, primary: FakeProvider, secondary: FakeProvider
) -> None:
    primary.dni_queue.append(ok(ProviderName.PERU_API, DNI_ACTIVE))
    respuesta = await api.get(f"{IDENTITY}/dni/10000001")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["full_name"] == "Ana Torres Diaz"
    assert cuerpo["provider"] == "PERU_API"
    assert cuerpo["cache_hit"] is False
    assert secondary.dni_calls == 0


@pytest.mark.parametrize(
    "estado",
    [LookupStatus.RATE_LIMITED, LookupStatus.TIMEOUT, LookupStatus.PROVIDER_ERROR],
)
async def test_si_el_primario_falla_se_llama_al_secundario(
    api: httpx.AsyncClient, primary: FakeProvider, secondary: FakeProvider, estado: LookupStatus
) -> None:
    primary.dni_queue.append(fail(ProviderName.PERU_API, estado))
    secondary.dni_queue.append(ok(ProviderName.DECOLECTA, DNI_ACTIVE))
    respuesta = await api.get(f"{IDENTITY}/dni/10000002")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["provider"] == "DECOLECTA"
    assert primary.dni_calls == 1
    assert secondary.dni_calls == 1


async def test_si_ambos_fallan_el_error_es_normalizado(
    api: httpx.AsyncClient, primary: FakeProvider, secondary: FakeProvider
) -> None:
    primary.dni_queue.append(fail(ProviderName.PERU_API, LookupStatus.TIMEOUT))
    secondary.dni_queue.append(fail(ProviderName.DECOLECTA, LookupStatus.PROVIDER_ERROR))
    respuesta = await api.get(f"{IDENTITY}/dni/10000003")
    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["error"]["code"] == "IDENTITY_LOOKUP_UNAVAILABLE"
    # Nunca el texto crudo del proveedor.
    assert "PROVIDER_ERROR" not in cuerpo["error"]["message"]
    assert "TIMEOUT" not in cuerpo["error"]["message"]


# ---------------------------------------------------------------------------
# NOT_FOUND: politica por omision vs configurable
# ---------------------------------------------------------------------------
async def test_not_found_no_llama_al_secundario_por_omision(
    api: httpx.AsyncClient, primary: FakeProvider, secondary: FakeProvider
) -> None:
    primary.dni_queue.append(fail(ProviderName.PERU_API, LookupStatus.NOT_FOUND))
    respuesta = await api.get(f"{IDENTITY}/dni/10000004")
    assert respuesta.status_code == 404
    assert secondary.dni_calls == 0


async def test_not_found_llama_al_secundario_si_la_politica_lo_permite(
    identity_app: FastAPI, primary: FakeProvider, secondary: FakeProvider
) -> None:
    override_service(identity_app, primary, secondary, fallback_on_not_found=True)
    primary.dni_queue.append(fail(ProviderName.PERU_API, LookupStatus.NOT_FOUND))
    secondary.dni_queue.append(ok(ProviderName.DECOLECTA, DNI_ACTIVE))

    transport = httpx.ASGITransport(app=identity_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await authenticate(client, email=TEST_EMAIL, password=TEST_PASSWORD)
        respuesta = await client.get(f"{IDENTITY}/dni/10000005")
    assert respuesta.status_code == 200
    assert secondary.dni_calls == 1


# ---------------------------------------------------------------------------
# Cache: acierto sin red, refresco explicito, TTL vencido
# ---------------------------------------------------------------------------
async def test_una_segunda_consulta_sirve_desde_cache_sin_llamar_al_proveedor(
    api: httpx.AsyncClient, primary: FakeProvider
) -> None:
    primary.dni_queue.append(ok(ProviderName.PERU_API, DNI_ACTIVE))
    primera = await api.get(f"{IDENTITY}/dni/10000006")
    assert primera.status_code == 200
    assert primera.json()["cache_hit"] is False

    segunda = await api.get(f"{IDENTITY}/dni/10000006")
    assert segunda.status_code == 200
    assert segunda.json()["cache_hit"] is True
    assert segunda.json()["full_name"] == "Ana Torres Diaz"
    assert primary.dni_calls == 1  # no crecio: la segunda no toco la red


async def test_la_fila_de_cache_nunca_guarda_el_documento_en_claro(
    api: httpx.AsyncClient, primary: FakeProvider, db_session: AsyncSession
) -> None:
    """La cache indexa por hash: guardar tambien el numero en el payload
    anularia esa proteccion. El numero de la respuesta se repone en cada
    lectura a partir de lo que pidio esa peticion, no de lo guardado."""
    primary.dni_queue.append(ok(ProviderName.PERU_API, DNI_ACTIVE))
    respuesta = await api.get(f"{IDENTITY}/dni/10000014")
    assert respuesta.status_code == 200
    assert respuesta.json()["document_number"] == "10000014"

    fila = (
        await db_session.execute(text("SELECT payload FROM identity_lookup_cache"))
    ).scalar_one()
    assert "document_number" not in fila
    assert "10000014" not in str(fila)


async def test_refresh_ignora_la_cache_y_vuelve_a_llamar_al_proveedor(
    api: httpx.AsyncClient, primary: FakeProvider
) -> None:
    primary.dni_queue.extend(
        [ok(ProviderName.PERU_API, DNI_ACTIVE), ok(ProviderName.PERU_API, DNI_ACTIVE)]
    )
    await api.get(f"{IDENTITY}/dni/10000007")

    respuesta = await api.get(f"{IDENTITY}/dni/10000007", params={"refresh": "true"})
    assert respuesta.status_code == 200
    assert respuesta.json()["cache_hit"] is False
    assert primary.dni_calls == 2


async def test_un_ttl_vencido_se_trata_como_si_no_hubiera_cache(
    api: httpx.AsyncClient, primary: FakeProvider, db_session: AsyncSession
) -> None:
    primary.dni_queue.extend(
        [ok(ProviderName.PERU_API, DNI_ACTIVE), ok(ProviderName.PERU_API, DNI_ACTIVE)]
    )
    await api.get(f"{IDENTITY}/dni/10000008")
    assert primary.dni_calls == 1

    vencido = datetime.now(UTC) - timedelta(days=1)
    await db_session.execute(
        text("UPDATE identity_lookup_cache SET expires_at = :vencido"), {"vencido": vencido}
    )
    await db_session.commit()

    respuesta = await api.get(f"{IDENTITY}/dni/10000008")
    assert respuesta.status_code == 200
    assert respuesta.json()["cache_hit"] is False
    assert primary.dni_calls == 2


# ---------------------------------------------------------------------------
# RUC y ubigeo
# ---------------------------------------------------------------------------
async def test_ruc_resuelve_el_ubigeo_contra_el_catalogo_existente(
    api: httpx.AsyncClient, primary: FakeProvider, db_session: AsyncSession
) -> None:
    """Un codigo que no existe en el INEI real, insertado a proposito por la
    prueba: as no depende de como venga escrito el catalogo real (mayusculas
    en ``ubigeo_inei_2022.csv``) y verifica solo que se use ese catalogo.
    """
    await db_session.execute(
        text(
            "INSERT INTO ubigeo_districts "
            "(code, department_code, department_name, province_code, province_name, district_name) "
            "VALUES ('999901', '99', 'Departamento De Prueba', '9901', "
            "'Provincia De Prueba', 'Distrito De Prueba')"
        )
    )
    await db_session.commit()
    primary.ruc_queue.append(ok(ProviderName.PERU_API, dict(RUC_ACTIVE, ubigeo="999901")))

    respuesta = await api.get(f"{IDENTITY}/ruc/20100000001")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["ubigeo"] == "999901"
    assert cuerpo["department"] == "Departamento De Prueba"
    assert cuerpo["province"] == "Provincia De Prueba"
    assert cuerpo["district"] == "Distrito De Prueba"


async def test_ruc_con_ubigeo_desconocido_no_inserta_un_distrito_nuevo(
    api: httpx.AsyncClient, primary: FakeProvider, db_session: AsyncSession
) -> None:
    """El valor crudo se conserva; el catalogo territorial no se toca."""
    otro = dict(RUC_ACTIVE, ubigeo="999999")
    primary.ruc_queue.append(ok(ProviderName.PERU_API, otro))

    respuesta = await api.get(f"{IDENTITY}/ruc/20100000002")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["ubigeo"] == "999999"
    assert cuerpo["department"] is None
    assert cuerpo["province"] is None
    assert cuerpo["district"] is None

    existe = await db_session.execute(
        text("SELECT count(*) FROM ubigeo_districts WHERE code = '999999'")
    )
    assert existe.scalar_one() == 0


# ---------------------------------------------------------------------------
# Nada del proveedor viaja crudo: ni token, ni respuesta completa
# ---------------------------------------------------------------------------
async def test_la_respuesta_no_expone_secretos_ni_metadatos_internos(
    api: httpx.AsyncClient, primary: FakeProvider
) -> None:
    primary.dni_queue.append(ok(ProviderName.PERU_API, DNI_ACTIVE))
    respuesta = await api.get(f"{IDENTITY}/dni/10000009")
    cuerpo = respuesta.text
    for prohibido in ("token", "Authorization", "Bearer", "peru-api", "decolecta"):
        assert prohibido not in cuerpo


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
async def test_operator_puede_consultar_pero_no_refrescar(
    api: httpx.AsyncClient, primary: FakeProvider
) -> None:
    await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

    primary.dni_queue.append(ok(ProviderName.PERU_API, DNI_ACTIVE))
    consulta = await api.get(f"{IDENTITY}/dni/10000010")
    assert consulta.status_code == 200

    refresco = await api.get(f"{IDENTITY}/dni/10000010", params={"refresh": "true"})
    assert refresco.status_code == 403
    assert refresco.json()["error"]["code"] == "AUTH_INSUFFICIENT_ROLE"
    # El intento de refresco no debe haber consumido cuota externa.
    assert primary.dni_calls == 1


async def test_sin_sesion_se_rechaza(anon_api: httpx.AsyncClient, primary: FakeProvider) -> None:
    """Un cliente que nunca inicio sesion no trae la cookie de acceso."""
    respuesta = await anon_api.get(f"{IDENTITY}/dni/10000011")
    assert respuesta.status_code == 401
    assert respuesta.json()["error"]["code"] == "AUTH_NOT_AUTHENTICATED"
    assert primary.dni_calls == 0


# ---------------------------------------------------------------------------
# Limite interno de consultas
# ---------------------------------------------------------------------------
async def test_el_limite_interno_bloquea_tras_demasiadas_consultas_del_mismo_documento(
    identity_app: FastAPI, primary: FakeProvider, secondary: FakeProvider
) -> None:
    # Un unico limitador para las tres peticiones: uno nuevo por peticion
    # nunca acumularia aciertos y el limite no se alcanzaria jamas.
    limiter = _InMemoryRateLimiter(max_requests=2, window_seconds=60)

    async def _override(session: DbSessionDep) -> IdentityLookupService:
        return IdentityLookupService(
            session,
            primary=primary,
            secondary=secondary,
            dni_ttl_days=30,
            ruc_ttl_days=7,
            fallback_on_not_found=False,
            rate_limiter=limiter,
            hash_secret=TEST_HASH_SECRET,
        )

    identity_app.dependency_overrides[get_identity_lookup_service] = _override
    for _ in range(2):
        primary.dni_queue.append(fail(ProviderName.PERU_API, LookupStatus.NOT_FOUND))

    transport = httpx.ASGITransport(app=identity_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await authenticate(client, email=TEST_EMAIL, password=TEST_PASSWORD)
        for _ in range(2):
            respuesta = await client.get(f"{IDENTITY}/dni/10000012")
            assert respuesta.status_code == 404
        limitada = await client.get(f"{IDENTITY}/dni/10000012")
    assert limitada.status_code == 503
    assert limitada.json()["error"]["code"] == "IDENTITY_LOOKUP_UNAVAILABLE"
    assert primary.dni_calls == 2  # la tercera ni siquiera llego al proveedor


# ---------------------------------------------------------------------------
# Metricas
# ---------------------------------------------------------------------------
async def test_se_registran_metricas_por_proveedor_y_estadisticas_del_dia(
    api: httpx.AsyncClient, primary: FakeProvider, secondary: FakeProvider, db_session: AsyncSession
) -> None:
    primary.dni_queue.append(fail(ProviderName.PERU_API, LookupStatus.RATE_LIMITED))
    secondary.dni_queue.append(ok(ProviderName.DECOLECTA, DNI_ACTIVE))
    await api.get(f"{IDENTITY}/dni/10000013")
    # Repetir para tener un acierto de cache.
    await api.get(f"{IDENTITY}/dni/10000013")

    filas = (
        await db_session.execute(
            text(
                "SELECT provider, requests, rate_limited, success "
                "FROM identity_lookup_provider_metrics ORDER BY provider"
            )
        )
    ).all()
    por_proveedor = {f[0]: f for f in filas}
    assert por_proveedor["PERU_API"][1] == 1  # una peticion
    assert por_proveedor["PERU_API"][2] == 1  # que fue RATE_LIMITED
    assert por_proveedor["DECOLECTA"][3] == 1  # exitosa

    diario = (
        await db_session.execute(
            text("SELECT cache_hits, fallback_used FROM identity_lookup_daily_stats")
        )
    ).one()
    assert diario[0] == 1  # la segunda consulta fue cache
    assert diario[1] == 1  # la primera uso fallback
