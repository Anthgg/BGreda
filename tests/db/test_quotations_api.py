from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockMovement
from app.models.masters import Product
from app.models.quotations import Quotation, QuotationProductPriceUpdate
from tests.db.conftest import (
    OPERATOR_EMAIL,
    OPERATOR_PASSWORD,
    TEST_EMAIL,
    TEST_PASSWORD,
    authenticate,
)
from tests.db.test_firings_api import (
    FIRINGS,
    head,
    hoja_de_referencia,
    hornos_de_referencia,
)
from tests.db.test_masters_api import create_category, create_product

QUOTATIONS = "/api/v1/quotations"
RECIPES = "/api/v1/recipes"
TECHNIQUES = "/api/v1/techniques"


async def _finished_product_and_recipe(
    api: httpx.AsyncClient, csrf: str, suffix: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    category = await create_category(api, csrf, f"Cotizador QA{suffix}")
    finished_response = await create_product(
        api,
        csrf,
        product_category_id=category["id"],
        product_type="FINISHED_PRODUCT",
        name=f"Plato palta QA{suffix}",
        base_uom_code="unit",
        sellable=True,
        sale_price="459",
    )
    assert finished_response.status_code == 201, finished_response.text
    finished = finished_response.json()

    material_response = await create_product(
        api,
        csrf,
        product_category_id=category["id"],
        name=f"Pasta local QA{suffix}",
        product_type="RAW_MATERIAL",
        base_uom_code="g",
        cost="0.5",
    )
    assert material_response.status_code == 201, material_response.text
    material = material_response.json()

    # La receta describe un **material preparado**, no la pieza. Que la
    # cotizacion de una pieza terminada pueda elegirla es justamente lo que se
    # comprueba: son dominios independientes.
    prepared_response = await create_product(
        api,
        csrf,
        product_category_id=category["id"],
        name=f"Barniz local QA{suffix}",
        product_type="PREPARED_MATERIAL",
        base_uom_code="g",
    )
    assert prepared_response.status_code == 201, prepared_response.text
    prepared = prepared_response.json()

    recipe_response = await api.post(
        RECIPES,
        json={
            "product_id": prepared["id"],
            "name": f"Formula barniz QA{suffix}",
            "lines": [
                {
                    "component_product_id": material["id"],
                    "component_type": "BASE",
                    "percentage": "100",
                    "sort_order": 0,
                }
            ],
            "active": True,
            "activate_immediately": True,
        },
        headers=head(csrf),
    )
    assert recipe_response.status_code == 201, recipe_response.text
    return finished, recipe_response.json()


async def _confirmed_firing_line(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    product_id: int,
) -> dict[str, Any]:
    small, large = await hornos_de_referencia(api, csrf, db_session)
    payload = hoja_de_referencia(small, large)
    payload["lines"][0]["product_id"] = product_id
    draft_response = await api.post(FIRINGS, json=payload, headers=head(csrf))
    assert draft_response.status_code == 201, draft_response.text
    draft = draft_response.json()
    confirm_response = await api.post(f"{FIRINGS}/{draft['id']}/confirm", headers=head(csrf))
    assert confirm_response.status_code == 200, confirm_response.text
    return confirm_response.json()["lines"][0]


def _quote_payload(
    product: dict[str, Any],
    recipe: dict[str, Any],
    firing_line: dict[str, Any],
    *,
    technique_id: int | None = None,
) -> dict[str, Any]:
    techniques: list[dict[str, Any]] = []
    if technique_id is not None:
        techniques.append({"technique_id": technique_id, "quantity": 19})
    current_version = recipe["current_version"]
    return {
        "product_id": product["id"],
        "quantity": 19,
        "recipe_id": recipe["id"],
        "recipe_version_id": current_version["id"],
        "firing_line_id": firing_line["id"],
        # Explicito: ya no existe un valor por omision de un gramo.
        "material_grams_per_piece": "1",
        "materials_applied": "11.58",
        "techniques": techniques,
        "additionals": [],
        "days_adjustment": 0,
        "waiting_days": 0,
        "other_costs": [],
        "commercial_factor": "2",
    }


async def test_calculate_is_pure_and_uses_decimal_strings(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    before_quotes = await db_session.scalar(select(func.count()).select_from(Quotation))
    before_stock = await db_session.scalar(select(func.count()).select_from(StockMovement))

    response = await api.post(
        f"{QUOTATIONS}/calculate",
        json=_quote_payload(product, recipe, firing_line),
        headers=head(admin_csrf),
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert Decimal(data["materials_calculated"]) == Decimal("9.5")
    assert Decimal(data["materials_applied"]) == Decimal("11.58")
    assert Decimal(data["firing_cost"]) == Decimal(firing_line["allocated_cost"])
    # El IGV si tiene regla: la cotizacion se emite neta y el impuesto se anade
    # encima, de modo que el documento entregado muestre las dos cifras.
    assert data["igv_rule_source"] == "FOUND"
    assert data["discount_rule_source"] == "NOT_FOUND"
    neto = Decimal(data["calculated_total"])
    tasa = Decimal(data["tax_percentage"])
    assert Decimal(data["tax_amount"]) == neto * tasa / Decimal(100)
    assert Decimal(data["total_with_tax"]) == neto + Decimal(data["tax_amount"])
    assert Decimal(data["unit_price_with_tax"]) == Decimal(data["total_with_tax"]) / Decimal(
        data["quantity"]
    )
    assert await db_session.scalar(select(func.count()).select_from(Quotation)) == before_quotes
    assert await db_session.scalar(select(func.count()).select_from(StockMovement)) == before_stock


async def test_draft_confirm_freezes_sources_and_updates_price_explicitly(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    create_response = await api.post(
        QUOTATIONS,
        json=_quote_payload(product, recipe, firing_line),
        headers=head(admin_csrf),
    )
    assert create_response.status_code == 201, create_response.text
    draft = create_response.json()
    assert draft["status"] == "DRAFT"
    assert draft["code"].startswith("CTZ-")

    confirm_response = await api.post(
        f"{QUOTATIONS}/{draft['id']}/confirm",
        json={"accept_source_changes": False},
        headers=head(admin_csrf),
    )
    assert confirm_response.status_code == 200, confirm_response.text
    confirmed = confirm_response.json()
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["firing_snapshot"]["firing_line_id"] == firing_line["id"]
    assert Decimal(confirmed["current_sale_price_snapshot"]) == Decimal("459")

    immutable_response = await api.put(
        f"{QUOTATIONS}/{draft['id']}",
        json={
            **_quote_payload(product, recipe, firing_line),
            "expected_source_fingerprint": confirmed["source_fingerprint"],
            "accept_source_changes": False,
        },
        headers=head(admin_csrf),
    )
    assert immutable_response.status_code == 409
    assert immutable_response.json()["error"]["code"] == "QUOTATION_NOT_EDITABLE"

    price_response = await api.post(
        f"{QUOTATIONS}/{draft['id']}/update-product-price",
        headers=head(admin_csrf),
    )
    assert price_response.status_code == 200, price_response.text
    updated = price_response.json()
    assert Decimal(updated["old_price"]) == Decimal("459")
    assert Decimal(updated["new_price"]) == Decimal(confirmed["calculated_unit_price"])
    events = await db_session.scalar(select(func.count()).select_from(QuotationProductPriceUpdate))
    assert events == 1


async def test_confirmation_blocks_missing_recipe_and_firing(
    api: httpx.AsyncClient,
    admin_csrf: str,
) -> None:
    category = await create_category(api, admin_csrf, "Sin fuentes QA")
    product_response = await create_product(
        api,
        admin_csrf,
        product_category_id=category["id"],
        product_type="FINISHED_PRODUCT",
        name="Pieza sin fuentes",
        base_uom_code="unit",
    )
    product = product_response.json()
    create_response = await api.post(
        QUOTATIONS,
        json={
            "product_id": product["id"],
            "quantity": 1,
            "materials_applied": "0",
            "other_costs": [],
        },
        headers=head(admin_csrf),
    )
    assert create_response.status_code == 201, create_response.text
    draft = create_response.json()
    assert set(draft["warnings"]) >= {"RECIPE_REQUIRED", "FIRING_LINE_REQUIRED"}

    response = await api.post(
        f"{QUOTATIONS}/{draft['id']}/confirm",
        json={"accept_source_changes": False},
        headers=head(admin_csrf),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RECIPE_REQUIRED"


async def test_source_change_requires_explicit_acceptance(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    technique_response = await api.post(
        TECHNIQUES,
        json={
            "code": "TORNO_QA",
            "name": "Torno QA",
            "unit_price": "220",
            "formula_type": "TWO_FACTORS",
            "factor_1": "50",
            "factor_2": "100",
            "active": True,
        },
        headers=head(admin_csrf),
    )
    assert technique_response.status_code == 201, technique_response.text
    technique = technique_response.json()
    quote_response = await api.post(
        QUOTATIONS,
        json=_quote_payload(product, recipe, firing_line, technique_id=technique["id"]),
        headers=head(admin_csrf),
    )
    assert quote_response.status_code == 201, quote_response.text
    quote = quote_response.json()

    update_response = await api.put(
        f"{TECHNIQUES}/{technique['id']}",
        json={
            "code": technique["code"],
            "name": technique["name"],
            "unit_price": "221",
            "formula_type": technique["formula_type"],
            "factor_1": technique["factor_1"],
            "factor_2": technique["factor_2"],
            "active": True,
            "notes": None,
        },
        headers=head(admin_csrf),
    )
    assert update_response.status_code == 200, update_response.text

    blocked = await api.post(
        f"{QUOTATIONS}/{quote['id']}/confirm",
        json={"accept_source_changes": False},
        headers=head(admin_csrf),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "SOURCE_CHANGED"

    accepted = await api.post(
        f"{QUOTATIONS}/{quote['id']}/confirm",
        json={"accept_source_changes": True},
        headers=head(admin_csrf),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "CONFIRMED"
    assert Decimal(accepted.json()["techniques"][0]["unit_price_snapshot"]) == Decimal("221")


async def test_operator_can_calculate_but_cannot_persist(
    api: httpx.AsyncClient,
) -> None:
    admin_csrf = await authenticate(api, email=TEST_EMAIL, password=TEST_PASSWORD)
    category = await create_category(api, admin_csrf, "Roles cotizador")
    product_response = await create_product(
        api,
        admin_csrf,
        product_category_id=category["id"],
        product_type="FINISHED_PRODUCT",
        name="Pieza roles",
        base_uom_code="unit",
    )
    payload = {
        "product_id": product_response.json()["id"],
        "quantity": 2,
        "materials_applied": "10",
        "other_costs": [],
    }
    operator_csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
    calculate_response = await api.post(
        f"{QUOTATIONS}/calculate", json=payload, headers=head(operator_csrf)
    )
    assert calculate_response.status_code == 200, calculate_response.text

    create_response = await api.post(QUOTATIONS, json=payload, headers=head(operator_csrf))
    assert create_response.status_code == 403
    assert create_response.json()["error"]["code"] == "AUTH_INSUFFICIENT_ROLE"


async def test_wrong_product_firing_line_is_rejected(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    first_product, _ = await _finished_product_and_recipe(api, admin_csrf)
    wrong_line = await _confirmed_firing_line(api, admin_csrf, db_session, first_product["id"])
    second_product, second_recipe = await _finished_product_and_recipe(api, admin_csrf, " Dos")

    response = await api.post(
        f"{QUOTATIONS}/calculate",
        json=_quote_payload(second_product, second_recipe, wrong_line),
        headers=head(admin_csrf),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "FIRING_LINE_PRODUCT_MISMATCH"


async def test_draft_update_cancel_and_duplicate_issue_new_code(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    create_response = await api.post(
        QUOTATIONS,
        json=_quote_payload(product, recipe, firing_line),
        headers=head(admin_csrf),
    )
    original = create_response.json()
    updated_response = await api.put(
        f"{QUOTATIONS}/{original['id']}",
        json={
            **_quote_payload(product, recipe, firing_line),
            "quantity": 20,
            "expected_source_fingerprint": original["source_fingerprint"],
            "accept_source_changes": False,
        },
        headers=head(admin_csrf),
    )
    assert updated_response.status_code == 200, updated_response.text
    assert updated_response.json()["quantity"] == 20

    cancel_response = await api.post(
        f"{QUOTATIONS}/{original['id']}/cancel", headers=head(admin_csrf)
    )
    assert cancel_response.status_code == 200, cancel_response.text
    assert cancel_response.json()["status"] == "CANCELLED"

    duplicate_response = await api.post(
        f"{QUOTATIONS}/{original['id']}/duplicate", headers=head(admin_csrf)
    )
    assert duplicate_response.status_code == 201, duplicate_response.text
    duplicate = duplicate_response.json()
    assert duplicate["status"] == "DRAFT"
    assert duplicate["code"] != original["code"]
    assert duplicate["quantity"] == 20


async def test_update_price_requires_confirmed_and_does_not_modify_cost(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    draft_response = await api.post(
        QUOTATIONS,
        json=_quote_payload(product, recipe, firing_line),
        headers=head(admin_csrf),
    )
    draft = draft_response.json()
    before_cost = await db_session.scalar(select(Product.cost).where(Product.id == product["id"]))

    blocked = await api.post(
        f"{QUOTATIONS}/{draft['id']}/update-product-price",
        headers=head(admin_csrf),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PRODUCT_PRICE_UPDATE_NOT_ALLOWED"

    confirmed_response = await api.post(
        f"{QUOTATIONS}/{draft['id']}/confirm",
        json={"accept_source_changes": False},
        headers=head(admin_csrf),
    )
    assert confirmed_response.status_code == 200, confirmed_response.text
    price_response = await api.post(
        f"{QUOTATIONS}/{draft['id']}/update-product-price",
        headers=head(admin_csrf),
    )
    assert price_response.status_code == 200, price_response.text
    db_session.expire_all()
    after_cost = await db_session.scalar(select(Product.cost).where(Product.id == product["id"]))
    assert after_cost == before_cost


async def test_parallel_creates_keep_quote_sequence_unique_and_atomic(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    payload = _quote_payload(product, recipe, firing_line)

    responses = await asyncio.gather(
        *(api.post(QUOTATIONS, json=payload, headers=head(admin_csrf)) for _ in range(4))
    )

    assert {response.status_code for response in responses} == {201}
    codes = [response.json()["code"] for response in responses]
    assert len(set(codes)) == len(codes)
    assert all(code.startswith("CTZ-") for code in codes)


async def test_el_igv_se_anade_sobre_el_neto_y_no_lo_altera(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """La cotizacion se emite sin IGV; el impuesto se calcula encima.

    Es la regla del negocio: el precio que se negocia es neto y el documento
    que se entrega muestra las dos cifras. Por eso el impuesto no entra en la
    formula comercial ni se multiplica por el factor.
    """
    from app.models.settings import SINGLETON_ID, CommercialSettings

    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])

    sin_igv = (
        await api.post(
            f"{QUOTATIONS}/calculate",
            json=_quote_payload(product, recipe, firing_line),
            headers=head(admin_csrf),
        )
    ).json()
    assert Decimal(sin_igv["tax_percentage"]) == Decimal(0)
    assert Decimal(sin_igv["total_with_tax"]) == Decimal(sin_igv["calculated_total"])
    assert "IGV_RATE_NOT_CONFIGURED" in sin_igv["warnings"]

    settings = await db_session.get(CommercialSettings, SINGLETON_ID)
    assert settings is not None
    settings.tax_percent = Decimal("18")
    await db_session.commit()

    con_igv = (
        await api.post(
            f"{QUOTATIONS}/calculate",
            json=_quote_payload(product, recipe, firing_line),
            headers=head(admin_csrf),
        )
    ).json()

    neto = Decimal(con_igv["calculated_total"])
    # El neto no cambia al configurar el impuesto: el IGV va encima, no dentro.
    assert neto == Decimal(sin_igv["calculated_total"])
    assert Decimal(con_igv["tax_percentage"]) == Decimal("18")
    assert Decimal(con_igv["tax_amount"]) == neto * Decimal("18") / Decimal(100)
    assert Decimal(con_igv["total_with_tax"]) == neto * Decimal("1.18")
    assert "IGV_RATE_NOT_CONFIGURED" not in con_igv["warnings"]


async def test_los_gramos_por_pieza_escalan_el_costo_de_materiales(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Una pieza no pesa un gramo: la receta se cotiza sobre los gramos reales."""
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])

    base = _quote_payload(product, recipe, firing_line)
    base.pop("materials_applied", None)

    uno = (await api.post(f"{QUOTATIONS}/calculate", json=base, headers=head(admin_csrf))).json()

    diez = (
        await api.post(
            f"{QUOTATIONS}/calculate",
            json={**base, "material_grams_per_piece": "10"},
            headers=head(admin_csrf),
        )
    ).json()

    assert Decimal(uno["material_grams_per_piece"]) == Decimal(1)
    assert Decimal(diez["material_grams_per_piece"]) == Decimal(10)
    assert Decimal(diez["material_total_grams"]) == Decimal(10) * Decimal(diez["quantity"])
    # Diez veces mas material, diez veces mas costo calculado.
    assert Decimal(diez["materials_calculated"]) == Decimal(uno["materials_calculated"]) * 10


async def test_los_gramos_por_pieza_sobreviven_a_guardar_editar_y_confirmar(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Confirmar no puede cambiar en silencio los gramos capturados.

    El fallo que cubre esta prueba era real: la reconstruccion del cuerpo a
    partir de la cotizacion guardada no incluia los gramos, de modo que
    confirmar recalculaba con el valor por omision y una hoja de 450 g/pieza
    pasaba a valer lo que valdria a 1 g/pieza.
    """
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])

    cuerpo = _quote_payload(product, recipe, firing_line)
    cuerpo["material_grams_per_piece"] = "450"
    cuerpo.pop("materials_applied", None)

    creada = (await api.post(QUOTATIONS, json=cuerpo, headers=head(admin_csrf))).json()
    assert Decimal(creada["material_grams_per_piece"]) == Decimal(450)
    assert Decimal(creada["material_total_grams"]) == Decimal(450) * Decimal(creada["quantity"])

    antes = {
        "grams": Decimal(creada["material_grams_per_piece"]),
        "total_grams": Decimal(creada["material_total_grams"]),
        "calculado": Decimal(creada["materials_calculated"]),
        "aplicado": Decimal(creada["materials_applied"]),
        "total": Decimal(creada["calculated_total"]),
    }

    releida = (await api.get(f"{QUOTATIONS}/{creada['id']}")).json()
    assert Decimal(releida["material_grams_per_piece"]) == Decimal(450)

    editada = (
        await api.put(
            f"{QUOTATIONS}/{creada['id']}",
            json={**cuerpo, "expected_source_fingerprint": releida["source_fingerprint"]},
            headers=head(admin_csrf),
        )
    ).json()
    assert Decimal(editada["material_grams_per_piece"]) == Decimal(450)

    confirmada = (
        await api.post(
            f"{QUOTATIONS}/{creada['id']}/confirm",
            json={"accept_source_changes": True},
            headers=head(admin_csrf),
        )
    ).json()

    assert confirmada["status"] == "CONFIRMED"
    assert Decimal(confirmada["material_grams_per_piece"]) == antes["grams"]
    assert Decimal(confirmada["material_total_grams"]) == antes["total_grams"]
    # Confirmar congela, no recalcula a otra escala.
    assert Decimal(confirmada["materials_calculated"]) == antes["calculado"]
    assert Decimal(confirmada["materials_applied"]) == antes["aplicado"]
    assert Decimal(confirmada["calculated_total"]) == antes["total"]

    final = (await api.get(f"{QUOTATIONS}/{creada['id']}")).json()
    assert Decimal(final["material_grams_per_piece"]) == Decimal(450)


async def test_sin_gramos_no_se_supone_uno_y_no_se_puede_confirmar(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Un dato ausente no se convierte en un costo creible."""
    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])

    cuerpo = _quote_payload(product, recipe, firing_line)
    cuerpo.pop("material_grams_per_piece", None)
    cuerpo.pop("materials_applied", None)

    calculo = (
        await api.post(f"{QUOTATIONS}/calculate", json=cuerpo, headers=head(admin_csrf))
    ).json()
    assert calculo["material_grams_per_piece"] is None
    assert calculo["material_total_grams"] is None
    assert Decimal(calculo["materials_calculated"]) == Decimal(0)
    assert "MATERIAL_GRAMS_PER_PIECE_REQUIRED" in calculo["warnings"]

    borrador = (await api.post(QUOTATIONS, json=cuerpo, headers=head(admin_csrf))).json()
    respuesta = await api.post(
        f"{QUOTATIONS}/{borrador['id']}/confirm",
        json={"accept_source_changes": True},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "MATERIAL_GRAMS_PER_PIECE_REQUIRED"


async def test_la_tasa_del_producto_manda_sobre_la_configuracion(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Y un cero explicito significa exento, no «sin definir»."""
    from app.models.masters import Product
    from app.models.settings import SINGLETON_ID, CommercialSettings

    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    cuerpo = _quote_payload(product, recipe, firing_line)

    settings = await db_session.get(CommercialSettings, SINGLETON_ID)
    assert settings is not None
    settings.tax_percent = Decimal("18")
    fila = await db_session.get(Product, product["id"])
    assert fila is not None
    fila.sale_tax_rate = None
    await db_session.commit()

    heredada = (
        await api.post(f"{QUOTATIONS}/calculate", json=cuerpo, headers=head(admin_csrf))
    ).json()
    assert Decimal(heredada["tax_percentage"]) == Decimal("18")
    assert heredada["tax_rate_source"] == "COMMERCIAL_SETTINGS"

    fila.sale_tax_rate = Decimal("10")
    await db_session.commit()
    propia = (
        await api.post(f"{QUOTATIONS}/calculate", json=cuerpo, headers=head(admin_csrf))
    ).json()
    assert Decimal(propia["tax_percentage"]) == Decimal("10")
    assert propia["tax_rate_source"] == "PRODUCT"

    # Exento: cero es un valor, no una ausencia.
    fila.sale_tax_rate = Decimal("0")
    await db_session.commit()
    exento = (
        await api.post(f"{QUOTATIONS}/calculate", json=cuerpo, headers=head(admin_csrf))
    ).json()
    assert Decimal(exento["tax_percentage"]) == Decimal(0)
    assert exento["tax_rate_source"] == "PRODUCT"
    assert Decimal(exento["total_with_tax"]) == Decimal(exento["calculated_total"])


async def test_actualizar_precio_usa_el_unitario_neto_y_no_el_de_con_igv(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """`sale_price` y `sale_tax_rate` son conceptos separados."""
    from app.models.masters import Product
    from app.models.settings import SINGLETON_ID, CommercialSettings

    settings = await db_session.get(CommercialSettings, SINGLETON_ID)
    assert settings is not None
    settings.tax_percent = Decimal("18")
    await db_session.commit()

    product, recipe = await _finished_product_and_recipe(api, admin_csrf)
    firing_line = await _confirmed_firing_line(api, admin_csrf, db_session, product["id"])
    cuerpo = _quote_payload(product, recipe, firing_line)
    cuerpo["material_grams_per_piece"] = "5"

    creada = (await api.post(QUOTATIONS, json=cuerpo, headers=head(admin_csrf))).json()
    confirmada = (
        await api.post(
            f"{QUOTATIONS}/{creada['id']}/confirm",
            json={"accept_source_changes": True},
            headers=head(admin_csrf),
        )
    ).json()
    neto = Decimal(confirmada["calculated_unit_price"])
    con_igv = Decimal(confirmada["unit_price_with_tax"])
    assert con_igv > neto

    await api.post(f"{QUOTATIONS}/{creada['id']}/update-product-price", headers=head(admin_csrf))
    await db_session.commit()
    fila = await db_session.get(Product, product["id"])
    assert fila is not None
    await db_session.refresh(fila)
    assert fila.sale_price == neto
    assert fila.sale_price != con_igv
