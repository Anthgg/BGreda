"""Modelos de vista y formateadores para el documento comercial de cotizacion."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect

from app.documents.common import (
    CompanyDocInfo,
    build_company_doc_info,
    format_date_display,
    sanitize_pdf_filename,
)
from app.models.quotations import Quotation, QuotationStatus
from app.models.settings import CommercialSettings, CompanySettings

#: El documento comercial siempre se ha construido con este nombre privado. Se
#: mantiene como alias del comun para no reescribir el resto del modulo cuando
#: 009I.1 mueve la identidad de la empresa al sistema documental compartido.
_build_company_doc_info = build_company_doc_info

__all__ = [
    "CompanyDocInfo",
    "format_date_display",
    "sanitize_pdf_filename",
]

if TYPE_CHECKING:
    from app.models.masters import Partner
    from app.schemas.quotation_builder import CommercialLineOut, QuotationBuilderOut


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


#: Simbolo de la moneda BASE del sistema. No sale de `commercial_settings`
#: a proposito: ese ajuste guarda el simbolo de la moneda configurada, que
#: puede ser el dolar, y aqui hace falta siempre el del sol, que es contra lo
#: que se cotiza la tasa.
BASE_CURRENCY_SYMBOL = "S/"


def format_exchange_rate(rate: Decimal | None, currency_code: str | None) -> str | None:
    """La tasa tal y como se lee: «1 USD = S/ 3.31».

    En soles devuelve None. Una cotizacion en PEN no se convirtio, y ensenarle
    una tasa al cliente afirmaria un cambio de moneda que nunca ocurrio.

    Los ceros de la escala se recortan —la columna guarda 3.310000— pero nunca
    por debajo de dos decimales, para que 4 se lea como 4.00 y no como un
    numero redondo sospechoso de estar truncado.
    """
    if rate is None or not currency_code or currency_code == "PEN":
        return None
    entero, _, decimales = f"{rate.normalize():f}".partition(".")
    return f"1 {currency_code} = {BASE_CURRENCY_SYMBOL} {entero}.{decimales.ljust(2, '0')}"


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
    #: Fase 009G. Ya formateado —«1 USD = S/ 3.31»— porque la plantilla solo
    #: renderiza. Nulo en soles: ahi no hubo conversion, y ensenar una tasa
    #: describiria un cambio de moneda que nunca ocurrio.
    exchange_rate_text: str | None = None


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
class QuotationDocCommercialLine:
    """Un cargo comercial en el documento.

    No reutiliza `QuotationDocItem` a proposito: aquel tiene material, gramaje,
    dimensiones y unidad de medida, y un cargo no tiene ninguna de esas cosas.
    Rellenarlas con huecos o inventarle una unidad le daria al cliente la
    apariencia de un producto que no existe.
    """

    item_number: int
    description: str
    quantity: int = 1
    quantity_formatted: str = "1"
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
    #: Fase 009K.1. Conceptos que se cobran y no se fabrican.
    commercial_lines: list[QuotationDocCommercialLine] = field(default_factory=list)
    totals: QuotationDocTotals = field(default_factory=QuotationDocTotals)
    conditions: CommercialDocConditions = field(default_factory=CommercialDocConditions)
    bank_accounts: list[BankAccountDocInfo] = field(default_factory=list)


def _build_conditions_doc(
    commercial_settings: CommercialSettings | None,
    validity_days: int | None,
) -> CommercialDocConditions:
    """Las condiciones del documento, con la vigencia que le corresponda.

    `validity_days` llega decidido por quien construye el documento, y no se
    vuelve a mirar la configuracion aqui: una confirmada trae su plazo
    congelado y un borrador el vigente. Resolverlo dentro haria imposible que
    los dos casos convivieran sin que uno pisara al otro.
    """
    return CommercialDocConditions(
        validity_text=f"Cotización válida por {validity_days} días." if validity_days else None,
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


def _build_commercial_lines_doc(
    quotation: Quotation, currency_symbol: str, offset: int
) -> list[QuotationDocCommercialLine]:
    """Los cargos comerciales del documento, numerados tras los productos.

    El importe que se imprime es el NETO que se tecleo, que es el mismo que
    entro en el subtotal. No se recalcula nada aqui: el PDF muestra lo que el
    backend decidio, y un documento que rehiciera la aritmetica podria acabar
    imprimiendo un total distinto del que se guardo.

    Del cargo NO sale al papel ni el prototipo del que viene ni ningun dato
    fisico: el cliente ve un concepto y un importe.
    """
    if "commercial_lines" in sa_inspect(quotation).unloaded:
        return []
    return [
        QuotationDocCommercialLine(
            item_number=offset + index,
            description=line.description,
            quantity=line.quantity,
            quantity_formatted=format_quantity(Decimal(line.quantity)),
            unit_price_formatted=format_currency(line.manual_net_amount, currency_symbol),
            subtotal_formatted=format_currency(
                line.manual_net_amount * Decimal(line.quantity), currency_symbol
            ),
        )
        for index, line in enumerate(quotation.commercial_lines, start=1)
    ]


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

    # Fase 009G. La vigencia sale del snapshot congelado al confirmar, nunca de
    # la configuracion viva: este documento ya se entrego, y cambiar hoy el
    # ajuste no puede reescribir lo que decia. Sin snapshot —las confirmadas
    # anteriores a 009G— no se muestra vigencia: no hay registro de cual era, y
    # poner la de hoy seria inventarle un dato contractual.
    validity_days = quotation.validity_days_snapshot
    validity_date_str = f"{validity_days} días calendario" if validity_days else None

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
        # Del snapshot congelado de la cotizacion, nunca de la configuracion
        # actual: es la tasa con la que se calculo este documento. Si una
        # confirmada en dolares no la tiene, es una incoherencia historica y se
        # calla; poner la de hoy explicaria el precio con un numero que no lo
        # produjo.
        exchange_rate_text=format_exchange_rate(quotation.exchange_rate_snapshot, currency_code),
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
    conditions_doc = _build_conditions_doc(commercial_settings, validity_days)

    # 7. Cuentas bancarias comerciales
    bank_accounts_doc = _build_bank_accounts_doc(commercial_settings)

    # 8. Cargos comerciales (Fase 009K.1)
    commercial_doc = _build_commercial_lines_doc(quotation, currency_symbol, len(items))

    return QuotationPdfDocument(
        company=company_doc,
        customer=customer_doc,
        document=document_header,
        items=items,
        commercial_lines=commercial_doc,
        totals=totals_doc,
        conditions=conditions_doc,
        bank_accounts=bank_accounts_doc,
    )


def _build_draft_commercial_lines_doc(
    lines: Sequence[CommercialLineOut], currency_symbol: str, offset: int
) -> list[QuotationDocCommercialLine]:
    """Los mismos cargos, para el PDF del borrador.

    El borrador se construye desde el esquema y la confirmada desde el ORM, asi
    que hay dos constructores. Lo que NO puede haber son dos documentos: quien
    revisa la previsualizacion tiene que ver exactamente lo que va a firmar, y
    un cargo que aparece solo al confirmar es una sorpresa en la factura.
    """
    return [
        QuotationDocCommercialLine(
            item_number=offset + index,
            description=line.description,
            quantity=line.quantity,
            quantity_formatted=format_quantity(Decimal(line.quantity)),
            unit_price_formatted=format_currency(line.manual_net_amount, currency_symbol),
            subtotal_formatted=format_currency(
                line.manual_net_amount * Decimal(line.quantity), currency_symbol
            ),
        )
        for index, line in enumerate(lines, start=1)
    ]


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

    # Un borrador no tiene vigencia congelada porque todavia no se ha emitido
    # nada: aqui la configuracion vigente es la respuesta correcta, y el numero
    # que se ve es el que quedara guardado si se confirma hoy.
    validity_days = commercial_settings.quote_validity_days if commercial_settings else None
    validity_date_str = f"{validity_days} días calendario" if validity_days else None

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
        # La tasa que el borrador lleva puesta ahora mismo. Es la que se
        # congelara si se confirma, asi que la previsualizacion tiene que
        # ensenar exactamente esa.
        exchange_rate_text=format_exchange_rate(
            quotation_out.exchange_rate_snapshot, currency_code
        ),
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
    conditions_doc = _build_conditions_doc(commercial_settings, validity_days)

    # 7. Cuentas bancarias comerciales
    bank_accounts_doc = _build_bank_accounts_doc(commercial_settings)

    # 8. Cargos comerciales (Fase 009K.1)
    commercial_doc = _build_draft_commercial_lines_doc(
        quotation_out.commercial_lines, currency_symbol, len(items)
    )

    return QuotationPdfDocument(
        company=company_doc,
        customer=customer_doc,
        document=document_header,
        items=items,
        commercial_lines=commercial_doc,
        totals=totals_doc,
        conditions=conditions_doc,
        bank_accounts=bank_accounts_doc,
    )
