"""Fase 009E — el motor comercial extremo a extremo.

La matematica pura esta en tests/unit/test_pricing_math.py. Aqui se comprueba
lo que solo se ve con la cotizacion entera: que los costos fijos son de la
COTIZACION y no de la linea, que el reparto reconcilia, que el total es la
suma de las lineas sin volver a redondear, y que una cotizacion confirmada no
se mueve cuando cambia la configuracion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head

OTHER_COSTS = "/api/v1/other-costs"
COMMERCIAL = "/api/v1/settings/commercial"

#: Los tres costos fijos del taller. Suman 320, el caso del enunciado.
FIXED_COSTS = (
    ("Alquiler / uso de espacio", "110"),
    ("Servicios", "10"),
    ("Costo administrativo", "200"),
)
TOTAL_FIXED = Decimal(320)


async def _seed_fixed_costs(api: httpx.AsyncClient, csrf: str) -> None:
    """Crea los maestros de costo fijo. El tipo ya no es autoridad de calculo."""
    for name, price in FIXED_COSTS:
        response = await api.post(
            OTHER_COSTS,
            json={"name": name, "unit_price": price, "calculation_type": "FIXED"},
            headers=head(csrf),
        )
        assert response.status_code == 201, response.text


def _with_policy(payload: dict[str, Any], **policy: Any) -> dict[str, Any]:
    return {**payload, **policy}


# ---------------------------------------------------------------------------
# Costos fijos: una vez por cotizacion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_los_costos_fijos_no_se_duplican_por_producto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """FIXED_COST_DUPLICATED_PER_PRODUCT: NO.

    Es la regresion del defecto que 009E corrige. Antes, cada linea cobraba el
    alquiler y el administrativo enteros: dos productos en la misma quema
    pagaban el taller dos veces. Con el comportamiento antiguo la suma daria
    640 y este test fallaria.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    await _seed_fixed_costs(api, admin_csrf)

    respuesta = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    body = respuesta.json()

    assert len(body["items"]) == 2
    asignado = [Decimal(item["fixed_cost_allocation"]) for item in body["items"]]
    assert sum(asignado) == TOTAL_FIXED, "el taller se cobra una vez, no una por producto"
    assert Decimal(body["total_fixed_cost"]) == TOTAL_FIXED


@pytest.mark.asyncio
async def test_el_costo_fijo_no_se_multiplica_por_los_dias(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PER_DAY_FIXED_COST_MULTIPLICATION: NO.

    `calculation_type` dejo de ser autoridad de calculo. Un maestro marcado
    PER_DAY suma su importe UNA vez, no una por cada dia del lote: antes, un
    lote de doce dias facturaba doce dias de servicios en cada linea.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    creado = await api.post(
        OTHER_COSTS,
        json={"name": "Servicios", "unit_price": "10", "calculation_type": "PER_DAY"},
        headers=head(admin_csrf),
    )
    assert creado.status_code == 201, creado.text

    respuesta = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    body = respuesta.json()

    assert Decimal(body["total_fixed_cost"]) == Decimal(10)
    assert sum(Decimal(item["fixed_cost_allocation"]) for item in body["items"]) == Decimal(10)
    # Y el lote dura varios dias, asi que el 10 no es una casualidad de un dia.
    assert any(item["total_days"] > 1 for item in body["items"])


@pytest.mark.asyncio
async def test_el_reparto_es_proporcional_al_costo_factorado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """FIXED_COST_ALLOCATION: el peso es el costo factorado de cada linea."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    await _seed_fixed_costs(api, admin_csrf)

    respuesta = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    body = respuesta.json()
    lineas = body["items"]

    total_factorado = sum(Decimal(item["factored_cost"]) for item in lineas)
    for item in lineas:
        esperado = (TOTAL_FIXED * Decimal(item["factored_cost"]) / total_factorado).quantize(
            Decimal("0.01")
        )
        # La ultima linea absorbe el residuo de la cuantizacion, asi que se
        # admite un centimo de diferencia; la SUMA sigue siendo exacta.
        assert abs(Decimal(item["fixed_cost_allocation"]) - esperado) <= Decimal("0.01")
    assert sum(Decimal(item["fixed_cost_allocation"]) for item in lineas) == TOTAL_FIXED


# ---------------------------------------------------------------------------
# Factor, margen, IGV y redondeo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_factor_de_produccion_por_defecto_es_tres(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DEFAULT_PRODUCTION_FACTOR + PRODUCTION_FACTOR_DOUBLE_COUNT: 0."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)

    body = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()

    assert Decimal(body["production_factor"]) == Decimal(3)
    for item in body["items"]:
        # El factor entra UNA vez: factorado = tecnico x 3, exacto.
        assert Decimal(item["factored_cost"]) == Decimal(item["technical_cost"]) * Decimal(3)


@pytest.mark.asyncio
async def test_la_cotizacion_puede_sobreescribir_el_factor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTE_FACTOR_OVERRIDE, y sobrevive a guardar y reabrir."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    body = _with_policy(payload, production_factor="4")

    creada = await api.post(BUILDER, json=body, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    assert Decimal(creada.json()["production_factor"]) == Decimal(4)

    reabierta = await api.get(f"{BUILDER}/{creada.json()['id']}", headers=head(admin_csrf))
    assert Decimal(reabierta.json()["production_factor"]) == Decimal(4)
    for item in reabierta.json()["items"]:
        assert Decimal(item["production_factor"]) == Decimal(4)


@pytest.mark.asyncio
async def test_el_redondeo_contractual_sube_y_admite_los_dos_pasos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CEILING_GROSS_ROUNDING + ROUNDING_STEP.

    El paso lo fija Configuracion, NO la cotizacion: si cada cotizacion pudiera
    mandar el suyo, la politica de precios de la empresa se saltaria pieza a
    pieza. Por eso aqui se cambia el ajuste y no el payload.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)

    for version, paso in enumerate(("0.50", "1.00"), start=1):
        cambio = await api.put(
            COMMERCIAL,
            json={"version": version, "rounding_step": paso},
            headers=head(admin_csrf),
        )
        assert cambio.status_code == 200, cambio.text
        respuesta = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()

        assert Decimal(cuerpo["rounding_step"]) == Decimal(paso)
        for item in cuerpo["items"]:
            bruto = Decimal(item["final_gross_unit"])
            assert bruto % Decimal(paso) == 0, f"{bruto} no es multiplo de {paso}"
            # Nunca baja: el precio final es >= al crudo.
            assert bruto >= Decimal(item["raw_gross_unit"]).quantize(Decimal("0.01"))
            assert Decimal(item["rounding_adjustment_unit"]) >= 0


@pytest.mark.asyncio
async def test_neto_mas_igv_es_exactamente_el_bruto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """RECONSTRUCT_NET_TAX: el bruto redondeado manda y el neto se deriva."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)

    body = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()

    for item in body["items"]:
        neto = Decimal(item["final_net_unit"])
        igv = Decimal(item["final_tax_unit"])
        assert neto + igv == Decimal(item["final_gross_unit"])
        assert Decimal(item["line_total_net"]) + Decimal(item["line_total_tax"]) == Decimal(
            item["line_total_gross"]
        )


@pytest.mark.asyncio
async def test_cada_linea_lleva_su_propio_margen(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PER_PRODUCT_MARKUP: 100 % y 50 % en la misma cotizacion."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    # Sin precio manual: un precio escrito a mano gana sobre el margen, asi que
    # dejarlo puesto mediria el override, no el markup.
    items = [{**item, "commercial_sale_unit_price": None} for item in payload["items"]]
    items[0]["markup_percent"] = "100"
    items[1]["markup_percent"] = "50"

    body = (
        await api.post(
            f"{BUILDER}/preview", json={**payload, "items": items}, headers=head(admin_csrf)
        )
    ).json()

    a, b = body["items"]
    assert Decimal(a["markup_percent"]) == Decimal(100)
    assert Decimal(b["markup_percent"]) == Decimal(50)
    assert Decimal(a["raw_net_unit"]) == Decimal(a["commercial_base_unit_cost"]) * Decimal(2)
    assert Decimal(b["raw_net_unit"]) == Decimal(b["commercial_base_unit_cost"]) * Decimal("1.5")


# ---------------------------------------------------------------------------
# Totales
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_total_es_la_suma_de_las_lineas_sin_segundo_redondeo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NO_SECOND_TOTAL_ROUNDING + MULTIPRODUCT_TOTAL + autoridad del backend."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    await _seed_fixed_costs(api, admin_csrf)

    body = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()

    neto = sum(Decimal(item["line_total_net"]) for item in body["items"])
    igv = sum(Decimal(item["line_total_tax"]) for item in body["items"])
    bruto = sum(Decimal(item["line_total_gross"]) for item in body["items"])

    # El backend expone los tres con semantica unica: el frontend no elige.
    assert Decimal(body["quotation_net_total"]) == neto
    assert Decimal(body["quotation_tax_total"]) == igv
    assert Decimal(body["quotation_gross_total"]) == bruto
    assert neto + igv == bruto


# ---------------------------------------------------------------------------
# Confirmada: inmutable
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_cotizacion_confirmada_no_se_mueve(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_COMMERCIAL_IMMUTABLE.

    Se confirma con el IGV al 18 %, se cambia al 10 %, y la cotizacion sigue
    diciendo lo mismo. Un precio comprometido no se recalcula al leerlo.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    await _seed_fixed_costs(api, admin_csrf)

    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]

    reabierta = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    confirmada = await api.post(
        f"{BUILDER}/{quotation_id}/confirm",
        json={"expected_updated_at": reabierta.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    congelado = confirmada.json()

    cambio = await api.put(
        COMMERCIAL,
        json={"version": 1, "tax_percent": "10", "estimated_glaze_percent": "15"},
        headers=head(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    despues = (await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))).json()

    assert despues["quotation_gross_total"] == congelado["quotation_gross_total"]
    assert despues["quotation_net_total"] == congelado["quotation_net_total"]
    for antes_item, despues_item in zip(congelado["items"], despues["items"], strict=True):
        assert despues_item["final_gross_unit"] == antes_item["final_gross_unit"]
        assert despues_item["fixed_cost_allocation"] == antes_item["fixed_cost_allocation"]
        assert despues_item["production_factor"] == antes_item["production_factor"]


@pytest.mark.asyncio
async def test_el_precio_manual_manda_pero_no_se_salta_el_redondeo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El precio escrito a mano sustituye al del margen, no al contrato.

    Es una eleccion del usuario y gana sobre el markup. Pero el bruto sigue
    teniendo que ser redondo: dejar que un precio manual se saltara el
    redondeo pondria en el documento una cifra con centimos arbitrarios, que
    es justo lo que 009E vino a quitar.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    items = [dict(item) for item in payload["items"]]
    items[0]["commercial_sale_unit_price"] = "8.50"

    body = (
        await api.post(
            f"{BUILDER}/preview", json={**payload, "items": items}, headers=head(admin_csrf)
        )
    ).json()
    linea = body["items"][0]

    # El neto crudo es exactamente lo que tecleo el usuario...
    assert Decimal(linea["raw_net_unit"]) == Decimal("8.50")
    # ...y el bruto final sigue siendo multiplo del paso contractual.
    assert Decimal(linea["final_gross_unit"]) % Decimal("0.50") == 0
    assert Decimal(linea["final_gross_unit"]) >= Decimal(linea["raw_gross_unit"]).quantize(
        Decimal("0.01")
    )
    assert Decimal(linea["final_net_unit"]) + Decimal(linea["final_tax_unit"]) == Decimal(
        linea["final_gross_unit"]
    )


# ---------------------------------------------------------------------------
# La politica sale de Configuracion (Fase 009E / Alembic 0016)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_factor_por_defecto_sale_de_la_configuracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DEFAULT_FACTOR_FROM_SETTINGS + DRAFT_DEFAULT_FACTOR_CHANGE_RECALCULATES."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)

    antes = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()
    assert Decimal(antes["production_factor"]) == Decimal(3)

    cambio = await api.put(
        COMMERCIAL,
        json={"version": 1, "production_factor_default": "4"},
        headers=head(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    despues = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()
    assert Decimal(despues["production_factor"]) == Decimal(4)
    for item in despues["items"]:
        assert Decimal(item["factored_cost"]) == Decimal(item["technical_cost"]) * Decimal(4)


@pytest.mark.asyncio
async def test_el_override_de_la_cotizacion_gana_al_default(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTE_FACTOR_OVERRIDE_WINS + DRAFT_OVERRIDE_SURVIVES_DEFAULT_CHANGE."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    body = _with_policy(payload, production_factor="2.5")

    creada = await api.post(BUILDER, json=body, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    assert Decimal(creada.json()["production_factor"]) == Decimal("2.5")

    # Cambiar el default NO puede mover un borrador que eligio su propio factor.
    cambio = await api.put(
        COMMERCIAL,
        json={"version": 1, "production_factor_default": "4"},
        headers=head(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    reabierta = await api.get(f"{BUILDER}/{creada.json()['id']}", headers=head(admin_csrf))
    assert Decimal(reabierta.json()["production_factor"]) == Decimal("2.5")

    recalculada = await api.put(
        f"{BUILDER}/{creada.json()['id']}",
        json={**body, "expected_updated_at": reabierta.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert recalculada.status_code == 200, recalculada.text
    assert Decimal(recalculada.json()["production_factor"]) == Decimal("2.5")


@pytest.mark.asyncio
async def test_cambiar_el_paso_de_redondeo_recalcula_el_borrador(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ROUNDING_SETTING_CHANGE_RECALCULATES."""
    payload, _products = await _complete_payload(api, admin_csrf, db_session)

    antes = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()
    assert Decimal(antes["rounding_step"]) == Decimal("0.50")

    cambio = await api.put(
        COMMERCIAL, json={"version": 1, "rounding_step": "1.00"}, headers=head(admin_csrf)
    )
    assert cambio.status_code == 200, cambio.text

    despues = (await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))).json()
    assert Decimal(despues["rounding_step"]) == Decimal("1.00")
    for item in despues["items"]:
        assert Decimal(item["final_gross_unit"]) % Decimal(1) == 0


@pytest.mark.asyncio
async def test_un_gasto_manual_no_puede_cobrar_dos_veces_los_fijos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MANUAL_SELECTION_OF_AUTO_FIXED_COSTS: 0 + AUTO_FIXED_COSTS_APPLIED_ONCE.

    Los maestros de costo fijo se aplican automaticamente. Si ademas se
    pudieran seleccionar por linea, el alquiler se cobraria dos veces. La
    seleccion manual se acepta por compatibilidad de despliegue pero se
    ignora: el importe no cambia.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    await _seed_fixed_costs(api, admin_csrf)
    maestros = (await api.get(f"{OTHER_COSTS}?limit=50")).json()["items"]

    sin_seleccion = (
        await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    ).json()

    items = [
        {
            **item,
            "other_costs": [
                {"other_cost_id": maestro["id"], "sort_order": index}
                for index, maestro in enumerate(maestros)
            ],
        }
        for item in payload["items"]
    ]
    con_seleccion = (
        await api.post(
            f"{BUILDER}/preview", json={**payload, "items": items}, headers=head(admin_csrf)
        )
    ).json()

    assert Decimal(con_seleccion["total_fixed_cost"]) == TOTAL_FIXED
    assert con_seleccion["quotation_gross_total"] == sin_seleccion["quotation_gross_total"]


@pytest.mark.asyncio
async def test_un_maestro_desactivado_no_entra_en_los_fijos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El retiro del «Factor» legacy: desactivarlo lo saca del total.

    PRODUCTION_FACTOR_DOUBLE_COUNT: 0 — el factor canonico es el multiplicador,
    y un maestro llamado «Factor» sumando ademas su importe lo cobraria dos
    veces con dos significados distintos.
    """
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    await _seed_fixed_costs(api, admin_csrf)
    legacy = await api.post(
        OTHER_COSTS,
        json={"name": "Factor", "unit_price": "3", "calculation_type": "PER_PIECE"},
        headers=head(admin_csrf),
    )
    assert legacy.status_code == 201, legacy.text
    legacy_id = legacy.json()["id"]

    con_legacy = (
        await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    ).json()
    assert Decimal(con_legacy["total_fixed_cost"]) == TOTAL_FIXED + Decimal(3)

    baja = await api.put(
        f"{OTHER_COSTS}/{legacy_id}",
        json={
            "name": "Factor",
            "unit_price": "3",
            "calculation_type": "PER_PIECE",
            "active": False,
        },
        headers=head(admin_csrf),
    )
    assert baja.status_code == 200, baja.text

    sin_legacy = (
        await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    ).json()
    assert Decimal(sin_legacy["total_fixed_cost"]) == TOTAL_FIXED
