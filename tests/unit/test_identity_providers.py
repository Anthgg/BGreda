"""Contrato de los adaptadores de proveedor, con la red simulada por respx.

## Sobre el contrato asumido

Los nombres de campo que estas pruebas fijan (``nombres``, ``razon_social``,
``full_name``...) son el mejor esfuerzo sin acceso a la documentacion en vivo
de Peru API ni Decolecta en el momento de escribir esto (ver el aviso al
inicio de ``app.services.identity_providers``). Estas pruebas documentan
exactamente que contrato asume el codigo: si al verificar contra una
respuesta real un campo tiene otro nombre, la prueba que falla senala
exactamente que ajustar.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.identity import LookupStatus, ProviderName
from app.services.identity_providers import DecolectaProvider, PeruApiProvider

PERU_BASE = "https://peru-api-test.example"
DECOLECTA_BASE = "https://decolecta-test.example"


@pytest.fixture
def peru_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=PERU_BASE)


@pytest.fixture
def decolecta_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=DECOLECTA_BASE)


@pytest.fixture
def peru_provider(peru_client: httpx.AsyncClient) -> PeruApiProvider:
    return PeruApiProvider(peru_client, "token-peru-api")


@pytest.fixture
def decolecta_provider(decolecta_client: httpx.AsyncClient) -> DecolectaProvider:
    return DecolectaProvider(decolecta_client, "token-decolecta")


# ---------------------------------------------------------------------------
# Peru API — DNI
# ---------------------------------------------------------------------------
@respx.mock
async def test_peru_api_dni_normaliza_una_respuesta_exitosa(
    peru_provider: PeruApiProvider,
) -> None:
    respx.get(f"{PERU_BASE}/v1/dni").mock(
        return_value=httpx.Response(
            200,
            json={"nombres": "Ana Maria", "apellido_paterno": "Torres", "apellido_materno": "Diaz"},
        )
    )
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.SUCCESS
    assert resultado.provider is ProviderName.PERU_API
    assert resultado.data == {
        "full_name": "Ana Maria Torres Diaz",
        "first_names": "Ana Maria",
        "paternal_surname": "Torres",
        "maternal_surname": "Diaz",
    }


@respx.mock
async def test_peru_api_dni_sin_apellido_materno_no_inventa_el_campo(
    peru_provider: PeruApiProvider,
) -> None:
    """Un campo que el proveedor no trae llega como None, nunca como cadena vacia inventada."""
    respx.get(f"{PERU_BASE}/v1/dni").mock(
        return_value=httpx.Response(200, json={"nombres": "Luis", "apellido_paterno": "Vega"})
    )
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.SUCCESS
    assert resultado.data is not None
    assert resultado.data["maternal_surname"] is None


@respx.mock
async def test_peru_api_dni_404_es_not_found(peru_provider: PeruApiProvider) -> None:
    respx.get(f"{PERU_BASE}/v1/dni").mock(return_value=httpx.Response(404))
    resultado = await peru_provider.lookup_dni("99999999")
    assert resultado.status is LookupStatus.NOT_FOUND


@respx.mock
async def test_peru_api_dni_429_es_rate_limited(peru_provider: PeruApiProvider) -> None:
    respx.get(f"{PERU_BASE}/v1/dni").mock(return_value=httpx.Response(429))
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.RATE_LIMITED


@respx.mock
async def test_peru_api_dni_500_es_provider_error(peru_provider: PeruApiProvider) -> None:
    respx.get(f"{PERU_BASE}/v1/dni").mock(return_value=httpx.Response(500))
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.PROVIDER_ERROR


@respx.mock
async def test_peru_api_dni_timeout_es_timeout(peru_provider: PeruApiProvider) -> None:
    respx.get(f"{PERU_BASE}/v1/dni").mock(side_effect=httpx.TimeoutException("timeout"))
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.TIMEOUT


@respx.mock
async def test_peru_api_dni_json_malformado_es_provider_error(
    peru_provider: PeruApiProvider,
) -> None:
    respx.get(f"{PERU_BASE}/v1/dni").mock(
        return_value=httpx.Response(200, content=b"esto no es json")
    )
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.PROVIDER_ERROR


@respx.mock
async def test_peru_api_dni_cuerpo_vacio_es_not_found(peru_provider: PeruApiProvider) -> None:
    """200 con cuerpo vacio o sin nombres: se trata como no encontrado, no como exito vacio."""
    respx.get(f"{PERU_BASE}/v1/dni").mock(return_value=httpx.Response(200, json={}))
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.NOT_FOUND


# ---------------------------------------------------------------------------
# Peru API — RUC
# ---------------------------------------------------------------------------
@respx.mock
async def test_peru_api_ruc_normaliza_una_respuesta_exitosa(
    peru_provider: PeruApiProvider,
) -> None:
    respx.get(f"{PERU_BASE}/v1/ruc").mock(
        return_value=httpx.Response(
            200,
            json={
                "razon_social": "Ceramica Greda SAC",
                "estado": "ACTIVO",
                "condicion": "HABIDO",
                "direccion": "Av. Siempre Viva 123",
                "ubigeo": "150101",
            },
        )
    )
    resultado = await peru_provider.lookup_ruc("20123456789")
    assert resultado.status is LookupStatus.SUCCESS
    assert resultado.data == {
        "business_name": "Ceramica Greda SAC",
        "status": "ACTIVO",
        "condition": "HABIDO",
        "address": "Av. Siempre Viva 123",
        "ubigeo": "150101",
    }


@respx.mock
async def test_peru_api_ruc_sin_direccion_no_inventa_el_campo(
    peru_provider: PeruApiProvider,
) -> None:
    respx.get(f"{PERU_BASE}/v1/ruc").mock(
        return_value=httpx.Response(200, json={"razon_social": "Solo Nombre SAC"})
    )
    resultado = await peru_provider.lookup_ruc("20123456789")
    assert resultado.status is LookupStatus.SUCCESS
    assert resultado.data is not None
    assert resultado.data["address"] is None


# ---------------------------------------------------------------------------
# Decolecta — DNI y RUC (mismo contrato de estados, distinto payload)
# ---------------------------------------------------------------------------
@respx.mock
async def test_decolecta_dni_normaliza_una_respuesta_exitosa(
    decolecta_provider: DecolectaProvider,
) -> None:
    respx.get(f"{DECOLECTA_BASE}/v1/reniec/dni").mock(
        return_value=httpx.Response(
            200,
            json={
                "first_name": "Ana Maria",
                "first_last_name": "Torres",
                "second_last_name": "Diaz",
                "full_name": "Ana Maria Torres Diaz",
                "document_number": "12345678",
            },
        )
    )
    resultado = await decolecta_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.SUCCESS
    assert resultado.provider is ProviderName.DECOLECTA
    assert resultado.data == {
        "full_name": "Ana Maria Torres Diaz",
        "first_names": "Ana Maria",
        "paternal_surname": "Torres",
        "maternal_surname": "Diaz",
    }


@respx.mock
async def test_decolecta_dni_404_es_not_found(decolecta_provider: DecolectaProvider) -> None:
    respx.get(f"{DECOLECTA_BASE}/v1/reniec/dni").mock(return_value=httpx.Response(404))
    resultado = await decolecta_provider.lookup_dni("99999999")
    assert resultado.status is LookupStatus.NOT_FOUND


@respx.mock
async def test_decolecta_dni_429_es_rate_limited(decolecta_provider: DecolectaProvider) -> None:
    respx.get(f"{DECOLECTA_BASE}/v1/reniec/dni").mock(return_value=httpx.Response(429))
    resultado = await decolecta_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.RATE_LIMITED


@respx.mock
async def test_decolecta_ruc_normaliza_una_respuesta_exitosa(
    decolecta_provider: DecolectaProvider,
) -> None:
    respx.get(f"{DECOLECTA_BASE}/v1/sunat/ruc").mock(
        return_value=httpx.Response(
            200,
            json={
                "razon_social": "Ceramica Greda SAC",
                "estado": "ACTIVO",
                "condicion": "HABIDO",
                "direccion": "Av. Siempre Viva 123",
                "ubigeo": "150101",
            },
        )
    )
    resultado = await decolecta_provider.lookup_ruc("20123456789")
    assert resultado.status is LookupStatus.SUCCESS
    assert resultado.data is not None
    assert resultado.data["business_name"] == "Ceramica Greda SAC"


@respx.mock
async def test_decolecta_timeout_es_timeout(decolecta_provider: DecolectaProvider) -> None:
    respx.get(f"{DECOLECTA_BASE}/v1/sunat/ruc").mock(side_effect=httpx.TimeoutException("timeout"))
    resultado = await decolecta_provider.lookup_ruc("20123456789")
    assert resultado.status is LookupStatus.TIMEOUT


@respx.mock
async def test_decolecta_500_es_provider_error(decolecta_provider: DecolectaProvider) -> None:
    respx.get(f"{DECOLECTA_BASE}/v1/reniec/dni").mock(return_value=httpx.Response(500))
    resultado = await decolecta_provider.lookup_dni("12345678")
    assert resultado.status is LookupStatus.PROVIDER_ERROR


# ---------------------------------------------------------------------------
# Ningun secreto viaja mas alla del proveedor
# ---------------------------------------------------------------------------
@respx.mock
async def test_el_token_no_aparece_en_el_resultado_normalizado(
    peru_provider: PeruApiProvider,
) -> None:
    respx.get(f"{PERU_BASE}/v1/dni").mock(
        return_value=httpx.Response(200, json={"nombres": "Ana", "apellido_paterno": "Torres"})
    )
    resultado = await peru_provider.lookup_dni("12345678")
    assert resultado.data is not None
    assert "token-peru-api" not in str(resultado.data)
