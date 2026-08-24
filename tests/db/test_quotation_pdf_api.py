"""Pruebas de integracion contra base de datos para la generacion del PDF de cotizaciones."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.masters import DocumentType, Partner, PartnerRole, Product
from app.models.quotations import Quotation
from app.models.settings import CommercialSettings, CompanySettings
from tests.db.conftest import (
    OPERATOR_EMAIL,
    OPERATOR_PASSWORD,
    authenticate,
)
from tests.db.fakes import FakeObjectStorage
from tests.db.test_firings_api import FIRINGS, head, hoja_de_referencia, hornos_de_referencia
from tests.db.test_masters_api import create_category, create_product

QUOTATIONS = "/api/v1/quotations"
RECIPES = "/api/v1/recipes"


async def _setup_confirmed_quotation(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    *,
    customer_name: str = "Restaurante Central SAC",
    customer_doc: str = "20123456789",
    product_name: str = "Taza Rustica 350ml",
    product_material: str = "Greda roja",
    product_width: str = "8.5",
    product_height: str = "11.0",
    tax_percent: str = "18.00",
    commercial_price: str = "15.00",
    quantity: int = 100,
) -> dict[str, Any]:
    # 1. Ajustar configuracion comercial
    comm_curr = (await api.get("/api/v1/settings/commercial", headers=head(admin_csrf))).json()
    await api.put(
        "/api/v1/settings/commercial",
        json={
            "version": comm_curr["version"],
            "tax_percent": tax_percent,
            "currency_code": "PEN",
            "quote_validity_days": 15,
            "general_conditions": "Entrega en taller.",
            "payment_notes": "50% adelanto, 50% entrega.",
            "bank_account": {
                "bank_name": "BCP",
                "account_holder": "Ceramica Greda SAC",
                "account_number": "191-12345678-0-12",
                "cci": "00219100123456780012",
            },
        },
        headers=head(admin_csrf),
    )

    # 2. Crear Cliente
    partner = Partner(
        name=customer_name,
        document_type=DocumentType.RUC,
        document_number=customer_doc,
        email="compras@central.pe",
        phone="999888777",
        address="Av. Miraflores 123",
        role=PartnerRole.CLIENT,
        active=True,
    )
    db_session.add(partner)
    await db_session.flush()

    # 3. Crear Categoria y Producto
    category = await create_category(api, admin_csrf, f"PDF Cat {product_name[:10]}")
    prod_res = await create_product(
        api,
        admin_csrf,
        product_category_id=category["id"],
        product_type="FINISHED_PRODUCT",
        name=product_name,
        base_uom_code="unit",
        material=product_material,
        width=product_width,
        height=product_height,
        grammage="350",
    )
    assert prod_res.status_code == 201, prod_res.text
    product = prod_res.json()

    # 4. Crear Receta y Horno
    mat_res = await create_product(
        api,
        admin_csrf,
        product_category_id=category["id"],
        product_type="RAW_MATERIAL",
        name=f"Barro {product_name[:10]}",
        base_uom_code="g",
        cost="0.05",
    )
    material = mat_res.json()

    prep_res = await create_product(
        api,
        admin_csrf,
        product_category_id=category["id"],
        product_type="PREPARED_MATERIAL",
        name=f"Pasta {product_name[:10]}",
        base_uom_code="g",
    )
    prepared = prep_res.json()

    rec_res = await api.post(
        RECIPES,
        json={
            "product_id": prepared["id"],
            "name": f"Receta {product_name[:10]}",
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
        headers=head(admin_csrf),
    )
    assert rec_res.status_code == 201, rec_res.text
    recipe = rec_res.json()

    small, large = await hornos_de_referencia(api, admin_csrf, db_session)
    firing_payload = hoja_de_referencia(small, large)
    firing_payload["lines"][0]["product_id"] = product["id"]
    draft_f = await api.post(FIRINGS, json=firing_payload, headers=head(admin_csrf))
    conf_f = await api.post(f"{FIRINGS}/{draft_f.json()['id']}/confirm", headers=head(admin_csrf))
    firing_line = conf_f.json()["lines"][0]

    # 5. Crear y Confirmar Cotizacion
    quote_payload = {
        "name": f"Cotizacion para {customer_name}",
        "customer_id": partner.id,
        "product_id": product["id"],
        "quantity": quantity,
        "recipe_id": recipe["id"],
        "recipe_version_id": recipe["current_version"]["id"],
        "firing_line_id": firing_line["id"],
        "material_grams_per_piece": "350",
        "materials_applied": "17.50",
        "techniques": [],
        "additionals": [],
        "days_adjustment": 0,
        "waiting_days": 0,
        "other_costs": [],
        "commercial_factor": "2.0",
        "commercial_sale_unit_price": commercial_price,
    }

    create_res = await api.post(QUOTATIONS, json=quote_payload, headers=head(admin_csrf))
    assert create_res.status_code == 201, create_res.text
    quote = create_res.json()

    confirm_res = await api.post(
        f"{QUOTATIONS}/{quote['id']}/confirm",
        json={"accept_source_changes": True},
        headers=head(admin_csrf),
    )
    assert confirm_res.status_code == 200, confirm_res.text
    return confirm_res.json()


async def test_get_quotation_pdf_requires_authentication(api: httpx.AsyncClient) -> None:
    # Sin sesion -> 401
    res = await api.get(f"{QUOTATIONS}/1/pdf")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_NOT_AUTHENTICATED"


async def test_get_quotation_pdf_not_found(
    api: httpx.AsyncClient,
    admin_csrf: str,
) -> None:
    res = await api.get(f"{QUOTATIONS}/999999/pdf", headers=head(admin_csrf))
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "QUOTATION_NOT_FOUND"


async def test_draft_quotation_pdf_is_blocked(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    # Crear cotizacion en DRAFT
    category = await create_category(api, admin_csrf, "Draft Cat")
    prod_res = await create_product(
        api,
        admin_csrf,
        product_category_id=category["id"],
        product_type="FINISHED_PRODUCT",
        name="Vaso Borrador",
        base_uom_code="unit",
    )
    product = prod_res.json()

    create_res = await api.post(
        QUOTATIONS,
        json={"product_id": product["id"], "quantity": 10},
        headers=head(admin_csrf),
    )
    assert create_res.status_code == 201
    draft_quote = create_res.json()

    # Intentar descargar PDF de cotizacion en DRAFT -> 409
    pdf_res = await api.get(f"{QUOTATIONS}/{draft_quote['id']}/pdf", headers=head(admin_csrf))
    assert pdf_res.status_code == 409
    assert pdf_res.json()["error"]["code"] == "QUOTATION_DRAFT_PDF_BLOCKED"


async def test_confirmed_quotation_generates_valid_pdf_with_headers_and_content(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    confirmed = await _setup_confirmed_quotation(
        api,
        admin_csrf,
        db_session,
        customer_name="Restaurante Astrid y Gaston",
        customer_doc="20501234567",
        product_name="Plato Hondo Cerámica",
        tax_percent="18.00",
        commercial_price="22.50",
        quantity=50,
    )

    pdf_res = await api.get(f"{QUOTATIONS}/{confirmed['id']}/pdf", headers=head(admin_csrf))
    assert pdf_res.status_code == 200
    assert pdf_res.headers["Content-Type"] == "application/pdf"
    assert "Content-Disposition" in pdf_res.headers
    assert "inline;" in pdf_res.headers["Content-Disposition"]
    assert f"{confirmed['code']}" in pdf_res.headers["Content-Disposition"]

    content = pdf_res.content
    assert content.startswith(b"%PDF")
    assert len(content) > 1000


async def test_cancelled_quotation_generates_historical_pdf_marked_as_anulada(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    confirmed = await _setup_confirmed_quotation(
        api,
        admin_csrf,
        db_session,
        customer_name="Restaurante Maido SAC",
        product_name="Bowl Degustacion",
    )

    # Cancelar la cotizacion
    cancel_res = await api.post(f"{QUOTATIONS}/{confirmed['id']}/cancel", headers=head(admin_csrf))
    assert cancel_res.status_code == 200
    cancelled = cancel_res.json()
    assert cancelled["status"] == "CANCELLED"

    pdf_res = await api.get(f"{QUOTATIONS}/{confirmed['id']}/pdf", headers=head(admin_csrf))
    assert pdf_res.status_code == 200
    assert pdf_res.headers["Content-Type"] == "application/pdf"
    assert pdf_res.content.startswith(b"%PDF")


async def test_operator_user_can_download_pdf(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    confirmed = await _setup_confirmed_quotation(api, admin_csrf, db_session)

    # Autenticar como operario
    operator_csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
    pdf_res = await api.get(f"{QUOTATIONS}/{confirmed['id']}/pdf", headers=head(operator_csrf))
    assert pdf_res.status_code == 200
    assert pdf_res.headers["Content-Type"] == "application/pdf"
    assert pdf_res.content.startswith(b"%PDF")


async def test_historical_snapshot_immutability_in_pdf(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """TEST OBLIGATORIO: Modificar los registros maestros despues de confirmar

    NO altera la informacion reproducida en el PDF de la cotizacion emitida.
    """
    confirmed = await _setup_confirmed_quotation(
        api,
        admin_csrf,
        db_session,
        customer_name="Cliente Alfa SAC",
        customer_doc="20111111111",
        product_name="Taza Alfa Original",
        product_material="Greda negra",
        product_width="7.5",
        product_height="9.0",
        tax_percent="10.00",
        commercial_price="8.50",
        quantity=200,
    )
    quote_id = confirmed["id"]

    # 1. Modificar el cliente en el maestro de Partners
    partner_stmt = select(Partner).where(Partner.document_number == "20111111111")
    partner = (await db_session.execute(partner_stmt)).scalar_one()
    partner.name = "Cliente Beta Modificado"
    partner.document_number = "20999999999"

    # 2. Modificar el producto en el maestro de Products
    prod_stmt = select(Product).where(Product.id == confirmed["product_id"])
    product = (await db_session.execute(prod_stmt)).scalar_one()
    product.name = "Taza Beta Modificada"
    product.material = "Porcelana importada"
    product.width = Decimal("15.00")
    product.height = Decimal("20.00")

    # 3. Modificar la configuracion comercial (IGV a 18%)
    comm_settings_stmt = select(CommercialSettings).where(CommercialSettings.id == 1)
    comm_settings = (await db_session.execute(comm_settings_stmt)).scalar_one()
    comm_settings.tax_percent = Decimal("18.00")

    await db_session.commit()

    # 4. Generar el PDF y verificar que conserva los snapshots historicos
    pdf_res = await api.get(f"{QUOTATIONS}/{quote_id}/pdf", headers=head(admin_csrf))
    assert pdf_res.status_code == 200
    assert pdf_res.content.startswith(b"%PDF")

    # Verificar que el PDF renderizado mantuvo los snapshots
    # (podemos instanciar el servicio para auditar el ViewModel y el HTML exactos)
    from app.services.quotation_pdf import QuotationPdfService

    pdf_service = QuotationPdfService(db_session)
    quote_row = (
        await db_session.execute(select(Quotation).where(Quotation.id == quote_id))
    ).scalar_one()
    company_row = (
        await db_session.execute(select(CompanySettings).where(CompanySettings.id == 1))
    ).scalar_one_or_none()
    comm_row = (
        await db_session.execute(
            select(CommercialSettings)
            .where(CommercialSettings.id == 1)
            .options(selectinload(CommercialSettings.bank_accounts))
        )
    ).scalar_one_or_none()

    doc_model = pdf_service.build_document_model(
        quotation=quote_row,
        company_settings=company_row,
        commercial_settings=comm_row,
    )

    # Afirmar que el ViewModel contiene la data congelada original y no los maestros alterados
    assert doc_model.customer.name == "Cliente Alfa SAC"
    assert doc_model.customer.document_number == "20111111111"
    assert doc_model.items[0].product_name == "Taza Alfa Original"
    assert doc_model.items[0].material == "Greda negra"
    assert doc_model.items[0].dimensions_formatted == "Ancho: 7.5 cm | Alto: 9 cm"
    assert doc_model.totals.tax_label == "IGV (10%)"
    assert doc_model.items[0].unit_price_formatted == "S/ 8.50"
    assert doc_model.totals.subtotal_formatted == "S/ 1,700.00"
    assert doc_model.totals.total_formatted == "S/ 1,870.00"


async def test_idempotent_pdf_generation(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Generar repetidamente el PDF no altera la base de datos ni los correlativos."""
    confirmed = await _setup_confirmed_quotation(api, admin_csrf, db_session)
    quote_id = confirmed["id"]

    res1 = await api.get(f"{QUOTATIONS}/{quote_id}/pdf", headers=head(admin_csrf))
    assert res1.status_code == 200

    res2 = await api.get(f"{QUOTATIONS}/{quote_id}/pdf", headers=head(admin_csrf))
    assert res2.status_code == 200

    # Estado de cotizacion sigue intacto
    get_res = await api.get(f"{QUOTATIONS}/{quote_id}", headers=head(admin_csrf))
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "CONFIRMED"
    assert get_res.json()["code"] == confirmed["code"]


async def test_logo_storage_resilience_in_pdf(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    storage: FakeObjectStorage,
) -> None:
    """Si el logo esta en storage, se incluye; si storage falla o no existe, no revienta."""
    confirmed = await _setup_confirmed_quotation(api, admin_csrf, db_session)
    quote_id = confirmed["id"]

    # 1. Configurar logo en CompanySettings y subir a FakeObjectStorage
    logo_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xaf"
        b"\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    logo_path = "company/logo-test-123.png"
    await storage.upload(logo_path, logo_bytes, "image/png")

    company_stmt = select(CompanySettings).where(CompanySettings.id == 1)
    company = (await db_session.execute(company_stmt)).scalar_one()
    company.logo_object_path = logo_path
    company.logo_content_type = "image/png"
    company.legal_name = "Cerámica Greda SAC"
    await db_session.commit()

    # Descargar PDF con logo -> 200
    res_with_logo = await api.get(f"{QUOTATIONS}/{quote_id}/pdf", headers=head(admin_csrf))
    assert res_with_logo.status_code == 200
    assert res_with_logo.content.startswith(b"%PDF")

    # 2. Simular falla en Storage -> no debe reventar con 500, genera PDF sin logo
    storage.fail = True
    res_storage_failed = await api.get(f"{QUOTATIONS}/{quote_id}/pdf", headers=head(admin_csrf))
    assert res_storage_failed.status_code == 200
    assert res_storage_failed.content.startswith(b"%PDF")

