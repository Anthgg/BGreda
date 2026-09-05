"""La cotizacion de un prototipo: el documento comercial, no la muestra fisica.

Tabla propia y no una fila mas en `quotations`, y no por gusto de separar. Una
cotizacion de producto obliga a un `commercial_factor > 0` por restriccion de
esa tabla, y un prototipo no lleva factor: meterlo ahi con un 1 significaria
«factor 1 aplicado», que no es lo mismo que «sin factor». Ademas habria que
rellenar con ceros una quincena de columnas del costeo de produccion cuyo
nombre coincide con el del prototipo pero cuyo significado no —el `firing_cost`
de una pieza en serie no es el de una muestra—. Compartir tabla saldria mas
caro que compartir servicios.

Lo que SI se comparte es todo lo demas: `Partner`, `SequenceService`,
`CommercialSettings`, `Kiln`/`KilnRate`, `Product.cost`, la auditoria, el
renderer de documentos y las dos politicas de dinero de `app/core/pricing.py`.

Dos ejes independientes, igual que en las cotizaciones normales: `status` dice
en que punto comercial esta el documento y `payment_status` si se cobro. Una
anulada puede estar pagada, y mezclarlos obligaria a perder uno de los dos.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import (
    calculation_numeric,
    percentage_numeric,
    stock_quantity_numeric,
    unit_cost_numeric,
)
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType
from app.models.firings import FiringType

if TYPE_CHECKING:
    from app.models.firings import Kiln
    from app.models.masters import Partner, Product


class PrototypeQuotationStatus(StrEnum):
    """Estado comercial del documento. El cobro es otro eje."""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class PrototypeQuotationPaymentStatus(StrEnum):
    UNPAID = "UNPAID"
    PAID = "PAID"


class PrototypeQuotation(Base, TimestampMixin):
    """Cotizacion de prototipo: lo que cuesta y cuanto tarda hacer una muestra."""

    __tablename__ = "prototype_quotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: CPR-2026-000001. Lo emite el backend al CONFIRMAR: un borrador que nunca
    #: llega a emitirse no debe gastar un numero del talonario.
    code: Mapped[str | None] = mapped_column(String(32), unique=True)

    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("partners.id", ondelete="RESTRICT"), index=True
    )
    #: El nombre con el que se emitio. El maestro puede cambiar de razon social
    #: manana y el documento firmado no.
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[PrototypeQuotationStatus] = mapped_column(
        StrEnumType(PrototypeQuotationStatus, 16),
        nullable=False,
        server_default=text("'DRAFT'"),
    )
    payment_status: Mapped[PrototypeQuotationPaymentStatus] = mapped_column(
        StrEnumType(PrototypeQuotationPaymentStatus, 16),
        nullable=False,
        server_default=text("'UNPAID'"),
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- Que se cotiza ------------------------------------------------------
    #: Nulo cuando se cotiza un concepto que todavia no esta en el catalogo.
    #: Cotizar una idea no puede obligar a crear un producto maestro.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    #: Medidas de ESTA muestra. Editarlas no toca el maestro del producto.
    width_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    length_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    height_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    depth_cm: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    technical_specifications: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(Text)

    # -- Entradas del costeo, editables mientras es borrador -----------------
    design_days: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, server_default=text("0")
    )
    #: Nulo significa «usa la tarifa de configuracion». Guardar aqui una copia
    #: del valor por defecto impediria distinguir un precio pactado de uno
    #: heredado, y al cambiar la configuracion el borrador dejaria de seguirla.
    design_rate_override: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    artist_days: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, server_default=text("0")
    )
    artist_rate_override: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())

    mold_maker_partner_id: Mapped[int | None] = mapped_column(
        ForeignKey("partners.id", ondelete="RESTRICT")
    )
    #: Precio FIJO. Sus dias alargan el plazo y no multiplican este importe.
    mold_maker_price_override: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    mold_maker_days: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, server_default=text("0")
    )

    kiln_id: Mapped[int | None] = mapped_column(ForeignKey("kilns.id", ondelete="RESTRICT"))
    #: El MISMO vocabulario que las quemas —LOW/HIGH—, y no uno propio. Un
    #: segundo juego de nombres para lo mismo obliga a traducir en cada
    #: frontera, y basta con que alguien olvide una traduccion para que la
    #: tarifa no se encuentre. «Baja» y «Alta» son etiquetas de pantalla.
    #:
    #: Sin valor por defecto: elegir una en silencio cotizaria a una tarifa que
    #: nadie escogio.
    firing_type: Mapped[FiringType | None] = mapped_column(StrEnumType(FiringType, 8))
    firing_batches: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    drying_days: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, server_default=text("0")
    )
    adjustment_days: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, server_default=text("0")
    )
    fixed_cost_override: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())

    # -- Politica monetaria congelada al confirmar ---------------------------
    currency_code_snapshot: Mapped[str | None] = mapped_column(String(3))
    currency_symbol_snapshot: Mapped[str | None] = mapped_column(String(8))
    exchange_rate_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    tax_percent_snapshot: Mapped[Decimal | None] = mapped_column(percentage_numeric())
    #: El paso comercial con el que se redondeo. Sin el, regenerar el PDF de un
    #: documento historico usaria la politica de hoy y daria otro total.
    rounding_step_snapshot: Mapped[Decimal | None] = mapped_column(calculation_numeric())
    rounding_source_snapshot: Mapped[str | None] = mapped_column(String(32))

    # -- Resultado congelado -------------------------------------------------
    #: El desglose completo tal como se calculo. Es interno: el documento del
    #: cliente lleva el precio, no la tarifa del artista.
    cost_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    commercial_net_total: Mapped[Decimal | None] = mapped_column(calculation_numeric())
    commercial_tax_total: Mapped[Decimal | None] = mapped_column(calculation_numeric())
    commercial_gross_total: Mapped[Decimal | None] = mapped_column(calculation_numeric())
    estimated_days: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    target_date: Mapped[date | None] = mapped_column(Date)

    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_by_name: Mapped[str | None] = mapped_column(String(200))

    lines: Mapped[list[PrototypeQuotationMaterial]] = relationship(
        "PrototypeQuotationMaterial",
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by="PrototypeQuotationMaterial.sort_order",
        lazy="selectin",
    )
    customer: Mapped[Partner | None] = relationship(
        "Partner", foreign_keys=[customer_id], lazy="selectin"
    )
    mold_maker: Mapped[Partner | None] = relationship(
        "Partner", foreign_keys=[mold_maker_partner_id], lazy="selectin"
    )
    kiln: Mapped[Kiln | None] = relationship("Kiln", lazy="selectin")

    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')", name="pq_status_allowed"),
        CheckConstraint("payment_status IN ('UNPAID', 'PAID')", name="pq_payment_status_allowed"),
        CheckConstraint(
            "firing_type IS NULL OR firing_type IN ('LOW', 'HIGH')",
            name="pq_firing_type_allowed",
        ),
        CheckConstraint("quantity > 0", name="pq_quantity_positive"),
        CheckConstraint("design_days >= 0", name="pq_design_days_non_negative"),
        CheckConstraint("artist_days >= 0", name="pq_artist_days_non_negative"),
        CheckConstraint("mold_maker_days >= 0", name="pq_mold_maker_days_non_negative"),
        CheckConstraint("drying_days >= 0", name="pq_drying_days_non_negative"),
        CheckConstraint("adjustment_days >= 0", name="pq_adjustment_days_non_negative"),
        CheckConstraint("firing_batches >= 0", name="pq_firing_batches_non_negative"),
        # Un documento emitido sin numero no se puede referenciar; uno anulado
        # antes de emitirse nunca llego a tenerlo.
        CheckConstraint("status <> 'CONFIRMED' OR code IS NOT NULL", name="pq_confirmed_has_code"),
        Index("ix_prototype_quotations_status", "status"),
    )


class PrototypeQuotationMaterial(Base, TimestampMixin):
    """Un material previsto para la muestra, con su costo congelado al emitir.

    Linea relacional y no un JSON opaco: se edita una por una mientras el
    documento es borrador, y despues hay que poder explicar de donde salio cada
    importe. Un blob no se consulta ni se audita por linea.
    """

    __tablename__ = "prototype_quotation_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prototype_quotation_id: Mapped[int] = mapped_column(
        ForeignKey("prototype_quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: Por UNA muestra. El total se obtiene multiplicando por la cantidad, y se
    #: guarda asi porque es lo que la persona teclea.
    quantity_per_prototype: Mapped[Decimal] = mapped_column(
        stock_quantity_numeric(), nullable=False
    )
    #: La del catalogo. No se convierte a otra: sin densidad, pasar gramos a
    #: mililitros es inventarse el dato.
    uom_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Nulo mientras es borrador —se lee del maestro en cada preview— y escrito
    #: al confirmar, para que el documento historico no dependa de un costo que
    #: pudo cambiar despues.
    unit_cost_snapshot: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    product_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    is_body_material: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    quotation: Mapped[PrototypeQuotation] = relationship(
        "PrototypeQuotation", back_populates="lines"
    )
    product: Mapped[Product] = relationship("Product", lazy="selectin")

    __table_args__ = (
        CheckConstraint("quantity_per_prototype > 0", name="pqm_quantity_positive"),
        CheckConstraint(
            "unit_cost_snapshot IS NULL OR unit_cost_snapshot >= 0",
            name="pqm_unit_cost_non_negative",
        ),
    )
