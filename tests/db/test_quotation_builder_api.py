"""Regresiones criticas del Cotizador integral multiproducto."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.documents.quotation import build_quotation_pdf_document
from app.models.firings import Firing
from app.models.masters import Product
from app.models.quotations import Quotation
from tests.db.test_firings_api import FACTORES_CHICO, crear_horno
from tests.db.test_quotations_api import _finished_product_and_recipe

BUILDER = "/api/v1/quotation-builder"
PARTNERS = "/api/v1/partners"
QUOTATIONS = "/api/v1/quotations"
COMMERCIAL = "/api/v1/settings/commercial"


def head(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def cambiar_configuracion(api: httpx.AsyncClient, csrf: str, **campos: Any) -> dict[str, Any]:
    """Cambia la configuracion comercial y COMPRUEBA que el cambio entro.

    Existe porque devolverle al PUT lo que dio el GET no funciona y fallaba en
    silencio: la salida trae `bank_accounts` y la entrada espera `version`, no
    `expected_version`, asi que el servidor respondia 422 y la prueba que no
    miraba el codigo seguia en verde sin haber cambiado nada. Una prueba de
    inmutabilidad que nunca llega a mover la configuracion no prueba nada.

    El PUT reemplaza: los campos que no se mandan se quedan en nulo. Por eso se
    reenvia el estado actual completo, filtrado a lo que el esquema admite.
    """
    actual = (await api.get(COMMERCIAL)).json()
    admitidos = {
        clave: valor
        for clave, valor in actual.items()
        if clave not in {"updated_at", "version", "bank_accounts"}
    }
    respuesta = await api.put(
        COMMERCIAL,
        json={**admitidos, **campos, "version": actual["version"]},
        headers=head(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text
    return dict(respuesta.json())


async def _customer(api: httpx.AsyncClient, csrf: str) -> dict[str, Any]:
    response = await api.post(
        PARTNERS,
        json={
            "name": "Restaurante Cotizador SAC",
            "role": "CLIENT",
            "document_type": "RUC",
            "document_number": "20601234567",
        },
        headers=head(csrf),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _complete_payload(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    customer = await _customer(api, csrf)
    product_a, recipe_a = await _finished_product_and_recipe(api, csrf, "_builder_a")
    product_b, recipe_b = await _finished_product_and_recipe(api, csrf, "_builder_b")
    await db_session.execute(
        update(Product)
        .where(Product.id.in_([product_a["id"], product_b["id"]]))
        .values(height=Decimal("2"))
    )
    await db_session.commit()
    kiln = await crear_horno(
        api,
        csrf,
        db_session,
        nombre="Horno Cotizador QA",
        capacidad="10000000",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )
    products = [(product_a, recipe_a, 100, "8.50"), (product_b, recipe_b, 200, "12")]
    return (
        {
            "name": "Vajilla agosto",
            "customer_id": customer["id"],
            "kiln_id": kiln["id"],
            "items": [
                {
                    "product_id": product["id"],
                    "quantity": quantity,
                    "dimensions": {"width": "24", "length": "24"},
                    "recipe_id": recipe["id"],
                    "recipe_version_id": recipe["current_version"]["id"],
                    "material_grams_per_piece": "10",
                    "other_costs": [],
                    "markup_percent": "100",
                    "commercial_sale_unit_price": price,
                    "sort_order": index,
                }
                for index, (product, recipe, quantity, price) in enumerate(products)
            ],
        },
        [product_a, product_b],
    )


@pytest.mark.asyncio
async def test_preview_is_pure_and_two_products_persist_confirm_and_render_pdf_model(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    payload, products = await _complete_payload(api, admin_csrf, db_session)
    for item in payload["items"]:
        item.pop("sort_order")

    preview_response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["item_count"] == 2
    assert [item["sort_order"] for item in preview["items"]] == [0, 1]
    # Fase 009E: el precio manual (8,50 y 12) entra como neto crudo y pasa por
    # el mismo camino contractual que cualquier otro —IGV, CEILING del bruto,
    # reconstruccion del neto—, asi que el subtotal ya no es 8,50 x 100 + 12 x
    # 200. Se comprueba la INVARIANTE, que es lo que importa: el subtotal del
    # encabezado es la suma de los netos de linea que calcula el backend.
    assert Decimal(preview["commercial_subtotal"]) == sum(
        Decimal(item["line_total_net"]) for item in preview["items"]
    )
    assert Decimal(preview["quotation_gross_total"]) == sum(
        Decimal(item["line_total_gross"]) for item in preview["items"]
    )
    assert preview["production_summary"]["estimated"] is True
    assert int((await db_session.execute(select(func.count(Firing.id)))).scalar_one()) == 0

    # El preview no completa maestros; el guardado transaccional si.
    product_before = await api.get(f"/api/v1/products/{products[0]['id']}")
    assert product_before.status_code == 200
    assert product_before.json()["width"] is None

    create_response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert create_response.status_code == 201, create_response.text
    draft = create_response.json()
    assert draft["workflow"] == "COTIZADOR"
    assert draft["status"] == "DRAFT"
    assert draft["complete"] is True
    # La dimension enviada (completa un NULL del maestro) queda en la linea...
    assert Decimal(draft["items"][0]["width"]) == Decimal("24")
    assert Decimal(draft["items"][0]["height"]) == Decimal("2")

    # ...pero el maestro NUNCA se muta desde el Cotizador (Fase 009B):
    # antes de esta fase, crear el borrador escribia width=24 en products.
    product_after = await api.get(f"/api/v1/products/{products[0]['id']}")
    assert product_after.status_code == 200
    assert product_after.json()["width"] is None
    assert product_after.json()["height"] == "2.000000"

    valid_update = dict(payload)
    valid_update["items"] = [dict(value) for value in payload["items"]]
    valid_update["items"][0]["commercial_sale_unit_price"] = "9.00"
    valid_update["expected_updated_at"] = draft["updated_at"]
    update_response = await api.put(
        f"{BUILDER}/{draft['id']}", json=valid_update, headers=head(admin_csrf)
    )
    assert update_response.status_code == 200, update_response.text
    draft = update_response.json()
    assert Decimal(draft["items"][0]["commercial_sale_unit_price"]) == Decimal("9")

    await db_session.execute(
        update(Product)
        .where(Product.id == products[0]["id"])
        .values(name="Producto modificado antes de confirmar")
    )
    await db_session.commit()
    source_changed = await api.post(
        f"{BUILDER}/{draft['id']}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert source_changed.status_code == 409
    assert source_changed.json()["error"]["code"] == "QUOTATION_BUILDER_SOURCE_CHANGED"

    valid_update["expected_updated_at"] = draft["updated_at"]
    refreshed_response = await api.put(
        f"{BUILDER}/{draft['id']}", json=valid_update, headers=head(admin_csrf)
    )
    assert refreshed_response.status_code == 200, refreshed_response.text
    draft = refreshed_response.json()

    confirm_response = await api.post(
        f"{BUILDER}/{draft['id']}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirm_response.status_code == 200, confirm_response.text
    confirmed = confirm_response.json()
    assert confirmed["status"] == "CONFIRMED"

    immutable = dict(payload)
    immutable["expected_updated_at"] = confirmed["updated_at"]
    immutable_response = await api.put(
        f"{BUILDER}/{draft['id']}", json=immutable, headers=head(admin_csrf)
    )
    assert immutable_response.status_code == 409

    listing = await api.get(QUOTATIONS)
    assert listing.status_code == 200
    listed = next(value for value in listing.json()["items"] if value["id"] == draft["id"])
    assert listed["workflow"] == "COTIZADOR"
    assert listed["item_count"] == 2

    row = (
        await db_session.execute(
            select(Quotation)
            .where(Quotation.id == draft["id"])
            .options(selectinload(Quotation.items), selectinload(Quotation.product))
        )
    ).scalar_one()
    document = build_quotation_pdf_document(row)
    assert len(document.items) == 2
    serialized = repr(document)
    assert "final_unit_cost" not in serialized
    assert "markup_percent" not in serialized
    assert "effective_profit" not in serialized


@pytest.mark.asyncio
async def test_builder_preserva_ruta_de_quema_por_producto(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    payload, _products = await _complete_payload(api, admin_csrf, db_session)
    kiln_small = payload.pop("kiln_id")
    kiln_large = await crear_horno(
        api,
        admin_csrf,
        db_session,
        nombre="Horno Ruta Grande QA",
        capacidad="20000000",
        baja="1000",
        alta="2000",
        factores=FACTORES_CHICO,
    )
    payload["items"][0].update(
        low_kiln_id=kiln_small,
        high_kiln_id=kiln_large["id"],
        factor_kiln_id=kiln_small,
    )
    payload["items"][1].update(
        low_kiln_id=kiln_small,
        high_kiln_id=kiln_small,
        factor_kiln_id=kiln_large["id"],
    )

    preview_response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["complete"] is True
    assert {
        (session["kiln_id"], session["firing_type"])
        for session in preview["production_summary"]["sessions"]
    } == {
        (kiln_small, "LOW"),
        (kiln_small, "HIGH"),
        (kiln_large["id"], "HIGH"),
    }
    assert preview["items"][0]["low_kiln_id"] == kiln_small
    assert preview["items"][0]["high_kiln_id"] == kiln_large["id"]
    assert preview["items"][1]["factor_kiln_id"] == kiln_large["id"]

    created_response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    reopened = (await api.get(f"{BUILDER}/{created['id']}")).json()
    assert reopened["items"][0]["high_kiln_id"] == kiln_large["id"]
    assert reopened["items"][1]["factor_kiln_id"] == kiln_large["id"]


@pytest.mark.asyncio
async def test_builder_usa_una_linea_confirmada_como_fuente_del_costo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    payload, products = await _complete_payload(api, admin_csrf, db_session)
    kiln_id = payload.pop("kiln_id")
    payload["items"] = [payload["items"][0]]
    firing_payload = {
        "sessions": [
            {"kiln_id": kiln_id, "firing_type": "LOW", "sort_order": 0},
            {"kiln_id": kiln_id, "firing_type": "HIGH", "sort_order": 1},
        ],
        "lines": [
            {
                "product_id": products[0]["id"],
                "description": products[0]["name"],
                "quantity": 100,
                "length_cm": "24",
                "width_cm": "24",
                "height_cm": "2",
                "low_kiln_id": kiln_id,
                "high_kiln_id": kiln_id,
                "factor_kiln_id": kiln_id,
                "sort_order": 0,
            }
        ],
    }
    firing = (
        await api.post("/api/v1/firings", json=firing_payload, headers=head(admin_csrf))
    ).json()
    confirmed = (
        await api.post(f"/api/v1/firings/{firing['id']}/confirm", headers=head(admin_csrf))
    ).json()
    firing_line = confirmed["lines"][0]
    payload["items"][0].pop("recipe_id", None)
    payload["items"][0].pop("recipe_version_id", None)
    payload["items"][0].pop("material_grams_per_piece", None)
    payload["items"][0].pop("commercial_sale_unit_price", None)
    payload["items"][0]["firing_line_id"] = firing_line["id"]
    payload["items"][0]["materials_applied"] = "11.58"

    preview_response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["complete"] is True
    assert preview["production_summary"]["source"] == "CONFIRMED_FIRING_LINES"
    quoted_quantity = Decimal(str(payload["items"][0]["quantity"]))
    source_quantity = Decimal(str(firing_line["quantity"]))
    assert Decimal(preview["production_summary"]["total_volume_cm3"]) == (
        Decimal(firing_line["unit_volume_cm3"]) * quoted_quantity
    )
    assert Decimal(preview["production_summary"]["occupancy_percentage"]) == Decimal(
        firing_line["occupancy_percentage"]
    )
    assert Decimal(preview["production_summary"]["occupancy_factor"]) == Decimal(
        firing_line["occupancy_factor"]
    )
    assert Decimal(preview["production_summary"]["subtotal"]) == (
        Decimal(firing_line["base_cost"]) / source_quantity * quoted_quantity
    )
    assert preview["items"][0]["firing_line_id"] == firing_line["id"]
    assert Decimal(preview["items"][0]["materials_applied"]) == Decimal("11.58")
    assert preview["items"][0]["materials_applied_input"] == "11.58"
    assert preview["items"][0]["commercial_sale_unit_price_input"] is None
    assert Decimal(preview["items"][0]["commercial_sale_unit_price"]) == Decimal(
        preview["items"][0]["suggested_commercial_unit_price"]
    )
    assert "RECIPE_REQUIRED" not in preview["items"][0]["warnings"]
    assert "MATERIAL_GRAMS_PER_PIECE_REQUIRED" not in preview["items"][0]["warnings"]
    assert "DISCOUNT_RULE_BLOCKED_BY_SOURCE" not in preview["items"][0]["warnings"]
    assert Decimal(preview["items"][0]["firing_cost"]) == (
        Decimal(firing_line["allocated_cost"]) / source_quantity * quoted_quantity
    )

    # Fase 009B: personalizar medidas NO se admite sobre una linea de quema
    # confirmada. Esa quema ya ocurrio con medidas reales y su costo/volumen
    # salen del historico, asi que aceptar un override anunciaria una pieza
    # mas grande cobrando la quema de la pequena.
    with_override = dict(payload)
    with_override["items"] = [dict(payload["items"][0])]
    with_override["items"][0]["dimensions"] = {"width": "50", "height": "50", "length": "50"}
    with_override["items"][0]["dimensions_overridden"] = True
    override_preview = (
        await api.post(f"{BUILDER}/preview", json=with_override, headers=head(admin_csrf))
    ).json()
    assert (
        "CUSTOM_DIMENSIONS_NOT_ALLOWED_FOR_CONFIRMED_FIRING"
        in override_preview["items"][0]["warnings"]
    )
    assert override_preview["items"][0]["complete"] is False
    assert override_preview["complete"] is False

    created = (await api.post(BUILDER, json=payload, headers=head(admin_csrf))).json()
    reopened = (await api.get(f"{BUILDER}/{created['id']}")).json()
    assert reopened["items"][0]["firing_line_id"] == firing_line["id"]
    assert reopened["items"][0]["firing_code_snapshot"] == confirmed["code"]
    assert reopened["items"][0]["materials_applied_input"] == "11.58"
    assert reopened["items"][0]["commercial_sale_unit_price_input"] is None
    assert Decimal(reopened["items"][0]["commercial_unit_price_with_tax"]) == (
        Decimal(reopened["items"][0]["commercial_total"])
        / Decimal(str(reopened["items"][0]["quantity"]))
    )
    assert Decimal(reopened["production_summary"]["occupancy_percentage"]) == Decimal(
        firing_line["occupancy_percentage"]
    )

    listed = (await api.get("/api/v1/quotations")).json()["items"]
    listed_row = next(item for item in listed if item["id"] == created["id"])
    quantity = Decimal(str(payload["items"][0]["quantity"]))
    assert Decimal(str(listed_row["quantity"])) == quantity
    assert Decimal(listed_row["commercial_sale_unit_price"]) == (
        Decimal(reopened["commercial_subtotal"]) / quantity
    )

    duplicated = (
        await api.post(f"{BUILDER}/{created['id']}/duplicate", headers=head(admin_csrf))
    ).json()
    assert duplicated["items"][0]["commercial_sale_unit_price_input"] is None
    assert Decimal(duplicated["items"][0]["commercial_sale_unit_price"]) == Decimal(
        duplicated["items"][0]["suggested_commercial_unit_price"]
    )

    # Inmutabilidad de la quema fuente
    firing_after = (await api.get(f"/api/v1/firings/{firing['id']}")).json()
    assert firing_after["status"] == confirmed["status"]
    assert firing_after["total_cost"] == confirmed["total_cost"]
    assert len(firing_after["lines"]) == len(confirmed["lines"])
    assert firing_after["lines"][0]["quantity"] == confirmed["lines"][0]["quantity"]
    assert firing_after["lines"][0]["allocated_cost"] == confirmed["lines"][0]["allocated_cost"]


@pytest.mark.asyncio
async def test_incomplete_draft_can_be_saved_but_not_confirmed(
    api: httpx.AsyncClient,
    admin_csrf: str,
) -> None:
    response = await api.post(
        BUILDER,
        json={"name": "Borrador progresivo", "items": []},
        headers=head(admin_csrf),
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["complete"] is False
    assert draft["next_step"] == "GENERAL_DATA"

    confirmation = await api.post(
        f"{BUILDER}/{draft['id']}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmation.status_code == 409
    assert confirmation.json()["error"]["code"] == "QUOTATION_BUILDER_INCOMPLETE"


@pytest.mark.asyncio
async def test_builder_requires_authentication(api: httpx.AsyncClient) -> None:
    response = await api.post(f"{BUILDER}/preview", json={"items": []})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_mixed_tax_rates_use_an_effective_header_rate_and_explicit_pdf_label(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    payload, products = await _complete_payload(api, admin_csrf, db_session)
    await db_session.execute(
        update(Product).where(Product.id == products[0]["id"]).values(sale_tax_rate=Decimal(18))
    )
    await db_session.execute(
        update(Product).where(Product.id == products[1]["id"]).values(sale_tax_rate=Decimal(0))
    )
    await db_session.commit()

    preview_response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["complete"] is True
    assert preview["tax_rate_source_snapshot"] == "MIXED"
    assert "MIXED_TAX_RATES" in preview["warnings"]
    assert Decimal(0) < Decimal(preview["tax_percentage_snapshot"]) < Decimal(18)

    create_response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert create_response.status_code == 201, create_response.text
    draft = create_response.json()
    confirm_response = await api.post(
        f"{BUILDER}/{draft['id']}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirm_response.status_code == 200, confirm_response.text

    row = (
        await db_session.execute(
            select(Quotation)
            .where(Quotation.id == draft["id"])
            .options(selectinload(Quotation.items), selectinload(Quotation.product))
        )
    ).scalar_one()
    document = build_quotation_pdf_document(row)
    assert document.totals.tax_label.startswith("IGV (tasa efectiva ")


@pytest.mark.asyncio
async def test_pdf_preview_in_memory_and_saved_draft_and_no_mutation(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.quotation_pdf import QuotationPdfService

    # Mock de render WeasyPrint para garantizar ejecucion sin dependencia de binarios C del OS
    monkeypatch.setattr(
        QuotationPdfService,
        "render_pdf_from_html",
        lambda self, html: b"%PDF-1.4 mock preview",
    )

    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    # 1. Contar registros iniciales
    initial_quotes = (await db_session.execute(select(func.count(Quotation.id)))).scalar_one()
    initial_firings = (await db_session.execute(select(func.count(Firing.id)))).scalar_one()

    # 2. Preview en memoria POST
    preview_pdf_res = await api.post(
        f"{BUILDER}/pdf-preview",
        json=payload,
        headers=head(admin_csrf),
    )
    assert preview_pdf_res.status_code == 200, preview_pdf_res.text
    assert preview_pdf_res.headers["content-type"] == "application/pdf"
    assert preview_pdf_res.content == b"%PDF-1.4 mock preview"
    assert "BORRADOR" in preview_pdf_res.headers.get("content-disposition", "")

    # 3. Verificar NINGUNA mutacion en base de datos
    after_quotes = (await db_session.execute(select(func.count(Quotation.id)))).scalar_one()
    after_firings = (await db_session.execute(select(func.count(Firing.id)))).scalar_one()
    assert initial_quotes == after_quotes
    assert initial_firings == after_firings

    # 4. Crear borrador persistido
    create_res = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert create_res.status_code == 201, create_res.text
    draft = create_res.json()
    draft_id = draft["id"]

    # 5. Preview GET del borrador guardado
    get_preview_res = await api.get(f"{BUILDER}/{draft_id}/pdf-preview")
    assert get_preview_res.status_code == 200
    assert get_preview_res.headers["content-type"] == "application/pdf"

    # 6. El endpoint oficial GET /quotations/{id}/pdf SIGUE BLOQUEADO en 409
    official_pdf_draft = await api.get(f"{QUOTATIONS}/{draft_id}/pdf")
    assert official_pdf_draft.status_code == 409
    assert official_pdf_draft.json()["error"]["code"] == "QUOTATION_DRAFT_PDF_BLOCKED"

    # 7. Confirmar y verificar que el endpoint oficial funciona con 200
    confirm_res = await api.post(
        f"{BUILDER}/{draft_id}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirm_res.status_code == 200

    official_pdf_confirmed = await api.get(f"{QUOTATIONS}/{draft_id}/pdf")
    assert official_pdf_confirmed.status_code == 200
    assert official_pdf_confirmed.headers["content-type"] == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_preview_empty_blocked(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    empty_res = await api.post(
        f"{BUILDER}/pdf-preview",
        json={"items": []},
        headers=head(admin_csrf),
    )
    assert empty_res.status_code == 409
    assert empty_res.json()["error"]["code"] == "QUOTATION_BUILDER_INCOMPLETE"

    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    payload["customer_id"] = None

    incomplete_res = await api.post(
        f"{BUILDER}/pdf-preview",
        json=payload,
        headers=head(admin_csrf),
    )
    assert incomplete_res.status_code == 409
    assert incomplete_res.json()["error"]["code"] == "QUOTATION_BUILDER_INCOMPLETE"


@pytest.mark.asyncio
async def test_draft_persistence_with_mixed_tax_rate_source(
    api: httpx.AsyncClient,
    admin_csrf: str,
) -> None:
    # 1. Borrador inicial sin items ni cliente: tax_rate_source_snapshot queda 'MIXED'
    res = await api.post(
        BUILDER,
        json={"name": "Borrador inicial MIXED", "items": []},
        headers=head(admin_csrf),
    )
    assert res.status_code == 201, res.text
    draft = res.json()
    assert draft["tax_rate_source_snapshot"] == "MIXED"
    assert draft["status"] == "DRAFT"
    assert draft["workflow"] == "COTIZADOR"
