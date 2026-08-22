"""Catalogos de moneda, ubigeo y formatos expuestos por la API."""

from __future__ import annotations

import httpx

REFERENCE = "/api/v1/settings/reference-data"
COMPANY = "/api/v1/settings/company"
COMMERCIAL = "/api/v1/settings/commercial"
PATTERNS = "/api/v1/settings/sequence-patterns"


async def test_los_catalogos_exigen_sesion(api: httpx.AsyncClient) -> None:
    assert (await api.get(REFERENCE)).status_code == 401


async def test_operator_puede_leer_los_catalogos(
    api: httpx.AsyncClient, operator_csrf: str
) -> None:
    response = await api.get(REFERENCE)

    assert response.status_code == 200
    body = response.json()
    assert len(body["currencies"]) == 176
    assert len(body["districts"]) == 1891
    assert next(item for item in body["currencies"] if item["code"] == "PEN")["symbol"] == "S/"


async def test_los_formatos_de_sistema_conservan_la_ortografia_castellana(
    api: httpx.AsyncClient, operator_csrf: str
) -> None:
    catalog = (await api.get(REFERENCE)).json()["sequence_patterns"]

    nombres = {item["pattern"]: item["name"] for item in catalog if item["is_system"]}
    assert nombres == {
        "{PREFIX}-{YYYY}-{NUMBER}": "Prefijo - año - número",
        "{PREFIX}-{NUMBER}": "Prefijo - número",
    }


async def test_una_moneda_inexistente_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL,
        json={"version": 1, "currency_code": "ZZZ", "currency_symbol": "Z"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CATALOG_VALUE"


async def test_el_backend_deriva_el_simbolo_de_la_moneda(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.put(
        COMMERCIAL,
        json={"version": 1, "currency_code": "PEN", "currency_symbol": "X"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.text
    assert response.json()["currency_symbol"] == "S/"


async def test_un_distrito_inexistente_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMPANY,
        json={"version": 1, "ubigeo_code": "999999"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_CATALOG_VALUE"


async def test_el_backend_deriva_la_jerarquia_desde_el_ubigeo(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.put(
        COMPANY,
        json={
            "version": 1,
            "ubigeo_code": "150122",
            "district": "inventado",
            "province": "inventado",
            "department": "inventado",
            "country": "inventado",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["district"] == "MIRAFLORES"
    assert body["province"] == "LIMA"
    assert body["department"] == "LIMA"
    assert body["country"] == "Peru"


async def test_no_se_admite_un_distrito_escrito_a_mano(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.put(
        COMPANY,
        json={"version": 1, "district": "Distrito inventado"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422


async def test_admin_crea_un_formato_y_este_aparece_en_el_catalogo(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.post(
        PATTERNS,
        json={"name": "Serie mensual", "pattern": "{PREFIX}-{YYYY}{MM}-{NUMBER}"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 201, response.text
    assert response.json()["is_system"] is False
    catalog = (await api.get(REFERENCE)).json()["sequence_patterns"]
    assert any(item["name"] == "Serie mensual" for item in catalog)


async def test_un_formato_duplicado_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.post(
        PATTERNS,
        json={"name": "Otro nombre", "pattern": "{PREFIX}-{YYYY}-{NUMBER}"},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CATALOG_VALUE_EXISTS"


async def test_operator_no_puede_crear_formatos(api: httpx.AsyncClient, operator_csrf: str) -> None:
    response = await api.post(
        PATTERNS,
        json={"name": "Intento", "pattern": "{PREFIX}-{NUMBER}"},
        headers={"X-CSRF-Token": operator_csrf},
    )

    assert response.status_code == 403
