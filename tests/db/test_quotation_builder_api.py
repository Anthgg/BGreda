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


def head(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


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

    preview_response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["item_count"] == 2
    assert Decimal(preview["commercial_subtotal"]) == Decimal("3250")
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

    product_after = await api.get(f"/api/v1/products/{products[0]['id']}")
    assert product_after.status_code == 200
    assert product_after.json()["width"] == "24.000000"
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

    # Una dimension que ya existia no puede cambiarse desde el Cotizador.
    conflicting = dict(payload)
    conflicting["items"] = [dict(value) for value in payload["items"]]
    conflicting["items"][0]["dimensions"] = {"height": "99"}
    conflicting["expected_updated_at"] = draft["updated_at"]
    conflict_response = await api.put(
        f"{BUILDER}/{draft['id']}", json=conflicting, headers=head(admin_csrf)
    )
    assert conflict_response.status_code == 409
    assert conflict_response.json()["error"]["code"] == "PRODUCT_DIMENSION_CONFLICT"

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
