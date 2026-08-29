"""Fase 009C.1 — el codigo de un maestro de costos lo emite el backend.

Tecnicas, adicionales y otros gastos exigian el codigo en el payload y el
usuario lo escribia a mano en el formulario. Eso convierte una identidad
interna en un dato de interfaz: dos instalaciones numeran distinto, y editar
una tecnica permitia cambiarle el codigo por el de otra.

Aqui se fija lo contrario: el backend lo genera, garantiza unicidad y lo
conserva al actualizar.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.db.test_quotation_builder_api import head

TECHNIQUES = "/api/v1/techniques"
ADDITIONALS = "/api/v1/additionals"
OTHER_COSTS = "/api/v1/other-costs"


def _technique(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": "Tecnica 009C1",
        "unit_price": "12.50",
        "formula_type": "ONE_FACTOR",
        "factor_1": "1.5",
        "active": True,
    }
    base.update(overrides)
    return base


def _additional(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": "Adicional 009C1",
        "unit_price": "8.00",
        "formula_type": "PIECE_QUANTITY",
        "factor_1": "2",
        "active": True,
    }
    base.update(overrides)
    return base


def _other_cost(**overrides: Any) -> dict[str, Any]:
    base = {
        "name": "Otro gasto 009C1",
        "unit_price": "30.00",
        "calculation_type": "FIXED",
        "active": True,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("url", "payload", "prefix"),
    [
        (TECHNIQUES, _technique(), "TEC-"),
        (ADDITIONALS, _additional(), "ADI-"),
        (OTHER_COSTS, _other_cost(), "OTH-"),
    ],
)
@pytest.mark.asyncio
async def test_el_backend_emite_el_codigo_sin_que_el_cliente_lo_mande(
    api: httpx.AsyncClient, admin_csrf: str, url: str, payload: dict[str, Any], prefix: str
) -> None:
    """El alta no lleva codigo y aun asi el maestro nace con el suyo."""
    assert "code" not in payload
    response = await api.post(url, json=payload, headers=head(admin_csrf))
    assert response.status_code == 201, response.text
    code = response.json()["code"]
    assert code.startswith(prefix), code
    # Continua la numeracion existente: prefijo y tres digitos.
    assert code[len(prefix) :].isdigit()


@pytest.mark.parametrize(
    ("url", "payload", "prefix"),
    [
        (TECHNIQUES, _technique(), "TEC-"),
        (ADDITIONALS, _additional(), "ADI-"),
        (OTHER_COSTS, _other_cost(), "OTH-"),
    ],
)
@pytest.mark.asyncio
async def test_el_codigo_que_manda_el_cliente_se_ignora(
    api: httpx.AsyncClient, admin_csrf: str, url: str, payload: dict[str, Any], prefix: str
) -> None:
    """USER_AUTHORED_INTERNAL_CODES: cero.

    El campo se sigue aceptando para no romper a un cliente ya desplegado que
    todavia lo envie, pero no llega a la base: manda el backend.
    """
    response = await api.post(
        url, json={**payload, "code": "ELEGIDO-POR-EL-USUARIO"}, headers=head(admin_csrf)
    )
    assert response.status_code == 201, response.text
    assert response.json()["code"] != "ELEGIDO-POR-EL-USUARIO"
    assert response.json()["code"].startswith(prefix)


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        (TECHNIQUES, _technique()),
        (ADDITIONALS, _additional()),
        (OTHER_COSTS, _other_cost()),
    ],
)
@pytest.mark.asyncio
async def test_actualizar_conserva_el_codigo(
    api: httpx.AsyncClient, admin_csrf: str, url: str, payload: dict[str, Any]
) -> None:
    """El codigo es identidad: editar el maestro no lo cambia."""
    created = await api.post(url, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text
    row_id = created.json()["id"]
    original = created.json()["code"]

    updated = await api.put(
        f"{url}/{row_id}",
        json={**payload, "name": "Renombrado", "code": "INTENTO-DE-CAMBIO"},
        headers=head(admin_csrf),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Renombrado"
    assert updated.json()["code"] == original, "editar no debe cambiar el codigo"


@pytest.mark.asyncio
async def test_altas_sucesivas_no_repiten_codigo(api: httpx.AsyncClient, admin_csrf: str) -> None:
    """La unicidad la garantiza el backend, no la disciplina de quien teclea."""
    codes = []
    for i in range(3):
        response = await api.post(
            TECHNIQUES, json=_technique(name=f"Serie {i}"), headers=head(admin_csrf)
        )
        assert response.status_code == 201, response.text
        codes.append(response.json()["code"])
    assert len(set(codes)) == len(codes), codes
