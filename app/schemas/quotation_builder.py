"""Contratos del nuevo Cotizador multiproducto de Fase 005.11."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.quotations import (
    QuotationPaymentStatus,
    QuotationStatus,
    QuotationWorkflow,
)
from app.schemas.quotations import (
    AdditionalSelectionIn,
    OtherCostSelectionIn,
    TechniqueSelectionIn,
)

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
SortOrder = Annotated[int, Field(strict=True, ge=0, le=10_000)]
Money = Annotated[Decimal, Field(ge=0, max_digits=36, decimal_places=18)]
Dimension = Annotated[Decimal, Field(gt=0, le=100_000, max_digits=18, decimal_places=6)]
Markup = Annotated[Decimal, Field(ge=0, le=100_000, max_digits=36, decimal_places=18)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProductDimensionCompletionIn(_Strict):
    """Dimensiones efectivas para ESTA linea de cotizacion.

    Fase 009B: un valor aqui SIEMPRE gana sobre el maestro del producto para
    calcular y persistir la dimension efectiva de la linea — nunca se
    escribe de vuelta en products.* (el maestro es de solo lectura desde el
    Cotizador). Antes de esta fase, este contrato solo aceptaba completar
    campos ausentes en el maestro (y el guardado SI lo escribia en el
    maestro compartido); ver dimensions_overridden en QuotationBuilderItemIn
    para la bandera que distingue "usa el estandar" de "personalizado para
    esta cotizacion".
    """

    width: Dimension | None = None
    height: Dimension | None = None
    length: Dimension | None = None
    depth: Dimension | None = None


class BodyMaterialIn(_Strict):
    """Material que forma el CUERPO de la pieza, y cuanto lleva una pieza.

    Es el dato fisico principal del producto y sustituye a «receta + gramos»
    como intencion del usuario. La receta sigue existiendo, pero como
    procedencia del preparado, no como algo que haya que elegir para cotizar.

    **No lleva unidad.** La unidad la pone el maestro del material
    (`products.base_uom_code`) y la cantidad se expresa siempre en ella. Que el
    navegador pudiera mandarla abriria la puerta a cotizar en mililitros un
    material que el almacen lleva en gramos, y la cifra guardada dejaria de
    significar nada.
    """

    product_id: PositiveInt
    quantity_per_piece: Annotated[
        Decimal, Field(gt=0, le=1_000_000, max_digits=18, decimal_places=6)
    ]


class BodyMaterialOut(BaseModel):
    """El material base ya resuelto contra el maestro, con su costo.

    Todo salvo `product_id` y `quantity_per_piece` es derivado del backend.
    """

    model_config = ConfigDict(from_attributes=True)

    product_id: int
    product_internal_reference: str | None = None
    product_name: str | None = None
    product_type: str | None = None
    quantity_per_piece: Decimal
    #: Unidad base del material. Canonica: sale del maestro, no del navegador.
    uom: str | None = None
    #: `RAW` (materia prima directa) o `PREPARED` (material fabricado por una
    #: receta). Decide si hay procedencia que mostrar, no como se costea.
    source: str | None = None
    #: Procedencia del preparado. Solo lectura en el Cotizador.
    recipe_id_used: int | None = None
    recipe_version_id_used: int | None = None
    recipe_name_snapshot: str | None = None
    unit_cost_snapshot: Decimal | None = None
    required_quantity: Decimal | None = None
    material_cost: Decimal | None = None


class BodyMaterialOptionOut(BaseModel):
    """Un material elegible como cuerpo de pieza, tal como lo ve el selector."""

    model_config = ConfigDict(from_attributes=True)

    product_id: int
    internal_reference: str
    name: str
    product_type: str
    source: str
    uom: str | None = None
    #: Procedencia legible, para mostrarla como metadato de solo lectura.
    recipe_name: str | None = None
    #: El backend ya sabe costear este material. Falso no lo esconde de la
    #: lista —el usuario tiene que poder ver que existe— pero avisa antes de
    #: que lo elija y se lo bloquee la confirmacion.
    costable: bool = True


class BodyMaterialOptionPage(BaseModel):
    items: list[BodyMaterialOptionOut] = Field(default_factory=list)
    total: int = 0


class CommercialLineIn(_Strict):
    """Un cargo comercial de la cotizacion, tal como lo teclea una persona.

    El importe es NETO y ya en la moneda de emision, igual que el precio manual
    de una linea de producto: quien escribe 200 cotizando en dolares quiere
    cobrar doscientos dolares. El impuesto, el redondeo y los totales los pone
    el backend.
    """

    kind: Literal["PROTOTYPE"] = "PROTOTYPE"
    description: Annotated[str, Field(min_length=1, max_length=200)]
    prototype_id: PositiveInt | None = None
    quantity: PositiveInt = 1
    manual_net_amount: Annotated[
        Decimal, Field(gt=0, le=100_000_000, max_digits=36, decimal_places=18)
    ]
    sort_order: SortOrder = 0

    @model_validator(mode="after")
    def prototype_kind_needs_prototype(self) -> CommercialLineIn:
        """Un cargo de prototipo sin prototipo no se puede describir ni auditar."""
        if self.kind == "PROTOTYPE" and self.prototype_id is None:
            raise ValueError("Un cargo de prototipo debe indicar de que muestra es")
        return self


class CommercialLineOut(BaseModel):
    """El cargo ya valorado. Todo lo monetario derivado lo pone el backend."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    description: str
    prototype_id: int | None = None
    quantity: int
    manual_net_amount: Decimal
    sort_order: int
    line_total_net: Decimal
    line_total_tax: Decimal
    line_total_gross: Decimal


class GlazeSelectionItemIn(_Strict):
    """Intencion del usuario sobre un esmalte de la linea. Nada derivado.

    El navegador manda QUE esmalte y CON CUANTO PESO RELATIVO. No manda gramos,
    ni mililitros, ni concentracion, ni costo: todo eso lo calcula el backend a
    partir de la configuracion vigente y del lote. Si el cliente pudiera
    mandarlos, dos cotizaciones podrian discrepar sin que nadie lo decidiera.
    """

    preparation_id: PositiveInt | None = None
    prepared_product_id: PositiveInt | None = None
    #: Peso relativo, NO porcentaje. 1 y 1 son mitad y mitad; 2 y 1 son dos
    #: tercios y un tercio. El backend resuelve el porcentaje.
    share: Annotated[Decimal, Field(gt=0, le=1_000_000, max_digits=18, decimal_places=6)] = Decimal(
        1
    )

    @model_validator(mode="after")
    def at_least_one_reference(self) -> GlazeSelectionItemIn:
        if self.preparation_id is None and self.prepared_product_id is None:
            raise ValueError("Indique el preparado o el lote de cada esmalte")
        return self


class GlazeAllocationOut(BaseModel):
    """Una asignacion ya resuelta por el backend. Todo salvo `share` es derivado."""

    model_config = ConfigDict(from_attributes=True)

    prepared_product_id: int
    prepared_product_internal_reference: str | None = None
    prepared_product_name: str | None = None
    preparation_id: int | None = None
    preparation_code: str | None = None
    share: Decimal
    allocation_percent: Decimal
    grams: Decimal
    millilitres: Decimal | None = None
    solids_g_per_ml_snapshot: Decimal | None = None
    unit_cost_per_ml_snapshot: Decimal | None = None
    estimated_cost: Decimal | None = None


class GlazePlanOut(BaseModel):
    """El plan tecnico de esmaltes de una linea, tal como quedo guardado.

    En un borrador se recalcula con la configuracion vigente; en una cotizacion
    confirmada es historia y no vuelve a tocarse.
    """

    model_config = ConfigDict(from_attributes=True)

    unit: str = "g"
    #: El plan lo propuso el backend porque el usuario aun no habia elegido.
    #: Es una SUGERENCIA: en cuanto el usuario toca la seleccion, deja de
    #: proponerse y manda lo que el eligio.
    default_applied: bool = False
    estimated_glaze_percent_snapshot: Decimal
    piece_weight_g_snapshot: Decimal
    grams_per_piece: Decimal
    total_estimated_solids_g: Decimal
    allocations: list[GlazeAllocationOut] = Field(default_factory=list)
    total_estimated_cost: Decimal | None = None


class QuotationBuilderItemIn(_Strict):
    id: PositiveInt | None = None
    product_id: PositiveInt
    quantity: PositiveInt | None = None
    dimensions: ProductDimensionCompletionIn = Field(default_factory=ProductDimensionCompletionIn)
    #: Intencion explicita del usuario (paso Piezas: "Usar medidas estandar"
    #: vs "Personalizar medidas"), independiente de si `dimensions` coincide
    #: numericamente con el maestro. Persistida en production_snapshot (no
    #: amerita columna propia); impulsa el badge "Medidas personalizadas" y
    #: sobrevive a que el maestro cambie despues (nunca se re-infiere
    #: comparando contra el maestro en vivo).
    dimensions_overridden: bool = False
    #: Material que forma el cuerpo de la pieza. Cuando viene, es la autoridad
    #: del material y del costo, y `recipe_id`/`material_grams_per_piece`
    #: pasan a ser compatibilidad: se siguen escribiendo cuando el material lo
    #: permite, pero ya no son lo que el usuario eligio.
    body_material: BodyMaterialIn | None = None
    #: Camino legacy. Sigue vivo para las cotizaciones que ya existen y para
    #: los borradores que aun no han pasado por el selector de material.
    recipe_id: PositiveInt | None = None
    recipe_version_id: PositiveInt | None = None
    firing_line_id: PositiveInt | None = None
    materials_applied: Money | None = None
    material_grams_per_piece: Annotated[
        Decimal | None, Field(gt=0, le=1_000_000, max_digits=18, decimal_places=6)
    ] = None
    low_kiln_id: PositiveInt | None = None
    high_kiln_id: PositiveInt | None = None
    #: Fase 009C: intencion explicita de quemar (baja / alta). Son
    #: INDEPENDIENTES: una pieza puede necesitar solo baja, solo alta o ambas.
    #: Existen aparte de `*_kiln_id` para poder decir "si quiero quema baja,
    #: con el horno de cabecera de la cotizacion" sin repetir el id en cada
    #: linea. Por defecto True para no romper payloads anteriores a 009C, que
    #: siempre asumian ambas quemas.
    low_kiln_selected: bool = True
    high_kiln_selected: bool = True
    factor_kiln_id: PositiveInt | None = None
    techniques: list[TechniqueSelectionIn] = Field(default_factory=list, max_length=100)
    additionals: list[AdditionalSelectionIn] = Field(default_factory=list, max_length=100)
    #: Fase 009D. Esmaltes de esta pieza y su reparto. Estimar no consume
    #: inventario: el descuento real al vender pertenece a 009H.
    glazes: list[GlazeSelectionItemIn] = Field(default_factory=list, max_length=20)
    #: Unidad en la que el usuario quiere leer el plan de esmaltes.
    glaze_unit: Literal["g", "ml"] = "g"
    #: El usuario ya toco la seleccion de esmaltes de esta linea.
    #:
    #: Distingue "todavia no ha elegido" de "eligio no llevar ninguno", que
    #: en `glazes` se ven igual (lista vacia). Sin esta bandera, quitar el
    #: esmalte sugerido lo haria reaparecer en el siguiente recalculo y el
    #: usuario no podria quitarlo nunca.
    glaze_selection_touched: bool = False
    days_adjustment: Annotated[int, Field(strict=True, ge=-10_000, le=10_000)] = 0
    waiting_days: Annotated[int, Field(strict=True, ge=0, le=10_000)] = 0
    other_costs: list[OtherCostSelectionIn] | None = Field(default=None, max_length=100)
    markup_percent: Markup = Decimal(100)
    commercial_sale_unit_price: Money | None = None
    sort_order: SortOrder = 0

    @model_validator(mode="after")
    def recipe_pair(self) -> QuotationBuilderItemIn:
        if self.recipe_version_id is not None and self.recipe_id is None:
            raise ValueError("recipe_id es obligatorio cuando se indica recipe_version_id")
        return self


class QuotationBuilderDraftIn(_Strict):
    name: Annotated[str | None, Field(max_length=200)] = None
    customer_id: PositiveInt | None = None
    kiln_id: PositiveInt | None = None
    #: Fase 009E. Factor de PRODUCCION de esta cotizacion. Multiplica el costo
    #: tecnico y no tiene nada que ver con el margen. `None` usa el canonico.
    production_factor: Annotated[
        Decimal | None, Field(gt=0, le=1_000, max_digits=18, decimal_places=6)
    ] = None
    #: Fase 009F. Moneda en la que se EMITE la cotizacion. `None` toma la de
    #: Configuracion, que es lo que hacia el sistema entero antes de 009F.
    #: Los costos siguen en PEN: esto solo decide el precio que se factura.
    currency_code: Literal["PEN", "USD"] | None = None
    #: Cuantos soles vale un dolar (`1 USD = X PEN`). Obligatorio para USD y
    #: prohibido para PEN, porque en soles no hay conversion que declarar.
    exchange_rate: Annotated[
        Decimal | None, Field(gt=0, le=1_000_000, max_digits=18, decimal_places=6)
    ] = None
    items: list[QuotationBuilderItemIn] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_products(self) -> QuotationBuilderDraftIn:
        product_ids = [item.product_id for item in self.items]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("Un producto no puede repetirse en la misma cotizacion")
        return self

    @model_validator(mode="after")
    def coherent_currency(self) -> QuotationBuilderDraftIn:
        """Los dos unicos estados con sentido, tambien en la puerta de entrada.

        El CHECK de la base ya lo impide, pero un 422 explica que falta; un
        error de integridad llega como 500 y no dice nada util.
        """
        if self.currency_code == "USD" and self.exchange_rate is None:
            raise ValueError("EXCHANGE_RATE_REQUIRED")
        if self.currency_code != "USD" and self.exchange_rate is not None:
            raise ValueError("Una cotizacion en PEN no lleva tipo de cambio")
        return self


class QuotationBuilderCreateIn(QuotationBuilderDraftIn):
    pass


class QuotationBuilderUpdateIn(QuotationBuilderDraftIn):
    expected_updated_at: datetime


class QuotationBuilderConfirmIn(_Strict):
    expected_updated_at: datetime


class QuotationBuilderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    product_id: int
    product_internal_reference: str
    product_name: str
    product_type: str
    product_uom: str | None = None
    product_material: str | None = None
    product_grammage: Decimal | None = None
    #: Medidas EFECTIVAS de esta linea (personalizadas o heredadas del maestro).
    width: Decimal | None = None
    height: Decimal | None = None
    length: Decimal | None = None
    depth: Decimal | None = None
    #: Medidas ESTANDAR del maestro vigente, para que la UI pueda mostrar el
    #: contraste y restaurarlas al volver a "usar medidas estandar" sin tener
    #: que consultar el producto por separado (Fase 009B).
    standard_width: Decimal | None = None
    standard_height: Decimal | None = None
    standard_length: Decimal | None = None
    standard_depth: Decimal | None = None
    editable_dimensions: list[str] = Field(default_factory=list)
    dimensions_overridden: bool = False
    quantity: int | None = None
    #: El frontend consume ESTE campo tipado, nunca el JSON crudo de
    #: production_snapshot. `None` en las lineas legacy, que no lo tienen.
    body_material: BodyMaterialOut | None = None
    recipe_id: int | None = None
    recipe_version_id: int | None = None
    recipe_version_fingerprint_snapshot: str | None = None
    recipe_auto_selected: bool = False
    materials_applied_input: Decimal | None = None
    material_grams_per_piece: Decimal | None = None
    firing_id: int | None = None
    firing_line_id: int | None = None
    firing_code_snapshot: str | None = None
    kiln_id: int | None = None
    low_kiln_id: int | None = None
    high_kiln_id: int | None = None
    #: Fase 009C: la INTENCION de quemar, independiente de con que horno. Una
    #: quema que hereda el horno de cabecera no trae *_kiln_id propio, asi que
    #: la UI no puede deducirla del id sin apagarla por error.
    low_kiln_selected: bool = True
    high_kiln_selected: bool = True
    factor_kiln_id: int | None = None
    production_snapshot: dict[str, Any] = Field(default_factory=dict)
    #: Fase 009D. El frontend consume ESTE campo tipado, nunca el JSON crudo
    #: de production_snapshot.
    glaze_plan: GlazePlanOut | None = None
    glaze_unit: str = "g"
    glaze_selection_touched: bool = False
    techniques: list[dict[str, Any]] = Field(default_factory=list)
    additionals: list[dict[str, Any]] = Field(default_factory=list)
    other_costs: list[dict[str, Any]] = Field(default_factory=list)
    materials_calculated: Decimal = Decimal(0)
    materials_applied: Decimal = Decimal(0)
    firing_cost: Decimal = Decimal(0)
    labor_cost: Decimal = Decimal(0)
    calculated_days: int = 0
    days_adjustment: int = 0
    waiting_days: int = 0
    total_days: int = 0
    space_cost: Decimal = Decimal(0)
    # ---- Fase 009E: motor comercial, paso a paso -----------------------
    #: Materiales + quema asignada + mano de obra. SIN costos fijos: esos son
    #: de la cotizacion entera y se reparten aparte.
    technical_cost: Decimal = Decimal(0)
    production_factor: Decimal = Decimal(0)
    factored_cost: Decimal = Decimal(0)
    fixed_cost_allocation: Decimal = Decimal(0)
    commercial_base_cost: Decimal = Decimal(0)
    commercial_base_unit_cost: Decimal = Decimal(0)
    #: Fase 009F. Moneda y tasa efectivas de esta linea. Son las de la
    #: cotizacion: un documento con dos tasas daria un total inexplicable.
    currency_code_snapshot: str = "PEN"
    exchange_rate_snapshot: Decimal | None = None
    #: Neto unitario ANTES de convertir, siempre en PEN. Permite explicar de
    #: donde sale el precio en dolares sin rehacer la cuenta.
    raw_net_unit_base: Decimal = Decimal(0)
    raw_net_unit: Decimal = Decimal(0)
    raw_tax_unit: Decimal = Decimal(0)
    raw_gross_unit: Decimal = Decimal(0)
    rounding_step: Decimal = Decimal(0)
    #: Lo que el CEILING contractual anadio al bruto crudo. Siempre >= 0.
    rounding_adjustment_unit: Decimal = Decimal(0)
    final_gross_unit: Decimal = Decimal(0)
    final_net_unit: Decimal = Decimal(0)
    final_tax_unit: Decimal = Decimal(0)
    line_total_gross: Decimal = Decimal(0)
    line_total_net: Decimal = Decimal(0)
    line_total_tax: Decimal = Decimal(0)
    final_unit_cost: Decimal = Decimal(0)
    final_total_cost: Decimal = Decimal(0)
    markup_percent: Decimal = Decimal(100)
    calculated_sale_unit_price: Decimal = Decimal(0)
    suggested_commercial_unit_price: Decimal = Decimal(0)
    commercial_sale_unit_price_input: Decimal | None = None
    commercial_sale_unit_price: Decimal = Decimal(0)
    effective_profit_unit: Decimal = Decimal(0)
    effective_profit_total: Decimal = Decimal(0)
    effective_markup_percent: Decimal = Decimal(0)
    commercial_subtotal: Decimal = Decimal(0)
    commercial_unit_price_with_tax: Decimal = Decimal(0)
    commercial_total: Decimal = Decimal(0)
    tax_percentage_snapshot: Decimal = Decimal(0)
    tax_rate_source_snapshot: str = "COMMERCIAL_SETTINGS"
    tax_amount: Decimal = Decimal(0)
    source_fingerprint: str
    warnings: list[str] = Field(default_factory=list)
    complete: bool = False
    sort_order: int = 0


class QuotationBuilderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    code: str | None = None
    workflow: QuotationWorkflow = QuotationWorkflow.COTIZADOR
    status: QuotationStatus = QuotationStatus.DRAFT
    #: Fase 009K.1. De que muestra nacio esta cotizacion, si nacio de alguna.
    #: Nulo en todo lo anterior y en lo que se cotiza sin muestra previa. El
    #: codigo viaja al lado porque la pantalla escribe «PRT-2026-000007», no un
    #: numero de fila, y sin el tendria que pedir la muestra entera solo para
    #: pintar una etiqueta.
    origin_prototype_id: int | None = None
    origin_prototype_code: str | None = None
    name: str | None = None
    customer_id: int | None = None
    customer_name_snapshot: str | None = None
    kiln_id: int | None = None
    kiln_snapshot: dict[str, Any] = Field(default_factory=dict)
    production_summary: dict[str, Any] = Field(default_factory=dict)
    items: list[QuotationBuilderItemOut] = Field(default_factory=list)
    #: Fase 009K.1. Cargos comerciales: conceptos que se cobran y no se
    #: fabrican. Entran en el subtotal, el IGV y el total, y no llevan factor
    #: ni margen de producto.
    commercial_lines: list[CommercialLineOut] = Field(default_factory=list)
    item_count: int = 0
    commercial_subtotal: Decimal = Decimal(0)
    tax_percentage_snapshot: Decimal = Decimal(0)
    tax_rate_source_snapshot: str = "COMMERCIAL_SETTINGS"
    tax_amount: Decimal = Decimal(0)
    total_with_tax: Decimal = Decimal(0)
    # ---- Fase 009E: los tres totales, sin ambiguedad -------------------
    #: Suma de `line_total_net`. Es EL neto de la cotizacion.
    quotation_net_total: Decimal = Decimal(0)
    #: Suma de `line_total_tax`.
    quotation_tax_total: Decimal = Decimal(0)
    #: Suma de `line_total_gross`. Es EL total: el que se firma. No se
    #: vuelve a redondear — el cliente suma las lineas del documento.
    quotation_gross_total: Decimal = Decimal(0)
    production_factor: Decimal = Decimal(0)
    rounding_step: Decimal = Decimal(0)
    total_fixed_cost: Decimal = Decimal(0)
    #: Fase 009F. `currency_code_snapshot` es la autoridad semantica; el
    #: simbolo es presentacion. Quien lea esto no debe deducir la moneda del
    #: simbolo: un `$` suelto se lee como sol tan a menudo como como dolar.
    currency_code_snapshot: str = "PEN"
    currency_symbol_snapshot: str = "S/"
    #: Cuantos soles vale un dolar. Nulo cuando se emite en PEN.
    exchange_rate_snapshot: Decimal | None = None
    exchange_rate_source_snapshot: str | None = None
    warnings: list[str] = Field(default_factory=list)
    complete: bool = False
    next_step: str = "GENERAL_DATA"
    source_fingerprint: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    #: Fase 009H. Nulo significa que el pago no lo registro el sistema —todo lo
    #: anterior a 009H—, no que este impaga. La interfaz debe distinguirlo.
    payment_status: QuotationPaymentStatus | None = None
    paid_at: datetime | None = None
