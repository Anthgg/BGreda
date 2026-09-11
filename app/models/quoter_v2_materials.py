"""Valorizacion de materiales para el Cotizador V2.

Fase 010C. El MAESTRO del material sigue siendo `products`, y su existencia
sigue siendo `stock_balances`: no hay aqui un segundo catalogo ni un segundo
inventario. Lo que esta tabla anade es lo que V2 necesita y el maestro no
guarda: **como se llego al costo por gramo**.

## Por que una tabla propia y no columnas en `products`

Porque `products.cost` ya significa algo y alguien lo usa. El costeo del
Cotizador historico lee ese campo directamente —`app/services/body_material.py`
lo toma como costo unitario del cuerpo de la pieza—, asi que recalcularlo a
partir de compra mas transporte cambiaria el precio de las cotizaciones Legacy
sin que nadie lo pidiera. Es el mismo motivo por el que las tarifas de horno de
010B viven aparte: lo que V2 necesita saber no cabe en un campo que otro motor
ya esta leyendo con otro significado.

## Que se guarda y que se deriva

Se guardan los HECHOS de la adquisicion —cuanto se compro, por cuanto, cuanto
costo traerlo— y, si el taller lo decide, un valor de costeo propio. El costo
por unidad NO se guarda como un numero suelto: lo calcula la base de datos a
partir de esos hechos, de modo que no puede quedar desfasado ni aunque alguien
edite la fila por SQL.

## Lo que costo y lo que vale

Son dos cosas distintas y por eso hay dos campos. Una pasta donada costo cero;
valorizarla a cero significaria regalar tambien el margen de la pieza que se
haga con ella. El taller puede fijar un valor de costeo sin mentir sobre lo que
pago.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.masters import Product

from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import money_numeric, quantity_numeric, unit_cost_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType


class V2MaterialKind(StrEnum):
    """Para que sirve el material dentro de una cotizacion V2.

    Explicito y persistido, no deducido del nombre ni de la categoria: un
    esmalte no deja de serlo porque alguien lo llame «engobe», y la seleccion
    automatica del esmalte mas caro necesita saber exactamente entre que
    materiales esta eligiendo.
    """

    #: Forma el cuerpo de la pieza: pastas y arcillas.
    BODY = "BODY"
    #: Recubre la pieza. Solo entre estos se busca «el mas caro por gramo».
    GLAZE = "GLAZE"


class V2MaterialOrigin(StrEnum):
    """Como entro el material al taller.

    Importa porque explica un costo de adquisicion de cero sin que parezca un
    error: un material donado costo cero de verdad.
    """

    PURCHASE = "PURCHASE"
    MANUAL = "MANUAL"
    DONATION = "DONATION"
    OTHER = "OTHER"


class V2MaterialCost(Base, TimestampMixin):
    """Como se valoriza un material del maestro para cotizar en V2."""

    __tablename__ = "v2_material_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: Uno por producto. El maestro es `products`; esto es su valorizacion.
    #: RESTRICT y no CASCADE: borrar un producto con cotizaciones detras no
    #: puede llevarse por delante la explicacion de su precio.
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )

    material_kind: Mapped[V2MaterialKind] = mapped_column(
        StrEnumType(V2MaterialKind, 16), nullable=False
    )
    origin: Mapped[V2MaterialOrigin] = mapped_column(
        StrEnumType(V2MaterialOrigin, 16), nullable=False
    )

    # ---- Los hechos de la adquisicion -----------------------------------
    #: Cuanto se adquirio, SIEMPRE en la unidad base del producto. Que la
    #: unidad salga del maestro y no de un campo propio es lo que evita que un
    #: material que se lleva en gramos acabe valorizado en kilos.
    purchase_quantity: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    purchase_cost: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)
    #: Lo que costo traerlo. Forma parte del material: sin transporte, el
    #: material no esta en el taller. Separado de la compra porque son dos
    #: hechos y el negocio los anota por separado, pero suman para el costo.
    transport_cost: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)

    #: El valor con el que se COSTEA, cuando no es el que se pago. Nulo
    #: significa «no hay decision»: se usa el derivado. Cero significa «la
    #: decision es no valorizarlo», que es distinto.
    costing_override_per_unit: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())

    #: Mililitros por gramo de ESTE material. Nulo no es 1: significa que no
    #: hay conversion registrada, y quien la necesite tendra que declarar que
    #: esta usando la de reserva.
    ml_per_gram: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())

    notes: Mapped[str | None] = mapped_column(Text)

    # ---- Lo que se deriva -----------------------------------------------
    #: Costo por unidad base con el que se cotiza.
    #:
    #: Columna GENERADA: la calcula PostgreSQL a partir de los hechos de
    #: arriba, no el servicio. Un numero guardado a mano puede quedarse
    #: desfasado en cuanto alguien edite la compra por otra via; este no
    #: puede, porque no existe separado de sus operandos.
    #:
    #: `NULLIF` protege de la division por cero aunque el CHECK ya la impida:
    #: una columna generada que puede fallar bloquea la fila entera.
    effective_cost_per_unit: Mapped[Decimal] = mapped_column(
        unit_cost_numeric(),
        Computed(
            "COALESCE(costing_override_per_unit,"
            " (purchase_cost + transport_cost) / NULLIF(purchase_quantity, 0))",
            persisted=True,
        ),
    )

    product: Mapped[Product] = relationship("Product", lazy="joined")

    __table_args__ = (
        UniqueConstraint("product_id", name="uq_v2_material_costs_product_id"),
        CheckConstraint("purchase_quantity > 0", name="purchase_quantity_positive"),
        CheckConstraint("purchase_cost >= 0", name="purchase_cost_non_negative"),
        CheckConstraint("transport_cost >= 0", name="transport_cost_non_negative"),
        CheckConstraint(
            "costing_override_per_unit IS NULL OR costing_override_per_unit >= 0",
            name="costing_override_non_negative",
        ),
        # Cero o negativa no es una conversion pobre: es imposible, y daria un
        # volumen con aspecto de medida real.
        CheckConstraint(
            "ml_per_gram IS NULL OR ml_per_gram > 0",
            name="ml_per_gram_positive",
        ),
        CheckConstraint("material_kind IN ('BODY', 'GLAZE')", name="material_kind_allowed"),
        CheckConstraint(
            "origin IN ('PURCHASE', 'MANUAL', 'DONATION', 'OTHER')",
            name="origin_allowed",
        ),
        # Elegir «el esmalte mas caro por gramo» es un ORDER BY sobre esta
        # columna, y se hace en cada cotizacion con esmalte.
        Index("ix_v2_material_costs_kind_cost", "material_kind", "effective_cost_per_unit"),
    )
