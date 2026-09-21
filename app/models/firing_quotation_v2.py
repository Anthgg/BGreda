"""Fase 010K — Solo Quema V2: cotizar la quema de piezas que el cliente ya trae.

Hoja «Solo Quema» del Excel final. Es un servicio INDEPENDIENTE del Cotizador V2
de fabricacion y por eso vive en tablas propias:

- no lleva pasta, ni torno, ni mano de obra productiva, ni administracion, ni
  espacio;
- su factor es otro: de x1,00 a x2,00, y no el x2..x10 de fabricacion. La tabla
  `v2_quotations` exige factor >= 2 con un CHECK, y forzar ahi este servicio
  habria sido llenarla de excepciones;
- su precio se redondea sobre el TOTAL, porque el documento no lleva precio por
  pieza (hoja «PDF Quema»).

Lo que SI se comparte con el Cotizador V2, sin copiarlo: los estados (borrador,
emitida, anulada; la vencida se deriva), la vigencia, la huella comercial, las
tarifas V2 por horno, ciclo y tipo de cliente, la matematica de ocupacion y
carga de 010J, los esmaltes valorizados y los clientes.

Esta fase NO crea ordenes de produccion: la quema real de un servicio es la
planificacion de hornadas de 010L.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import (
    calculation_numeric,
    money_numeric,
    percentage_numeric,
    quantity_numeric,
    unit_cost_numeric,
)
from app.core.quoter_v2_config import DEFAULT_FIRING_SERVICE_FACTOR
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType
from app.models.quoter_v2 import (
    LIFECYCLE_COHERENT,
    V2CustomerKind,
    V2FiringMode,
    V2QuotationStatus,
)

#: Rango CERRADO del factor Solo Quema (hoja «Configuracion», A15:C15). No es
#: configuracion: es la regla del servicio. El x2..x10 de fabricacion no aplica.
FIRING_QUOTATION_FACTOR_MIN = Decimal("1.00")
FIRING_QUOTATION_FACTOR_MAX = Decimal("2.00")


class V2GlazeCostSource(StrEnum):
    """De donde sale el costo por gramo del vidriado.

    MASTER: el esmalte valorizado del maestro V2 (por defecto el activo mas
    caro, como el Excel). MANUAL: un costo tecleado para ESTA cotizacion porque
    el esmalte no esta en el maestro. El manual nunca se escribe en el maestro.
    """

    MASTER = "MASTER"
    MANUAL = "MANUAL"


class V2FiringQuotation(Base, TimestampMixin):
    """Una cotizacion de Solo Quema V2 (codigo Q-V2-AAAA-NNNNNN)."""

    __tablename__ = "v2_firing_quotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[V2QuotationStatus] = mapped_column(
        StrEnumType(V2QuotationStatus, 16),
        nullable=False,
        server_default=text("'DRAFT'"),
    )

    # ---- Cliente -----------------------------------------------------------
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("partners.id", ondelete="RESTRICT"), index=True
    )
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    #: Congelados al EMITIR, como en V2: el documento dice a quien se emitio.
    customer_document_type_snapshot: Mapped[str | None] = mapped_column(String(16))
    customer_document_number_snapshot: Mapped[str | None] = mapped_column(String(20))
    customer_address_snapshot: Mapped[str | None] = mapped_column(String(240))
    customer_email_snapshot: Mapped[str | None] = mapped_column(String(160))
    customer_phone_snapshot: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    #: Lo que el cliente lee en el documento.
    client_notes: Mapped[str | None] = mapped_column(Text)
    #: A quien se cotiza, a efectos de TARIFA (externo o alumno).
    customer_kind: Mapped[V2CustomerKind] = mapped_column(
        StrEnumType(V2CustomerKind, 16), nullable=False, server_default=text("'EXTERNAL'")
    )

    # ---- Condiciones congeladas al crear -----------------------------------
    currency_code_snapshot: Mapped[str | None] = mapped_column(String(3))
    currency_symbol_snapshot: Mapped[str | None] = mapped_column(String(8))
    #: NULL en moneda base; en moneda extranjera, el tipo de cambio manual.
    exchange_rate_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    tax_percent_snapshot: Mapped[Decimal | None] = mapped_column(percentage_numeric())
    rounding_step_snapshot: Mapped[Decimal | None] = mapped_column(percentage_numeric())
    validity_days_snapshot: Mapped[int | None] = mapped_column(Integer)
    settings_version_snapshot: Mapped[int | None] = mapped_column(Integer)

    # ---- Quema -------------------------------------------------------------
    #: El horno ELEGIDO por quien cotiza. El sistema compara y sugiere; nunca
    #: lo cambia solo.
    kiln_id: Mapped[int | None] = mapped_column(
        ForeignKey("kilns.id", ondelete="RESTRICT"), index=True
    )
    kiln_name_snapshot: Mapped[str | None] = mapped_column(String(120))
    kiln_capacity_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    firing_mode: Mapped[V2FiringMode] = mapped_column(
        StrEnumType(V2FiringMode, 16), nullable=False, server_default=text("'SHARED'")
    )
    low_fire_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    high_fire_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    piece_separation_cm: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("3")
    )
    #: Tarifas del horno elegido para el tipo de cliente, por hornada completa.
    #: En borrador se releen del maestro en cada recalculo; al emitir quedan.
    gas_cost_low_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    gas_cost_high_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    commercial_rate_low_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    commercial_rate_high_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    total_volume_cm3: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    occupancy_percent: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Hornadas FISICAS: cuantas veces se enciende.
    firing_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Hornadas que se COBRAN: ocupacion/100 en compartida, enteras en exclusiva.
    billed_load: Mapped[Decimal] = mapped_column(
        unit_cost_numeric(), nullable=False, server_default=text("0")
    )
    firing_commercial_total: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    #: Gas real. COSTO interno: nunca va al documento.
    firing_gas_total: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Vidriado opcional (hoja «Solo Quema», D6:E10 y K14) ---------------
    glaze_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: Gramos TOTALES de esmalte para el pedido, como el Excel.
    glaze_grams: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    glaze_cost_source: Mapped[V2GlazeCostSource] = mapped_column(
        StrEnumType(V2GlazeCostSource, 16), nullable=False, server_default=text("'MASTER'")
    )
    #: El esmalte del maestro. NULL con fuente MASTER = el activo mas caro.
    glaze_material_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT")
    )
    glaze_material_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    #: Costo por gramo tecleado para ESTA cotizacion (fuente MANUAL).
    glaze_manual_cost_per_gram: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    #: El costo por gramo que se uso, venga de donde venga.
    glaze_cost_per_gram_snapshot: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    glaze_material_cost: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Mano de obra de vidriado opcional ---------------------------------
    #: El Excel no la trae; se ofrece apagada. Si se enciende sigue las reglas
    #: de 010J: interno no suma costo, externo cobra horas reales.
    glaze_labor_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    glaze_labor_worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("v2_workers.id", ondelete="RESTRICT")
    )
    glaze_labor_technique_id: Mapped[int | None] = mapped_column(
        ForeignKey("v2_techniques.id", ondelete="RESTRICT")
    )
    glaze_labor_quantity: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    glaze_labor_worker_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    glaze_labor_worker_type_snapshot: Mapped[str | None] = mapped_column(String(16))
    glaze_labor_technique_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    glaze_labor_capacity_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    glaze_labor_workday_hours_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    glaze_labor_hourly_rate_snapshot: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    glaze_labor_hours: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    glaze_labor_cost: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Precio (hoja «Solo Quema», K15:K20) -------------------------------
    factor: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("1")
    )
    #: Quema comercial + vidriado + MO de vidriado. El gas NO entra.
    base_amount: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Base x factor, en moneda base, antes de redondear.
    commercial_price: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: En la moneda de la cotizacion, redondeado hacia arriba al escalon.
    subtotal_amount: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    #: Gas + vidriado + MO de vidriado. Interno.
    real_cost_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    estimated_profit: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: `quantity_numeric` y no `percentage_numeric`: aquel tope de 999,999999
    #: lo rompe una perdida grande —vender a 157 lo que cuesta 4697 es un
    #: -2892 %— y el margen reventaria la fila en vez de avisar.
    effective_margin_percent: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Ciclo de vida (igual que V2) --------------------------------------
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(200))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[date | None] = mapped_column(Date)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    issued_by_name: Mapped[str | None] = mapped_column(String(200))
    commercial_fingerprint: Mapped[str | None] = mapped_column(String(64))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    cancelled_by_name: Mapped[str | None] = mapped_column(String(200))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    duplicated_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("v2_firing_quotations.id", ondelete="RESTRICT"), index=True
    )

    lines: Mapped[list[V2FiringQuotationLine]] = relationship(
        "V2FiringQuotationLine",
        back_populates="service",
        cascade="all, delete-orphan",
        order_by="(V2FiringQuotationLine.sort_order, V2FiringQuotationLine.id)",
    )

    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')", name="status_allowed"),
        CheckConstraint(LIFECYCLE_COHERENT, name="lifecycle_coherent"),
        CheckConstraint("customer_kind IN ('EXTERNAL', 'STUDENT')", name="customer_kind_allowed"),
        CheckConstraint("firing_mode IN ('SHARED', 'EXCLUSIVE')", name="firing_mode_allowed"),
        CheckConstraint(
            "piece_separation_cm >= 0 AND piece_separation_cm <= 20",
            name="piece_separation_range",
        ),
        CheckConstraint("factor >= 1 AND factor <= 2", name="factor_range"),
        CheckConstraint(
            "kiln_capacity_snapshot IS NULL OR kiln_capacity_snapshot > 0",
            name="kiln_capacity_positive",
        ),
        CheckConstraint("total_volume_cm3 >= 0", name="volume_non_negative"),
        CheckConstraint("occupancy_percent >= 0", name="occupancy_non_negative"),
        CheckConstraint("firing_count >= 0", name="firing_count_non_negative"),
        CheckConstraint("billed_load >= 0", name="billed_load_non_negative"),
        CheckConstraint(
            "firing_commercial_total >= 0 AND firing_gas_total >= 0",
            name="firing_amounts_non_negative",
        ),
        CheckConstraint("glaze_cost_source IN ('MASTER', 'MANUAL')", name="glaze_source_allowed"),
        CheckConstraint("glaze_grams >= 0", name="glaze_grams_non_negative"),
        CheckConstraint(
            "glaze_manual_cost_per_gram IS NULL OR glaze_manual_cost_per_gram >= 0",
            name="glaze_manual_cost_non_negative",
        ),
        # Apagado es apagado: sin importes escondidos de un vidriado retirado.
        CheckConstraint(
            "glaze_enabled OR (glaze_material_cost = 0)", name="glaze_off_costs_nothing"
        ),
        CheckConstraint(
            "glaze_labor_enabled OR (glaze_labor_hours = 0 AND glaze_labor_cost = 0)",
            name="glaze_labor_off_costs_nothing",
        ),
        CheckConstraint(
            "glaze_material_cost >= 0 AND glaze_labor_hours >= 0 AND glaze_labor_cost >= 0",
            name="glaze_amounts_non_negative",
        ),
        CheckConstraint("glaze_labor_quantity >= 0", name="glaze_labor_quantity_non_negative"),
        CheckConstraint(
            "subtotal_amount >= 0 AND tax_amount >= 0 AND total_amount >= 0",
            name="document_amounts_non_negative",
        ),
        CheckConstraint(
            "exchange_rate_snapshot IS NULL OR exchange_rate_snapshot > 0",
            name="exchange_rate_positive",
        ),
        CheckConstraint(
            "duplicated_from_id IS NULL OR duplicated_from_id <> id",
            name="not_duplicated_from_itself",
        ),
        CheckConstraint(
            "expires_at IS NULL OR issued_at IS NULL OR expires_at > issued_at",
            name="expires_after_issue",
        ),
        Index("ix_v2_firing_quotations_status", "status"),
        Index("ix_v2_firing_quotations_created_at", "created_at"),
        # Un solo borrador abierto nacido de cada original: el doble clic en
        # «duplicar» no puede dejar dos.
        Index(
            "uq_v2_firing_quotations_open_duplicate",
            "duplicated_from_id",
            unique=True,
            postgresql_where=text("duplicated_from_id IS NOT NULL AND status = 'DRAFT'"),
        ),
    )


class V2FiringQuotationLine(Base, TimestampMixin):
    """Una pieza del cliente: nombre, cantidad y medidas."""

    __tablename__ = "v2_firing_quotation_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Nombre explicito: el de la convencion pasaria de 63 caracteres y
    #: PostgreSQL lo recortaria distinto del modelo.
    v2_firing_quotation_id: Mapped[int] = mapped_column(
        ForeignKey(
            "v2_firing_quotations.id",
            ondelete="CASCADE",
            name="fk_v2_firing_quotation_lines_quotation_id",
        ),
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Pieza del catalogo, opcional. Una pieza del cliente puede ir con nombre
    #: libre: no se crea un producto por cada encargo.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    product_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    length_cm: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    width_cm: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    height_cm: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    #: (L+s)(A+s)(H+s) con la separacion de la cabecera; 0 si falta una medida.
    unit_volume_cm3: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    total_volume_cm3: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    volume_share_percent: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )

    service: Mapped[V2FiringQuotation] = relationship("V2FiringQuotation", back_populates="lines")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("length_cm IS NULL OR length_cm > 0", name="length_positive"),
        CheckConstraint("width_cm IS NULL OR width_cm > 0", name="width_positive"),
        CheckConstraint("height_cm IS NULL OR height_cm > 0", name="height_positive"),
        CheckConstraint(
            "unit_volume_cm3 >= 0 AND total_volume_cm3 >= 0", name="volumes_non_negative"
        ),
        CheckConstraint(
            "volume_share_percent >= 0 AND volume_share_percent <= 100",
            name="volume_share_range",
        ),
        Index("ix_v2_firing_quotation_lines_service", "v2_firing_quotation_id", "sort_order"),
    )


__all__ = [
    "DEFAULT_FIRING_SERVICE_FACTOR",
    "FIRING_QUOTATION_FACTOR_MAX",
    "FIRING_QUOTATION_FACTOR_MIN",
    "V2FiringQuotation",
    "V2FiringQuotationLine",
    "V2GlazeCostSource",
]
