"""Modelos de vista y formateadores para el documento comercial de cotizacion."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect

from app.models.quotations import Quotation, QuotationStatus
from app.models.settings import CommercialSettings, CompanySettings

if TYPE_CHECKING:
    from app.models.masters import Partner
    from app.schemas.quotation_builder import QuotationBuilderOut


def format_currency(amount: Decimal | None, symbol: str = "S/") -> str:
    """Formatea un importe monetario en formato comercial 'S/ 1,250.00'."""
    if amount is None:
        amount = Decimal(0)
    return f"{symbol} {amount:,.2f}"


def format_quantity(qty: int | Decimal | None) -> str:
    """Formatea cantidades enteras o decimales sin ceros innecesarios."""
    if qty is None:
        return "0"
    if isinstance(qty, Decimal):
        if qty == qty.to_integral():
            return f"{int(qty):,}"
        return f"{qty.normalize():,}"
    return f"{qty:,}"


def format_tax_label(tax_percent: Decimal | None) -> str:
    """Genera la etiqueta dinamica del impuesto, e.g. 'IGV (18%)', 'IGV (10%)', 'IGV (0%)'."""
    if tax_percent is None:
        tax_percent = Decimal(0)
    if tax_percent == tax_percent.to_integral():
        rate_str = f"{int(tax_percent)}%"
    else:
        rate_str = f"{tax_percent.normalize()}%"
    return f"IGV ({rate_str})"


def format_dimensions(
    *,
    width: Decimal | None = None,
    height: Decimal | None = None,
    length: Decimal | None = None,
    depth: Decimal | None = None,
    unit: str = "cm",
) -> str | None:
    """Formatea las dimensiones tecnicas omitiendo campos nulos o no aplicables."""
    parts: list[str] = []
    if width is not None and width > 0:
        parts.append(f"Ancho: {format_quantity(width)} {unit}")
    if height is not None and height > 0:
        parts.append(f"Alto: {format_quantity(height)} {unit}")
    if length is not None and length > 0:
        parts.append(f"Largo: {format_quantity(length)} {unit}")
    if depth is not None and depth > 0:
        parts.append(f"Profundidad: {format_quantity(depth)} {unit}")
    return " | ".join(parts) if parts else None


def format_date_display(dt: date | datetime | None) -> str:
    """Formatea fechas en formato DD/MM/AAAA."""
    if dt is None:
        return "-"
    if isinstance(dt, datetime):
        return dt.strftime("%d/%m/%Y")
    return dt.strftime("%d/%m/%Y")


def sanitize_pdf_filename(code: str, customer_name: str | None = None) -> str:
    """Genera un nombre de archivo seguro para descarga de PDF, previniendo inyeccion."""
    clean_code = re.sub(r"[^A-Za-z0-9_-]", "", code.strip()) or "COTIZACION"
    if customer_name and customer_name.strip():
        clean_name = re.sub(r"[^A-Za-z0-9_-]", "_", customer_name.strip())
        clean_name = re.sub(r"_+", "_", clean_name).strip("_")
        if clean_name:
            return f"{clean_code}_{clean_name[:40]}.pdf"
    return f"{clean_code}.pdf"


@dataclass(slots=True)
class CompanyDocInfo:
    legal_name: str | None = None
    trade_name: str | None = None
    tax_id: str | None = None  # RUC
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    logo_data_uri: str | None = None

    @property
    def display_name(self) -> str:
        return self.trade_name or self.legal_name or "EMPRESA"


@dataclass(slots=True)
class CustomerDocInfo:
    name: str | None = None
    trade_name: str | None = None
    document_type: str | None = None  # RUC / DNI
    document_number: str | None = None
    address: str | None = None
    ubigeo: str | None = None
    email: str | None = None
    phone: str | None = None

    @property
    def document_label(self) -> str | None:
        if self.document_type and self.document_number:
            return f"{self.document_type}: {self.document_number}"
        return self.document_number or None

    @property
    def display_name(self) -> str:
        return self.name or self.trade_name or "Cliente General"


@dataclass(slots=True)
class DocumentHeaderInfo:
    title: str = "COTIZACIÓN"
    code: str = ""
    name: str | None = None
    status: str = "CONFIRMED"
    is_cancelled: bool = False
    emission_date: str = ""
    validity_date: str | None = None
    currency_symbol: str = "S/"
    currency_code: str | None = "PEN"


@dataclass(slots=True)
class QuotationDocItem:
    item_number: int
    product_name: str
    product_reference: str | None = None
    material: str | None = None
    grammage_formatted: str | None = None
    dimensions_formatted: str | None = None
    quantity: int = 1
    quantity_formatted: str = "1"
    unit_of_measure: str = "NIU"
    unit_price_formatted: str = "S/ 0.00"
    subtotal_formatted: str = "S/ 0.00"


@dataclass(slots=True)
class QuotationDocTotals:
    subtotal_formatted: str = "S/ 0.00"
    tax_percentage: Decimal = Decimal(0)
    tax_label: str = "IGV (18%)"
    tax_amount_formatted: str = "S/ 0.00"
    total_formatted: str = "S/ 0.00"
    unit_price_with_tax_formatted: str | None = None


@dataclass(slots=True)
class CommercialDocConditions:
    validity_text: str | None = None
    general_conditions: str | None = None
    payment_notes: str | None = None
    document_footer: str | None = None


@dataclass(slots=True)
class BankAccountDocInfo:
    bank_name: str | None = None
    account_holder: str | None = None
    account_number: str | None = None
    cci: str | None = None
    notes: str | None = None


@dataclass(slots=True)
class QuotationPdfDocument:
    company: CompanyDocInfo
    customer: CustomerDocInfo
    document: DocumentHeaderInfo
    items: list[QuotationDocItem] = field(default_factory=list)
    totals: QuotationDocTotals = field(default_factory=QuotationDocTotals)
    conditions: CommercialDocConditions = field(default_factory=CommercialDocConditions)
    bank_accounts: list[BankAccountDocInfo] = field(default_factory=list)


def _build_company_doc_info(
    company_settings: CompanySettings | None,
    logo_data_uri: str | None = None,
) -> CompanyDocInfo:
    address_parts = []
    if company_settings:
        if company_settings.address_line1:
            address_parts.append(company_settings.address_line1)
        if company_settings.address_line2:
            address_parts.append(company_settings.address_line2)
        loc = [
            p
            for p in (
                company_settings.district,
                company_settings.province,
                company_settings.department,
            )
            if p
        ]
        if loc:
            address_parts.append(", ".join(loc))

    return CompanyDocInfo(
        legal_name=company_settings.legal_name if company_settings else None,
        trade_name=company_settings.trade_name if company_settings else None,
        tax_id=company_settings.tax_id if company_settings else None,
        address=" - ".join(address_parts) if address_parts else None,
        phone=(company_settings.phone or company_settings.mobile) if company_settings else None,
        email=company_settings.email if company_settings else None,
        website=company_settings.website if company_settings else None,
        logo_data_uri=logo_data_uri,
    )


def _build_conditions_doc(
    commercial_settings: CommercialSettings | None,
) -> CommercialDocConditions:
    return CommercialDocConditions(
        validity_text=f"Cotización válida por {commercial_settings.quote_validity_days} días."
        if (commercial_settings and commercial_settings.quote_validity_days)
        else None,
        general_conditions=commercial_settings.general_conditions if commercial_settings else None,
        payment_notes=commercial_settings.payment_notes if commercial_settings else None,
        document_footer=commercial_settings.document_footer if commercial_settings else None,
    )


def _build_bank_accounts_doc(
    commercial_settings: CommercialSettings | None,
) -> list[BankAccountDocInfo]:
    bank_accounts_doc: list[BankAccountDocInfo] = []
    if commercial_settings and commercial_settings.bank_accounts:
        for account in commercial_settings.bank_accounts:
            if account.bank_name or account.account_number or account.cci:
                bank_accounts_doc.append(
                    BankAccountDocInfo(
                        bank_name=account.bank_name,
                        account_holder=account.account_holder,
                        account_number=account.account_number,
                        cci=account.cci,
                        notes=account.notes,
                    )
                )
    return bank_accounts_doc


def build_quotation_pdf_document(
    quotation: Quotation,
    company_settings: CompanySettings | None = None,
    commercial_settings: CommercialSettings | None = None,
    logo_data_uri: str | None = None,
) -> QuotationPdfDocument:
    """Construye el ViewModel del PDF comercial a partir de snapshots congelados."""
    # 0. Moneda comercial (Prioridad: snapshot congelado de la cotizacion)
    currency_symbol = (
        quotation.currency_symbol_snapshot
        or (commercial_settings.currency_symbol if commercial_settings else None)
        or "S/"
    )
    currency_code = (
        quotation.currency_code_snapshot
        or (commercial_settings.currency_code if commercial_settings else None)
        or "PEN"
    )

    # 1. Informacion de empresa
    company_doc = _build_company_doc_info(company_settings, logo_data_uri)

    # 2. Informacion de cliente (EXCLUSIVAMENTE DESDE SNAPSHOTS)
    customer_doc = CustomerDocInfo(
        name=quotation.customer_name_snapshot,
        trade_name=quotation.customer_trade_name_snapshot,
        document_type=quotation.customer_document_type_snapshot,
        document_number=quotation.customer_document_number_snapshot,
        address=quotation.customer_address_snapshot,
        ubigeo=quotation.customer_ubigeo_snapshot,
        email=quotation.customer_email_snapshot,
        phone=quotation.customer_phone_snapshot,
    )

    # 3. Informacion del documento
    is_cancelled = quotation.status == QuotationStatus.CANCELLED
    emission_date_val = quotation.confirmed_at or quotation.created_at
    emission_date_str = format_date_display(emission_date_val)

    validity_date_str = None
    if commercial_settings and commercial_settings.quote_validity_days and emission_date_val:
        validity_days = commercial_settings.quote_validity_days
        validity_date_str = f"{validity_days} días calendario"

    document_header = DocumentHeaderInfo(
        title="COTIZACIÓN",
        code=quotation.code,
        name=quotation.name,
        status=quotation.status.value,
        is_cancelled=is_cancelled,
        emission_date=emission_date_str,
        validity_date=validity_date_str,
        currency_symbol=currency_symbol,
        currency_code=currency_code,
    )

    # 4. Items comerciales (EXCLUSIVAMENTE DESDE SNAPSHOTS)
    items: list[QuotationDocItem] = []
    quotation_items = [] if "items" in sa_inspect(quotation).unloaded else quotation.items
    if quotation_items:
        for index, line in enumerate(quotation_items, start=1):
            grammage_str = (
                f"{format_quantity(line.product_grammage_snapshot)} g"
                if line.product_grammage_snapshot is not None and line.product_grammage_snapshot > 0
                else None
            )
            quantity = line.quantity or 0
            items.append(
                QuotationDocItem(
                    item_number=index,
                    product_name=line.product_name_snapshot,
                    product_reference=line.product_internal_reference_snapshot,
                    material=line.product_material_snapshot,
                    grammage_formatted=grammage_str,
                    dimensions_formatted=format_dimensions(
                        width=line.product_width_snapshot,
                        height=line.product_height_snapshot,
                        length=line.product_length_snapshot,
                        depth=line.product_depth_snapshot,
                    ),
                    quantity=quantity,
                    quantity_formatted=format_quantity(quantity),
                    unit_of_measure=line.product_uom_snapshot or "NIU",
                    unit_price_formatted=format_currency(
                        line.commercial_sale_unit_price, currency_symbol
                    ),
                    subtotal_formatted=format_currency(line.commercial_subtotal, currency_symbol),
                )
            )
    else:
        # Compatibilidad con objetos legacy/transitorios de pruebas. En base de
        # datos 0012 migra cada cabecera historica a una linea equivalente.
        if quotation.quantity is None:
            raise ValueError("La cotizacion no contiene productos")
        grammage_str = (
            f"{format_quantity(quotation.product_grammage_snapshot)} g"
            if quotation.product_grammage_snapshot is not None
            and quotation.product_grammage_snapshot > 0
            else None
        )
        items.append(
            QuotationDocItem(
                item_number=1,
                product_name=quotation.product_name_snapshot
                or (quotation.product.name if quotation.product else "Producto"),
                product_reference=quotation.product_internal_reference_snapshot,
                material=quotation.product_material_snapshot,
                grammage_formatted=grammage_str,
                dimensions_formatted=format_dimensions(
                    width=quotation.product_width_snapshot,
                    height=quotation.product_height_snapshot,
                    length=quotation.product_length_snapshot,
                    depth=quotation.product_depth_snapshot,
                ),
                quantity=quotation.quantity,
                quantity_formatted=format_quantity(quotation.quantity),
                unit_of_measure=quotation.product_uom_snapshot or "NIU",
                unit_price_formatted=format_currency(
                    quotation.commercial_sale_unit_price, currency_symbol
                ),
                subtotal_formatted=format_currency(quotation.commercial_subtotal, currency_symbol),
            )
        )

    # 5. Totales e impuestos comerciales (EXCLUSIVAMENTE DESDE SNAPSHOTS)
    tax_percent = quotation.tax_percentage_snapshot
    # REGLA: commercial_subtotal + commercial_tax_amount == commercial_total
    commercial_tax_amount = quotation.commercial_total - quotation.commercial_subtotal
    tax_label = (
        f"IGV (tasa efectiva {format_quantity(tax_percent)}%)"
        if quotation.tax_rate_source_snapshot == "MIXED"
        else format_tax_label(tax_percent)
    )
    totals_doc = QuotationDocTotals(
        subtotal_formatted=format_currency(quotation.commercial_subtotal, currency_symbol),
        tax_percentage=tax_percent,
        tax_label=tax_label,
        tax_amount_formatted=format_currency(commercial_tax_amount, currency_symbol),
        total_formatted=format_currency(quotation.commercial_total, currency_symbol),
        unit_price_with_tax_formatted=format_currency(
            quotation.commercial_unit_price_with_tax, currency_symbol
        )
        if quotation.commercial_unit_price_with_tax
        else None,
    )

    # 6. Condiciones comerciales
    conditions_doc = _build_conditions_doc(commercial_settings)

    # 7. Cuentas bancarias comerciales
    bank_accounts_doc = _build_bank_accounts_doc(commercial_settings)

    return QuotationPdfDocument(
        company=company_doc,
        customer=customer_doc,
        document=document_header,
        items=items,
        totals=totals_doc,
        conditions=conditions_doc,
        bank_accounts=bank_accounts_doc,
    )


def build_draft_quotation_pdf_document(
    quotation_out: QuotationBuilderOut,
    customer: Partner | None = None,
    company_settings: CompanySettings | None = None,
    commercial_settings: CommercialSettings | None = None,
    logo_data_uri: str | None = None,
) -> QuotationPdfDocument:
    """Construye el ViewModel del PDF comercial de previsualizacion a partir del borrador DRAFT."""
    currency_symbol = (
        quotation_out.currency_symbol_snapshot
        or (commercial_settings.currency_symbol if commercial_settings else None)
        or "S/"
    )
    currency_code = (
        quotation_out.currency_code_snapshot
        or (commercial_settings.currency_code if commercial_settings else None)
        or "PEN"
    )

    # 1. Informacion de empresa
    company_doc = _build_company_doc_info(company_settings, logo_data_uri)

    # 2. Informacion de cliente (del maestro asociado o datos del borrador)
    if customer is not None:
        customer_address_parts = []
        if customer.address:
            customer_address_parts.append(customer.address)
        loc = [
            p
            for p in (
                customer.district,
                customer.province,
                customer.department,
            )
            if p
        ]
        if loc:
            customer_address_parts.append(", ".join(loc))
        customer_doc = CustomerDocInfo(
            name=customer.name,
            trade_name=None,
            document_type=customer.document_type.value if customer.document_type else None,
            document_number=customer.document_number,
            address=" - ".join(customer_address_parts) if customer_address_parts else None,
            ubigeo=customer.ubigeo_code,
            email=customer.email,
            phone=customer.phone or customer.mobile,
        )
    else:
        customer_doc = CustomerDocInfo(
            name=quotation_out.customer_name_snapshot or "Cliente General",
            trade_name=None,
            document_type=None,
            document_number=None,
            address=None,
            ubigeo=None,
            email=None,
            phone=None,
        )

    # 3. Informacion del documento
    emission_date_val = quotation_out.updated_at or quotation_out.created_at or datetime.now()
    emission_date_str = format_date_display(emission_date_val)

    validity_date_str = None
    if commercial_settings and commercial_settings.quote_validity_days:
        validity_days = commercial_settings.quote_validity_days
        validity_date_str = f"{validity_days} días calendario"

    document_header = DocumentHeaderInfo(
        title="COTIZACIÓN",
        code=quotation_out.code or "BORRADOR",
        name=quotation_out.name,
        status="DRAFT",
        is_cancelled=False,
        emission_date=emission_date_str,
        validity_date=validity_date_str,
        currency_symbol=currency_symbol,
        currency_code=currency_code,
    )

    # 4. Items comerciales
    items: list[QuotationDocItem] = []
    for index, line in enumerate(quotation_out.items, start=1):
        grammage_str = (
            f"{format_quantity(line.product_grammage)} g"
            if line.product_grammage is not None and line.product_grammage > 0
            else None
        )
        quantity = line.quantity or 0
        items.append(
            QuotationDocItem(
                item_number=index,
                product_name=line.product_name,
                product_reference=line.product_internal_reference,
                material=line.product_material,
                grammage_formatted=grammage_str,
                dimensions_formatted=format_dimensions(
                    width=line.width,
                    height=line.height,
                    length=line.length,
                    depth=line.depth,
                ),
                quantity=quantity,
                quantity_formatted=format_quantity(quantity),
                unit_of_measure=line.product_uom or "NIU",
                unit_price_formatted=format_currency(
                    line.commercial_sale_unit_price, currency_symbol
                ),
                subtotal_formatted=format_currency(line.commercial_subtotal, currency_symbol),
            )
        )

    # 5. Totales e impuestos comerciales
    tax_percent = quotation_out.tax_percentage_snapshot
    commercial_tax_amount = quotation_out.tax_amount
    tax_label = (
        f"IGV (tasa efectiva {format_quantity(tax_percent)}%)"
        if quotation_out.tax_rate_source_snapshot == "MIXED"
        else format_tax_label(tax_percent)
    )
    totals_doc = QuotationDocTotals(
        subtotal_formatted=format_currency(quotation_out.commercial_subtotal, currency_symbol),
        tax_percentage=tax_percent,
        tax_label=tax_label,
        tax_amount_formatted=format_currency(commercial_tax_amount, currency_symbol),
        total_formatted=format_currency(quotation_out.total_with_tax, currency_symbol),
        unit_price_with_tax_formatted=None,
    )

    # 6. Condiciones comerciales
    conditions_doc = _build_conditions_doc(commercial_settings)

    # 7. Cuentas bancarias comerciales
    bank_accounts_doc = _build_bank_accounts_doc(commercial_settings)

    return QuotationPdfDocument(
        company=company_doc,
        customer=customer_doc,
        document=document_header,
        items=items,
        totals=totals_doc,
        conditions=conditions_doc,
        bank_accounts=bank_accounts_doc,
    )
