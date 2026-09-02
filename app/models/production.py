"""Ordenes de produccion: el paso del documento comercial al hecho fisico.

Una cotizacion confirmada es un compromiso; una orden de produccion es la
decision de fabricarla. Son cosas distintas y por eso viven en tablas
distintas: confirmar no consume material, y producir no reescribe el precio.

La frontera esta puesta a proposito en el ARRANQUE y no en la creacion. Crear
la orden es papeleo: reserva el correlativo, congela lo que hay que fabricar y
no toca ni un gramo de inventario. Arrancarla es el hecho fisico: descuenta el
material preparado y deja el movimiento que lo prueba. Quien crea una orden por
error no ha gastado nada; quien la arranca, si.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import quantity_numeric, stock_quantity_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType

#: Longitud de la columna del token opaco del QR. `secrets.token_urlsafe(32)`
#: rinde 43 caracteres; se deja holgura por si la generacion cambia.
QR_TOKEN_LENGTH = 64

#: Minimo de entropia exigido por el esquema. Impide que alguien guarde ahi el
#: codigo de la orden o un correlativo corto.
QR_TOKEN_MIN_LENGTH = 32


class ProductionOrderStatus(StrEnum):
    """Ciclo de vida de la orden.

    Solo hay dos salidas de CREATED —arrancar o anular— y son excluyentes. Una
    vez arrancada, la orden ya consumio material: por eso STARTED y COMPLETED
    no admiten anulacion. Cancelar una orden no devuelve a los sacos el barniz
    que ya se uso, y fingir que si lo hace convierte el inventario en una
    opinion.
    """

    CREATED = "CREATED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


#: Coherencia entre el estado y sus fechas, escrita rama a rama.
#:
#: Los `status IS NOT NULL` NO sobran aunque la columna sea NOT NULL. En SQL
#: `NULL = 'CREATED'` no es FALSE sino NULL, y un CHECK que evalua a NULL se da
#: por CUMPLIDO: un OR de ramas con igualdad deja pasar cualquier fila cuyo
#: discriminante sea nulo. Es el agujero que 0017 y 0019 tuvieron que tapar dos
#: veces. Con el guardia la restriccion es correcta por si misma, y no por
#: confiar en que el NOT NULL de otra clausula siga estando manana.
STATUS_TIMESTAMPS_COHERENT = (
    "(status IS NOT NULL AND status = 'CREATED'"
    " AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'STARTED'"
    " AND started_at IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'COMPLETED'"
    " AND started_at IS NOT NULL AND completed_at IS NOT NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'CANCELLED'"
    " AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)"
)


class ProductionReadinessCode(StrEnum):
    """Por que una orden todavia no puede arrancar.

    Son codigos de dominio y los traduce el frontend. El backend no manda
    frases: una frase traducida en el servidor obliga a desplegar el backend
    para corregir una errata de la interfaz.
    """

    #: La linea no tiene receta: no hay forma de saber que material lleva.
    MISSING_RECIPE = "MISSING_RECIPE"
    #: Hay receta, pero no cuantos gramos por pieza. No se deduce del precio.
    MISSING_MATERIAL_GRAMS = "MISSING_MATERIAL_GRAMS"
    #: La linea no fijo cuantas piezas. Sin cantidad no hay requerimiento.
    MISSING_QUANTITY = "MISSING_QUANTITY"
    #: La receta existe pero no apunta a un material preparado utilizable.
    PREPARED_PRODUCT_NOT_RESOLVABLE = "PREPARED_PRODUCT_NOT_RESOLVABLE"
    #: Nunca ha habido existencia de ese preparado en esta ubicacion.
    PREPARED_STOCK_MISSING = "PREPARED_STOCK_MISSING"
    #: Hay existencia, pero no alcanza.
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    #: El preparado se lleva en una unidad a la que no se puede convertir el
    #: requerimiento en gramos con los datos disponibles. El caso real es ml.
    UNSUPPORTED_UOM_CONVERSION = "UNSUPPORTED_UOM_CONVERSION"
    #: La ubicacion de la orden ya no sirve para descontar.
    INVALID_STOCK_LOCATION = "INVALID_STOCK_LOCATION"


class ProductionOrder(Base, TimestampMixin):
    """Decision de fabricar una cotizacion confirmada."""

    __tablename__ = "production_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: UNICA por cotizacion, y la unicidad la impone la base. Que el servicio
    #: compruebe antes de insertar no basta: dos peticiones simultaneas pasan
    #: las dos la comprobacion y crean dos ordenes del mismo pedido, cada una
    #: dispuesta a consumir el material entero.
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotations.id", ondelete="RESTRICT"), nullable=False, unique=True
    )

    #: De donde sale el material. Explicita siempre: no hay ubicacion por
    #: defecto ni aunque hoy solo exista una, porque el dia que haya dos el
    #: default silencioso descontaria del almacen equivocado sin avisar.
    stock_location_id: Mapped[int] = mapped_column(
        ForeignKey("stock_locations.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    status: Mapped[ProductionOrderStatus] = mapped_column(
        StrEnumType(ProductionOrderStatus, 16),
        nullable=False,
        default=ProductionOrderStatus.CREATED,
        index=True,
    )

    #: Reintento de red del cliente. Nula cuando no la manda: la unicidad
    #: fisica no descansa aqui sino en `quotation_id` y en el estado.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)

    #: Identificador opaco del QR. No es el id ni el codigo: un token
    #: secuencial dejaria recorrer las ordenes ajenas cambiando un digito.
    qr_token: Mapped[str] = mapped_column(String(QR_TOKEN_LENGTH), nullable=False, unique=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED', 'STARTED', 'COMPLETED', 'CANCELLED')",
            name="status_allowed",
        ),
        CheckConstraint(STATUS_TIMESTAMPS_COHERENT, name="status_timestamps_coherent"),
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        CheckConstraint(
            f"length(btrim(qr_token)) >= {QR_TOKEN_MIN_LENGTH}", name="qr_token_long_enough"
        ),
    )

    lines: Mapped[list[ProductionOrderLine]] = relationship(
        "ProductionOrderLine",
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="ProductionOrderLine.sort_order",
    )


class ProductionOrderLine(Base, TimestampMixin):
    """Lo que hay que fabricar, copiado de la cotizacion confirmada.

    Todos los datos tecnicos son copia y no referencia viva. Al arrancar no se
    vuelve a leer el maestro: si alguien cambia la receta o el gramaje entre
    crear y arrancar, se fabrica lo que se decidio, no lo que diga el maestro
    esa tarde. La cotizacion confirmada ya es inmutable, asi que copiar de ella
    no puede contradecirla.
    """

    __tablename__ = "production_order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    production_order_id: Mapped[int] = mapped_column(
        ForeignKey("production_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quotation_item_id: Mapped[int] = mapped_column(
        ForeignKey("quotation_items.id", ondelete="RESTRICT"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    product_internal_reference_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Nula cuando la cotizacion no la fijo. Sin cantidad no hay requerimiento
    #: que calcular, y arrancar queda bloqueado.
    quantity: Mapped[int | None] = mapped_column(Integer)

    #: Medidas efectivas ya congeladas en la cotizacion (Fase 009B). Van al
    #: documento de taller: quien fabrica necesita saber de que tamano.
    width_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    height_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    length_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    depth_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())

    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipes.id", ondelete="RESTRICT"))
    recipe_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("recipe_versions.id", ondelete="RESTRICT")
    )
    recipe_version_fingerprint_snapshot: Mapped[str | None] = mapped_column(String(64))
    material_grams_per_piece: Mapped[Decimal | None] = mapped_column(quantity_numeric())

    #: El preparado que la receta produce, resuelto AL CREAR. Nulo cuando la
    #: linea no trae receta o la receta no apunta a ningun preparado.
    prepared_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    #: Cuanto material preparado pide esta linea, y en que unidad. Se calcula
    #: al crear a partir de datos ya congelados, de modo que no puede cambiar
    #: despues por su cuenta.
    required_material_quantity: Mapped[Decimal | None] = mapped_column(stock_quantity_numeric())
    required_material_uom: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        UniqueConstraint(
            "production_order_id", "quotation_item_id", name="uq_production_order_lines_item"
        ),
        UniqueConstraint(
            "production_order_id", "sort_order", name="uq_production_order_lines_sort_order"
        ),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "material_grams_per_piece IS NULL OR material_grams_per_piece > 0",
            name="material_grams_positive",
        ),
        CheckConstraint(
            "required_material_quantity IS NULL OR required_material_quantity >= 0",
            name="required_material_non_negative",
        ),
        Index("ix_production_order_lines_quotation_item", "quotation_item_id"),
    )

    order: Mapped[ProductionOrder] = relationship("ProductionOrder", back_populates="lines")
