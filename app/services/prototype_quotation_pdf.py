"""Render del PDF de una cotizacion de prototipo.

**Es el MISMO documento que una cotizacion de producto.** No hay plantilla
propia ni ViewModel propio: se arma un `QuotationPdfDocument` y lo dibuja
`QuotationPdfService`, que es quien sabe de logo, datos de empresa, cabecera,
cliente, tabla, caja de totales, condiciones, cuentas bancarias, pie y
paginacion.

Antes esto tenia su propia plantilla. Extendia `base_document.html`, si, pero
redeclaraba el grid del cliente, la caja de totales y las tarjetas de
condiciones con clases propias, de modo que el papel salia con el mismo membrete
y otro sistema visual —una caja de totales sin borde ni fondo al lado de la de
un CTZ delata que son dos aplicaciones—. Un CPR tiene que ser hermano directo de
un CTZ: lo unico que cambia es el TIPO de documento, el correlativo y el cuerpo.

Un documento CONFIRMADO se dibuja con lo que congelo. Regenerar el PDF de una
cotizacion firmada leyendo la configuracion de hoy daria un papel distinto del
que se entrego, y el cliente conserva el suyo.

Aqui NO se calcula dinero. Los tres numeros salen congelados de la fila: el
escalon comercial se aplico una sola vez al emitir, y volver a tocarlo daria un
documento que no coincide con el total guardado.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import nombre_de_actor
from app.core.errors import APIError
from app.documents.common import (
    build_company_doc_info,
    format_date_display,
    sanitize_pdf_filename,
)
from app.documents.quotation import (
    CommercialDocConditions,
    CustomerDocInfo,
    DocumentHeaderInfo,
    QuotationDocItem,
    QuotationDocTotals,
    QuotationPdfDocument,
    _build_bank_accounts_doc,
    _build_conditions_doc,
    format_currency,
    format_dimensions,
    format_exchange_rate,
    format_quantity,
)
from app.models.masters import Partner
from app.models.prototype_quotations import PrototypeQuotation, PrototypeQuotationStatus
from app.services.quotation_pdf import QuotationPdfService

ZERO = Decimal(0)
TITULO = "COTIZACIÓN DE PROTOTIPO"
CONCEPTO = "Desarrollo de prototipo"

#: Un prototipo se cotiza por muestras, no por kilos ni por metros.
UNIDAD = "und"


class PrototypeQuotationPdfDraftBlockedError(APIError):
    """Un borrador no es un documento: todavia no tiene numero ni precio firme.

    Dejar descargarlo invitaria a enviarle al cliente un papel que puede cambiar
    al dia siguiente y que no se puede referenciar por codigo.
    """

    status_code = 409
    code = "PROTOTYPE_QUOTATION_PDF_DRAFT_BLOCKED"
    message = "Emita la cotizacion de prototipo antes de generar su PDF"


class PrototypeQuotationPdfService:
    """El documento del cliente. Presenta; no calcula."""

    def __init__(self, session: AsyncSession, base: QuotationPdfService) -> None:
        self._session = session
        # Sin Jinja propio y sin plantilla propia: el motor documental es el del
        # Cotizador, y por eso los dos papeles salen identicos.
        self._base = base

    async def render(self, fila: PrototypeQuotation) -> tuple[bytes, str]:
        if fila.status is PrototypeQuotationStatus.DRAFT:
            raise PrototypeQuotationPdfDraftBlockedError()

        cliente = await self._session.get(Partner, fila.customer_id) if fila.customer_id else None

        empresa = await self._base._get_company_settings()
        comercial = await self._base._get_commercial_settings()
        logo = await self._base._resolve_logo_data_uri(empresa)

        documento = self._build_document(fila, cliente, empresa, comercial, logo)
        html = self._base.render_html(documento)
        pdf = await asyncio.to_thread(self._base.render_pdf_from_html, html)
        return pdf, sanitize_pdf_filename(fila.code or "PROTOTIPO", fila.customer_name_snapshot)

    def _build_document(
        self,
        fila: PrototypeQuotation,
        cliente: Partner | None,
        empresa: object,
        comercial: object,
        logo: str | None,
    ) -> QuotationPdfDocument:
        simbolo = fila.currency_symbol_snapshot or "S/"
        moneda = fila.currency_code_snapshot or "PEN"
        # Los tres numeros salen CONGELADOS de la fila.
        neto = fila.commercial_net_total or ZERO
        impuesto = fila.commercial_tax_total or ZERO
        total = fila.commercial_gross_total or ZERO
        porcentaje = fila.tax_percent_snapshot or ZERO
        unitario = neto / fila.quantity if fila.quantity else neto

        return QuotationPdfDocument(
            company=build_company_doc_info(empresa, logo),  # type: ignore[arg-type]
            customer=_cliente_doc(cliente, fila.customer_name_snapshot),
            document=DocumentHeaderInfo(
                title=TITULO,
                code=fila.code or "",
                # La cabecera del Cotizador lo imprime como «Referencia /
                # Nombre», que es exactamente lo que se pacto desarrollar.
                name=fila.description,
                status=fila.status.value,
                is_cancelled=fila.status is PrototypeQuotationStatus.CANCELLED,
                emission_date=format_date_display(fila.confirmed_at or fila.created_at),
                # Sin vigencia congelada: una cotizacion de prototipo no la
                # declara todavia. Poner la de configuracion aqui dejaria en el
                # papel una fecha que nadie acordo.
                validity_date=None,
                currency_symbol=simbolo,
                currency_code=moneda,
                # Misma regla que el Cotizador: quien emitio manda sobre quien
                # escribio, y un CPR anterior a 009K.2 —sin actor de emision—
                # cae en su creador, que si se registraba desde 009K.1.1.
                prepared_by=nombre_de_actor(fila.confirmed_by_name or fila.created_by_name),
                # En soles sale None y la fila no se dibuja: no hubo conversion
                # que contar.
                exchange_rate_text=format_exchange_rate(fila.exchange_rate_snapshot, moneda),
            ),
            items=[
                QuotationDocItem(
                    item_number=1,
                    product_name=CONCEPTO,
                    # Ni codigo de catalogo, ni material, ni gramaje: un
                    # prototipo no es todavia un producto del maestro, y
                    # rellenar esas casillas le daria apariencia de serie.
                    dimensions_formatted=format_dimensions(
                        width=fila.width_cm,
                        height=fila.height_cm,
                        length=fila.length_cm,
                        depth=fila.depth_cm,
                    ),
                    quantity=fila.quantity,
                    quantity_formatted=format_quantity(fila.quantity),
                    unit_of_measure=UNIDAD,
                    unit_price_formatted=format_currency(unitario, simbolo),
                    subtotal_formatted=format_currency(neto, simbolo),
                )
            ],
            totals=QuotationDocTotals(
                subtotal_formatted=format_currency(neto, simbolo),
                tax_percentage=porcentaje,
                tax_label=f"IGV ({format(porcentaje.normalize(), 'f')}%)",
                tax_amount_formatted=format_currency(impuesto, simbolo),
                total_formatted=format_currency(total, simbolo),
            ),
            conditions=_condiciones(fila, comercial),
            bank_accounts=_build_bank_accounts_doc(comercial),  # type: ignore[arg-type]
        )


def _condiciones(fila: PrototypeQuotation, comercial: object) -> CommercialDocConditions:
    """Las condiciones de la casa, con lo acordado de esta muestra encima.

    El plazo y lo pactado —tecnica, acabado, color— van en la tarjeta de
    Condiciones Comerciales del documento global, no en un bloque inventado.
    Son compromisos, no atributos de un producto de catalogo: en la tabla no
    tendrian columna, y en la tarjeta se leen donde el cliente ya busca lo
    acordado.
    """
    base = _build_conditions_doc(comercial, None)  # type: ignore[arg-type]

    plazo = None
    if fila.estimated_days is not None:
        plazo = f"Plazo estimado de desarrollo: {format(fila.estimated_days.normalize(), 'f')} días"
        if fila.target_date:
            plazo += f" (fecha objetivo: {format_date_display(fila.target_date)})"
        plazo += "."

    acordado = _acordado(fila.technical_specifications)
    generales = "\n".join(parte for parte in (acordado, base.general_conditions) if parte) or None

    return CommercialDocConditions(
        validity_text=plazo,
        general_conditions=generales,
        payment_notes=base.payment_notes,
        document_footer=base.document_footer,
    )


def _acordado(ficha: dict[str, object] | None) -> str | None:
    """Lo comercial de la ficha tecnica, y solo eso.

    Del cuaderno del taller sale mucho mas —responsable, prioridad, peso de
    pasta, notas internas—, pero al cliente le importa el acabado, el color y
    la tecnica porque forman parte de lo acordado. El resto se queda dentro.
    """
    if not ficha:
        return None
    lineas = [
        f"{etiqueta}: {valor}"
        for etiqueta, valor in (
            ("Técnica", _texto(ficha.get("technique"))),
            ("Acabado", _texto(ficha.get("finish"))),
            ("Color", _texto(ficha.get("color"))),
        )
        if valor
    ]
    return "\n".join(lineas) if lineas else None


def _cliente_doc(cliente: Partner | None, nombre: str | None) -> CustomerDocInfo:
    if cliente is None:
        return CustomerDocInfo(name=nombre or "Cliente")
    return CustomerDocInfo(
        name=cliente.name,
        document_type=cliente.document_type.value if cliente.document_type else None,
        document_number=cliente.document_number,
        address=cliente.address,
        email=cliente.email,
        phone=cliente.phone or cliente.mobile,
    )


def _texto(valor: object) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None
