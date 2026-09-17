"""Matriz RBAC local para los maestros del Cotizador V2.

El gate A2H-002 necesitaba evidencia con un usuario no-admin real del arnes
local. La fixture `operator_csrf` autentica un perfil OPERATOR sembrado en la
base de prueba: no depende de produccion ni de secretos externos.
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.conftest import TEST_EMAIL, TEST_PASSWORD
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_commercial_settings import COMMERCIAL, _payload
from tests.db.test_quoter_v2_materials_api import crear_producto


def h(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def _technique(api: httpx.AsyncClient, csrf: str, suffix: str = "rbac") -> dict[str, Any]:
    response = await api.post(
        "/api/v1/quoter-v2/techniques",
        json={
            "code": f"tec-{suffix}",
            "name": f"Tecnica {suffix}",
            "default_capacity_per_workday": "40",
        },
        headers=h(csrf),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _worker(
    api: httpx.AsyncClient, csrf: str, technique_id: int, suffix: str = "rbac"
) -> dict[str, Any]:
    response = await api.post(
        "/api/v1/quoter-v2/workers",
        json={
            "name": f"Trabajador {suffix}",
            "worker_type": "INTERNAL",
            "daily_rate": "120",
            "technique_ids": [technique_id],
        },
        headers=h(csrf),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _kiln(api: httpx.AsyncClient, csrf: str) -> dict[str, Any]:
    response = await api.post(
        "/api/v1/kilns",
        json={"name": "Horno RBAC V2", "capacity_volume_cm3": "100000"},
        headers=h(csrf),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def _extra(api: httpx.AsyncClient, csrf: str, suffix: str = "rbac") -> dict[str, Any]:
    response = await api.post(
        "/api/v1/quoter-v2/extras",
        json={"name": f"Molde RBAC {suffix}", "unit": "unidad", "unit_cost": "12"},
        headers=h(csrf),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def test_a2h002_admin_puede_gestionar_maestros_v2(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    commercial = await api.put(
        COMMERCIAL,
        json=_payload(1, tax_percent=18, currency_code="PEN", currency_symbol="S/"),
        headers=h(admin_csrf),
    )
    assert commercial.status_code == 200, commercial.text

    technique = await _technique(api, admin_csrf, "admin")
    worker = await _worker(api, admin_csrf, technique["id"], "admin")
    assert technique["id"] in worker["technique_ids"]

    kiln = await _kiln(api, admin_csrf)
    rate = await api.post(
        f"/api/v1/kilns/{kiln['id']}/rates",
        json={"firing_type": "LOW", "rate": "100"},
        headers=h(admin_csrf),
    )
    assert rate.status_code == 201, rate.text

    pieza = await crear_producto(
        api, admin_csrf, "Pieza RBAC procesos", product_type="FINISHED_PRODUCT", purchasable=False
    )
    product_techniques = await api.put(
        f"/api/v1/quoter-v2/products/{pieza['id']}/techniques",
        json={"technique_ids": [technique["id"]]},
        headers=h(admin_csrf),
    )
    assert product_techniques.status_code == 200, product_techniques.text

    extra = await _extra(api, admin_csrf, "admin")
    assert extra["unit_cost"] == "12"


async def test_a2h002_operator_no_gestiona_maestros_v2(
    api: httpx.AsyncClient, admin_csrf: str, operator_csrf: str
) -> None:
    admin_csrf = await authenticate(api, email=TEST_EMAIL, password=TEST_PASSWORD)
    technique = await _technique(api, admin_csrf, "op")
    worker = await _worker(api, admin_csrf, technique["id"], "op")
    kiln = await _kiln(api, admin_csrf)
    pieza = await crear_producto(
        api, admin_csrf, "Pieza RBAC operador", product_type="FINISHED_PRODUCT", purchasable=False
    )
    extra = await _extra(api, admin_csrf, "operator")
    operator_csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

    denied: list[tuple[str, str, dict[str, Any] | None]] = [
        ("PUT", COMMERCIAL, _payload(1, tax_percent=18)),
        ("GET", "/api/v1/quoter-v2/workers", None),
        (
            "POST",
            "/api/v1/quoter-v2/workers",
            {"name": "No pasa", "worker_type": "INTERNAL", "daily_rate": "1"},
        ),
        (
            "PUT",
            f"/api/v1/quoter-v2/workers/{worker['id']}",
            {"expected_version": worker["version"], "daily_rate": "99"},
        ),
        ("GET", "/api/v1/quoter-v2/techniques", None),
        (
            "POST",
            "/api/v1/quoter-v2/techniques",
            {"code": "no", "name": "No pasa", "default_capacity_per_workday": "1"},
        ),
        (
            "PUT",
            f"/api/v1/quoter-v2/techniques/{technique['id']}",
            {"expected_version": technique["version"], "name": "No pasa"},
        ),
        (
            "POST",
            f"/api/v1/kilns/{kiln['id']}/rates",
            {"firing_type": "LOW", "rate": "1"},
        ),
        ("GET", f"/api/v1/quoter-v2/products/{pieza['id']}/techniques", None),
        (
            "PUT",
            f"/api/v1/quoter-v2/products/{pieza['id']}/techniques",
            {"technique_ids": [technique["id"]]},
        ),
        # La pagina de configuracion del Cotizador V2: leerla ya revela las
        # tarifas del taller, asi que tambien es superficie de administracion.
        ("GET", "/api/v1/quoter-v2/settings", None),
        (
            "PUT",
            "/api/v1/quoter-v2/settings",
            {"expected_version": 1, "administrative_cost_per_quotation": "999"},
        ),
        (
            "PUT",
            f"/api/v1/quoter-v2/settings/kiln-rates/{kiln['id']}/LOW",
            {"gas_cost": "1", "external_rate": "1"},
        ),
        ("GET", "/api/v1/quoter-v2/extras", None),
        ("POST", "/api/v1/quoter-v2/extras", {"name": "No pasa", "unit_cost": "1"}),
        (
            "PUT",
            f"/api/v1/quoter-v2/extras/{extra['id']}",
            {"expected_version": extra["version"], "unit_cost": "2"},
        ),
    ]

    for method, url, payload in denied:
        response = await api.request(method, url, json=payload, headers=h(operator_csrf))
        assert response.status_code == 403, f"{method} {url}: {response.text}"

    assert (await api.get(COMMERCIAL)).status_code == 200
    assert (await api.get("/api/v1/kilns", headers=h(operator_csrf))).status_code == 200
    rates = await api.get(f"/api/v1/kilns/{kiln['id']}/rates", headers=h(operator_csrf))
    assert rates.status_code == 200
