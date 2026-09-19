"""Escenario base de las pruebas de emision V2 (antes vivia en la prueba del Excel).

Fase 010J. La prueba del Excel paso a reproducir el caso canonico del Excel
final con sus entradas reales. Las pruebas de ciclo de vida siguen necesitando
el escenario sencillo de 010G —un horno chico, una pasta, un trabajador y una
tecnica— y lo importan de aqui.
"""

from __future__ import annotations

import httpx

V2 = "/api/v1/quotations-v2"
KILNS = "/api/v1/kilns"
V2_SETTINGS = "/api/v1/quoter-v2/settings"
COMMERCIAL = "/api/v1/settings/commercial"
WORKERS = "/api/v1/quoter-v2/workers"
TECHNIQUES = "/api/v1/quoter-v2/techniques"

CAPACIDAD = 17000


async def preparar_configuracion(api: httpx.AsyncClient, csrf: str) -> int:
    """Deja la casa con los numeros de la hoja «Configuracion»."""
    vigente = (await api.get(COMMERCIAL)).json()
    igv = await api.put(
        COMMERCIAL,
        json={
            # `version`, no `expected_version`: la concurrencia optimista de los
            # maestros nombra asi el campo que viaja de ida.
            "version": vigente["version"],
            "tax_percent": "18",
            "rounding_step": "0.5",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert igv.status_code == 200, igv.text

    horno = await api.post(
        KILNS,
        json={"name": "Horno chico Excel", "capacity_volume_cm3": str(CAPACIDAD)},
        headers={"X-CSRF-Token": csrf},
    )
    assert horno.status_code == 201, horno.text
    kiln_id = int(horno.json()["id"])
    for indice, tipo in enumerate(("LOW", "HIGH")):
        tarifa = await api.put(
            f"{V2_SETTINGS}/kiln-rates/{kiln_id}/{tipo}",
            json={
                "gas_cost": ("35", "70")[indice],
                "external_rate": ("200", "250")[indice],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert tarifa.status_code == 200, tarifa.text

    actual = (await api.get(V2_SETTINGS)).json()["settings"]
    ajustes = await api.put(
        V2_SETTINGS,
        json={
            "expected_version": actual["version"],
            "workday_hours": "8",
            "space_service_cost_per_day": "140",
            "administrative_cost_per_quote": "200",
            "commercial_factor_min": "2",
            "commercial_factor_default": "3",
            "commercial_factor_max": "3",
            "retail_kiln_id": kiln_id,
            "illustration_daily_rate": "88",
            "illustration_pieces_per_workday": "8",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert ajustes.status_code == 200, ajustes.text
    return kiln_id


async def preparar_maestros(api: httpx.AsyncClient, csrf: str) -> tuple[int, int, int]:
    """Una pasta, un trabajador y una tecnica: los minimos para cotizar."""
    categoria = await api.post(
        "/api/v1/categories",
        json={"name": "Cat Excel 010G", "parent_id": None},
        headers={"X-CSRF-Token": csrf},
    )
    assert categoria.status_code == 201, categoria.text
    pasta = await api.post(
        "/api/v1/products",
        json={
            "name": "Pasta Excel 010G",
            "product_type": "RAW_MATERIAL",
            "product_category_id": int(categoria.json()["id"]),
            "base_uom_code": "g",
            "purchasable": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert pasta.status_code == 201, pasta.text
    pasta_id = int(pasta.json()["id"])
    # El costo del maestro da igual: cada linea lo pisa con un acuerdo propio
    # para llegar al importe que el Excel declara.
    alta = await api.put(
        f"/api/v1/quoter-v2/materials/{pasta_id}",
        json={
            "material_kind": "BODY",
            "origin": "PURCHASE",
            "purchase_quantity": "100000",
            "purchase_cost": "100",
            "transport_cost": "30",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert alta.status_code == 200, alta.text

    trabajador = await api.post(
        WORKERS,
        json={"name": "Alfarero Excel", "worker_type": "INTERNAL", "daily_rate": "120"},
        headers={"X-CSRF-Token": csrf},
    )
    assert trabajador.status_code == 201, trabajador.text

    tecnica = await api.post(
        TECHNIQUES,
        json={
            "code": "TORNO-EXCEL-010G",
            "name": "Torno Excel",
            "default_capacity_per_workday": "50",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert tecnica.status_code == 201, tecnica.text

    return pasta_id, int(trabajador.json()["id"]), int(tecnica.json()["id"])
