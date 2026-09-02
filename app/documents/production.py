"""Modelos de los documentos de produccion.

Hay DOS, y estan separados a proposito:

- :class:`ProductionOrderDocument` es la hoja de taller. La lee quien fabrica,
  dentro del taller, con sesion iniciada. Lleva el almacen del que sale el
  material, que preparado toca y cuantos gramos.

- :class:`PublicTrackingDocument` es lo que ve alguien que escanea el QR sin
  tener cuenta. Lleva el codigo de la orden, en que punto va y que piezas son.
  Nada mas.

**La separacion no es cosmetica: es donde ocurre la sanitizacion.** El modelo
publico es un dataclass con `slots`, de modo que no tiene sitio donde guardar
un almacen, unos gramos o un identificador aunque alguien se lo pase. La
plantilla publica no puede ensenar lo que el modelo publico no puede contener,
y por eso la plantilla no necesita acordarse de ocultar nada.

Lo contrario —un unico modelo con campos opcionales y un `if es_publico` en la
plantilla— habria funcionado igual de bien hasta el dia que alguien anadiera
una fila sin mirar el `if`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.documents.common import CompanyDocInfo, DocFact, format_datetime_display
from app.models.production import ProductionOrder, ProductionOrderLine, ProductionOrderStatus

#: Titulo impreso de cada documento.
PRODUCTION_ORDER_TITLE = "Orden de producción"
PUBLIC_TRACKING_TITLE = "Seguimiento de producción"

#: Como se lee cada estado DENTRO del taller.
_INTERNAL_STATUS_LABELS = {
    ProductionOrderStatus.CREATED: "Creada",
    ProductionOrderStatus.STARTED: "En proceso",
    ProductionOrderStatus.COMPLETED: "Completada",
    ProductionOrderStatus.CANCELLED: "Anulada",
}

#: Como se lee cada estado FUERA. Mas explicito porque quien lo lee no conoce
#: el vocabulario de la casa: «Creada» no le dice si ya la estan haciendo.
_PUBLIC_STATUS_LABELS = {
    ProductionOrderStatus.CREATED: "Orden creada",
    ProductionOrderStatus.STARTED: "En producción",
    ProductionOrderStatus.COMPLETED: "Producción completada",
    ProductionOrderStatus.CANCELLED: "Orden anulada",
}

#: Tono visual de cada estado. El sistema documental conoce cinco tonos y
#: ninguno de estos cuatro estados; el mapa vive aqui, en produccion.
_STATUS_TONES = {
    ProductionOrderStatus.CREATED: "pending",
    ProductionOrderStatus.STARTED: "active",
    ProductionOrderStatus.COMPLETED: "done",
    ProductionOrderStatus.CANCELLED: "void",
}


def format_measure(value: Decimal | None, uom: str | None) -> str | None:
    """«2,012 g», no «2012.00 g».

    Los ceros de la escala se recortan porque la columna guarda seis decimales
    y nadie pesa barniz con seis decimales; pero solo los ceros: 12.5 sigue
    siendo 12.5. El separador de miles importa mas de lo que parece en una
    hoja impresa, donde 2012 y 20120 se confunden de un vistazo.
    """
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        # `normalize()` deja 1500 como Decimal("1.5E+3"), y `to_integral_value()`
        # CONSERVA ese exponente: formatearlo imprimiria «1.5E+3 g» en una hoja
        # de taller. `int()` es lo unico que devuelve la notacion posicional.
        texto = f"{int(normalized):,}"
    else:
        texto = f"{normalized:,f}"
    return f"{texto} {uom}" if uom else texto


def format_dimensions(line: ProductionOrderLine) -> str | None:
    """Medidas de la linea, en centimetros y con la inicial de cada eje.

    Salen del snapshot de la LINEA, nunca del maestro de productos: una
    cotizacion pudo fijar medidas propias (Fase 009B) y lo que hay que fabricar
    es aquello, no lo que diga el maestro hoy.
    """
    partes = []
    for etiqueta, valor in (
        ("A", line.width_snapshot),
        ("H", line.height_snapshot),
        ("L", line.length_snapshot),
        ("P", line.depth_snapshot),
    ):
        if valor:
            partes.append(f"{etiqueta} {valor.normalize():f}")
    return " · ".join(partes) if partes else None


# ---------------------------------------------------------------------------
# Hoja de taller (INTERNA)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ProductionDocLine:
    """Una pieza a fabricar, tal y como se imprime."""

    line_number: int
    product_name: str
    product_reference: str | None
    dimensions_formatted: str | None
    quantity_formatted: str
    prepared_product_name: str | None
    prepared_product_reference: str | None
    required_formatted: str | None
    #: Por que esta linea no tiene material asignado. Se imprime en vez de la
    #: columna vacia: en un papel, un hueco no se distingue de un descuido.
    missing_reason: str | None = None


@dataclass(slots=True)
class ProductionOrderDocument:
    """Hoja de taller completa. Interna: exige sesion para obtenerse."""

    company: CompanyDocInfo
    title: str
    code: str
    status_label: str
    status_tone: str
    is_cancelled: bool
    facts: list[DocFact] = field(default_factory=list)
    lines: list[ProductionDocLine] = field(default_factory=list)
    total_pieces: int = 0
    qr_data_uri: str | None = None
    qr_caption: str = ""


# ---------------------------------------------------------------------------
# Seguimiento (PUBLICO)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PublicTrackingItem:
    """Una pieza, como la nombraria quien la encargo."""

    product_name: str
    quantity: int | None


@dataclass(slots=True)
class PublicTrackingData:
    """LA frontera. Lo unico que puede salir de la aplicacion sin sesion.

    Si un campo no esta en esta lista, no hay forma de que llegue ni a la vista
    publica ni al documento publico. Ampliar esta clase es una decision
    deliberada sobre que deja de ser interno, no un detalle de maquetacion.

    Guarda valores CRUDOS, no frases: la fecha es una fecha y el estado es su
    codigo. Traducir aqui obligaria a desplegar el backend para corregir una
    errata de la interfaz, que es la misma regla que siguen los codigos de
    disponibilidad de 009I.
    """

    company_name: str
    order_code: str
    status: ProductionOrderStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    items: list[PublicTrackingItem] = field(default_factory=list)


@dataclass(slots=True)
class PublicTrackingStep:
    """Un hito de la linea de tiempo, ya escrito para imprimirlo."""

    label: str
    #: Nulo mientras el hito no ha ocurrido.
    happened_at: str | None
    done: bool


@dataclass(slots=True)
class PublicTrackingSheetItem:
    product_name: str
    quantity_formatted: str


@dataclass(slots=True)
class PublicTrackingSheet:
    """La MISMA informacion publica, ya en castellano, para el PDF.

    Existe porque un PDF se compone en el servidor y ahi no hay interfaz que
    traduzca. No es una segunda frontera: se construye desde
    `PublicTrackingData` y no puede ver nada que aquella no tenga.
    """

    company_name: str
    logo_data_uri: str | None
    title: str
    order_code: str
    status_label: str
    status_tone: str
    timeline: list[PublicTrackingStep] = field(default_factory=list)
    items: list[PublicTrackingSheetItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Constructores
# ---------------------------------------------------------------------------
def build_production_order_document(
    *,
    order: ProductionOrder,
    company: CompanyDocInfo,
    quotation_code: str | None,
    stock_location_name: str | None,
    prepared_names: dict[int, tuple[str, str]],
    qr_data_uri: str | None,
    qr_caption: str,
) -> ProductionOrderDocument:
    """Arma la hoja de taller.

    `prepared_names` mapea el id del preparado a (nombre, codigo interno). Se
    recibe ya resuelto para que este modulo no toque la base: construir un
    documento no puede depender de una sesion abierta.
    """
    facts = [
        DocFact("Cotización origen", quotation_code or "—"),
        DocFact("Almacén de salida", stock_location_name or "—"),
        DocFact("Creada", format_datetime_display(order.created_at) or "—"),
        DocFact("Arrancada", format_datetime_display(order.started_at) or "—"),
        DocFact("Completada", format_datetime_display(order.completed_at) or "—"),
    ]
    if order.cancelled_at is not None:
        facts.append(DocFact("Anulada", format_datetime_display(order.cancelled_at) or "—"))

    lines: list[ProductionDocLine] = []
    total = 0
    for index, line in enumerate(order.lines, start=1):
        nombre = referencia = None
        motivo = None
        if line.prepared_product_id is not None and line.prepared_product_id in prepared_names:
            nombre, referencia = prepared_names[line.prepared_product_id]
        elif line.recipe_id is not None:
            motivo = "Receta sin material preparado"
        else:
            motivo = "Sin receta"

        if line.quantity:
            total += line.quantity

        lines.append(
            ProductionDocLine(
                line_number=index,
                product_name=line.product_name_snapshot,
                product_reference=line.product_internal_reference_snapshot,
                dimensions_formatted=format_dimensions(line),
                quantity_formatted=f"{line.quantity:,}" if line.quantity is not None else "—",
                prepared_product_name=nombre,
                prepared_product_reference=referencia,
                required_formatted=format_measure(
                    line.required_material_quantity, line.required_material_uom
                ),
                missing_reason=motivo,
            )
        )

    return ProductionOrderDocument(
        company=company,
        title=PRODUCTION_ORDER_TITLE,
        code=order.code,
        status_label=_INTERNAL_STATUS_LABELS[order.status],
        status_tone=_STATUS_TONES[order.status],
        is_cancelled=order.status is ProductionOrderStatus.CANCELLED,
        facts=facts,
        lines=lines,
        total_pieces=total,
        qr_data_uri=qr_data_uri,
        qr_caption=qr_caption,
    )


def build_public_tracking_data(
    *,
    order: ProductionOrder,
    company_name: str,
) -> PublicTrackingData:
    """Reduce una orden a lo que puede ver cualquiera.

    Recibe la orden entera —con su almacen, sus gramos y su token— y devuelve
    un objeto que no tiene donde guardarlos. Esa es toda la sanitizacion: no
    hay una lista de campos que ocultar, hay una lista de campos que existen.

    Una orden ANULADA se responde igual que las demas. Ocultarla dejaria a
    quien tiene la hoja en la mano sin saber por que su pieza no avanza, y no
    esconderia nada: quien escanea ya tiene el papel.
    """
    return PublicTrackingData(
        company_name=company_name,
        order_code=order.code,
        status=order.status,
        created_at=order.created_at,
        started_at=order.started_at,
        completed_at=order.completed_at,
        cancelled_at=order.cancelled_at,
        items=[
            PublicTrackingItem(
                product_name=line.product_name_snapshot,
                quantity=line.quantity,
            )
            for line in order.lines
        ],
    )


def build_public_tracking_sheet(
    data: PublicTrackingData,
    *,
    logo_data_uri: str | None = None,
) -> PublicTrackingSheet:
    """Pone en castellano lo que ya estaba decidido que es publico."""
    creada = format_datetime_display(data.created_at)
    arrancada = format_datetime_display(data.started_at)
    completada = format_datetime_display(data.completed_at)
    anulada = format_datetime_display(data.cancelled_at)

    if data.status is ProductionOrderStatus.CANCELLED:
        # Una orden anulada no llego a producirse: ensenar «Producción
        # iniciada» en gris al lado de «Anulada» sugeriria que algo se fabrico.
        timeline = [
            PublicTrackingStep("Orden creada", creada, done=True),
            PublicTrackingStep("Orden anulada", anulada, done=True),
        ]
    else:
        timeline = [
            PublicTrackingStep("Orden creada", creada, done=creada is not None),
            PublicTrackingStep("Producción iniciada", arrancada, done=arrancada is not None),
            PublicTrackingStep("Producción completada", completada, done=completada is not None),
        ]

    return PublicTrackingSheet(
        company_name=data.company_name,
        logo_data_uri=logo_data_uri,
        title=PUBLIC_TRACKING_TITLE,
        order_code=data.order_code,
        status_label=_PUBLIC_STATUS_LABELS[data.status],
        status_tone=_STATUS_TONES[data.status],
        timeline=timeline,
        items=[
            PublicTrackingSheetItem(
                product_name=item.product_name,
                quantity_formatted=f"{item.quantity:,}" if item.quantity is not None else "—",
            )
            for item in data.items
        ],
    )
