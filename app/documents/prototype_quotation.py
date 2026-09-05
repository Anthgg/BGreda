"""El documento que ve el cliente de una cotizacion de prototipo.

Comparte todo lo que identifica a la empresa —logo, datos, condiciones, cuentas
bancarias, pie— con el resto de documentos: eso vive en `base_document.html` y
en `app/documents/common.py`, y duplicarlo daria dos identidades visuales que
se separan a la primera vez que alguien cambia una.

Lo unico propio es el cuerpo, porque lo que se cotiza es distinto: no hay lista
de piezas con gramaje y receta, hay UNA muestra con sus medidas y un plazo.

**El desglose interno no sale de aqui.** El cliente ve un concepto y un precio.
Cuanto se paga al artista por dia, cuanto cuesta el barro en el maestro o que
tarifa tiene el horno son numeros de la casa: imprimirlos entregaria el margen
en la misma hoja que el presupuesto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.documents.common import CompanyDocInfo
from app.documents.quotation import (
    BankAccountDocInfo,
    CommercialDocConditions,
    CustomerDocInfo,
    DocumentHeaderInfo,
    format_currency,
)


@dataclass(slots=True)
class PrototypeDocLine:
    """El concepto comercial. Uno, y sin explicar como se compone."""

    description: str
    quantity: int
    quantity_formatted: str
    unit_price_formatted: str
    subtotal_formatted: str


@dataclass(slots=True)
class PrototypeDocSpecs:
    """Lo acordado sobre la pieza. Datos del encargo, no costos."""

    dimensions: str | None = None
    finish: str | None = None
    color: str | None = None
    technique: str | None = None


@dataclass(slots=True)
class PrototypeDocDeadline:
    """El plazo, en dias, sin abrir en que se van.

    Cuantos dias de diseno frente a cuantos de artista es informacion de
    produccion: al cliente le importa cuando lo tiene.
    """

    estimated_days: str
    target_date: str | None = None


@dataclass(slots=True)
class PrototypeDocTotals:
    subtotal_formatted: str
    tax_label: str
    tax_formatted: str
    total_formatted: str


@dataclass(slots=True)
class PrototypeQuotationPdfDocument:
    company: CompanyDocInfo
    customer: CustomerDocInfo
    document: DocumentHeaderInfo
    lines: list[PrototypeDocLine] = field(default_factory=list)
    specs: PrototypeDocSpecs = field(default_factory=PrototypeDocSpecs)
    deadline: PrototypeDocDeadline | None = None
    totals: PrototypeDocTotals = field(default_factory=lambda: PrototypeDocTotals("", "", "", ""))
    conditions: CommercialDocConditions = field(default_factory=CommercialDocConditions)
    bank_accounts: list[BankAccountDocInfo] = field(default_factory=list)


def _medida(valor: Decimal | None) -> str | None:
    if valor is None:
        return None
    return format(valor.normalize(), "f")


def build_prototype_dimensions(
    width: Decimal | None,
    length: Decimal | None,
    height: Decimal | None,
    depth: Decimal | None,
) -> str | None:
    """«15 x 15 x 20 cm». Solo las medidas declaradas.

    Rellenar las que faltan con ceros diria que la pieza mide cero de alto, que
    es distinto de no haberlo acordado.
    """
    partes = [
        texto
        for texto in (_medida(valor) for valor in (width, length, height, depth))
        if texto is not None
    ]
    if not partes:
        return None
    return f"{' x '.join(partes)} cm"


def build_prototype_line(
    descripcion: str,
    cantidad: int,
    neto: Decimal,
    simbolo: str,
) -> PrototypeDocLine:
    """El concepto unico del documento.

    El precio unitario es el neto entre las muestras, y se muestra porque el
    cliente pregunta cuanto cuesta cada una. No es el costo: es el precio.
    """
    unitario = neto / cantidad if cantidad else neto
    return PrototypeDocLine(
        description=descripcion,
        quantity=cantidad,
        quantity_formatted=str(cantidad),
        unit_price_formatted=format_currency(unitario, simbolo),
        subtotal_formatted=format_currency(neto, simbolo),
    )


def build_prototype_totals(
    neto: Decimal,
    impuesto: Decimal,
    total: Decimal,
    porcentaje: Decimal,
    simbolo: str,
) -> PrototypeDocTotals:
    """Los tres numeros del pie, ya calculados aguas arriba.

    Aqui no se redondea ni se reconstruye nada: el escalon comercial se aplico
    una sola vez al valorar, y volver a tocarlo daria un documento que no
    coincide con el total guardado.
    """
    return PrototypeDocTotals(
        subtotal_formatted=format_currency(neto, simbolo),
        tax_label=f"IGV ({format(porcentaje.normalize(), 'f')}%)",
        tax_formatted=format_currency(impuesto, simbolo),
        total_formatted=format_currency(total, simbolo),
    )
