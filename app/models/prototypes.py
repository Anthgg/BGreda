"""Prototipos: la muestra fisica que se hace ANTES de fabricar en serie.

Un prototipo no es una orden de produccion pequena, y por eso no vive en
`production_orders`. Tres cosas lo impiden y las tres son deliberadas:

1. Una cotizacion admite UNA orden de produccion —lo impone un UNIQUE— para
   que dos peticiones simultaneas no creen dos ordenes del mismo pedido, cada
   una dispuesta a consumir el material entero. Un prototipo y la produccion
   final de la misma cotizacion no caben ahi.
2. Los materiales de una orden se DERIVAN de la cotizacion confirmada: nadie
   los elige a mano. Eso protege la produccion final y no debe ablandarse. Un
   prototipo necesita exactamente lo contrario: se eligen uno a uno los
   materiales que esa muestra concreta va a gastar.
3. `production_orders.quotation_id` es NOT NULL, y un prototipo puede existir
   sin cotizacion y sin producto todavia —una muestra preliminar de algo que
   aun no esta en el catalogo—.

**Dos ejes, no uno.** `status` dice si la muestra se fabrico; `approval` dice
si convencio. Son independientes: lo normal es quedarse en COMPLETED + PENDING
mientras alguien la mira. Confundirlos haria que aprobar significara fabricar,
o que fabricar diera por buena una pieza que nadie ha visto.

**No hay eje de pago propio.** Para gastar material hace falta que la
cotizacion de origen conste cobrada (Fase 009H.1). Un prototipo sin cotizacion
no puede arrancar: no hay nada que cobrar contra que comprobarlo.
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
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import stock_quantity_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType


class PrototypeStatus(StrEnum):
    """Donde esta la muestra fisicamente.

    Mismo ciclo que una orden de produccion, y por el mismo motivo: arrancar es
    el hecho que gasta material, y una vez gastado no se deshace. Por eso
    CANCELLED solo sale de CREATED —anular una muestra ya fabricada no devuelve
    el barro al saco, y fingir que si lo hace convierte el inventario en una
    opinion—.
    """

    CREATED = "CREATED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class PrototypeApproval(StrEnum):
    """Que se decidio sobre la muestra, una vez existe.

    Eje aparte de `status` a proposito. Una muestra fabricada y todavia sin
    mirar es COMPLETED + PENDING, y eso no es un estado a medias: es el estado
    normal mientras alguien la evalua.

    REJECTED no se reescribe nunca. Si hace falta otra muestra se crea un
    prototipo sucesor, y el rechazado se queda como historia de que aquella no
    valio.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


#: Coherencia entre el estado fisico y sus fechas, rama a rama.
#:
#: Los `status IS NOT NULL` NO sobran aunque la columna sea NOT NULL. En SQL
#: `NULL = 'CREATED'` no es FALSE sino NULL, y un CHECK que evalua a NULL se da
#: por CUMPLIDO: un OR de ramas con igualdad deja pasar cualquier fila cuyo
#: discriminante sea nulo. Es el agujero que 0017 y 0019 tuvieron que tapar dos
#: veces y que 0020 ya escribio asi.
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

#: La decision solo existe sobre una muestra que existe, y siempre lleva fecha.
#:
#: Sin la segunda rama se podria dejar `APPROVED` sobre un prototipo que nadie
#: llego a fabricar, y ese registro afirmaria que alguien vio una pieza que no
#: se hizo.
APPROVAL_COHERENT = (
    "(approval IS NOT NULL AND approval = 'PENDING' AND decided_at IS NULL)"
    " OR (approval IS NOT NULL AND approval IN ('APPROVED', 'REJECTED')"
    " AND decided_at IS NOT NULL"
    " AND status IS NOT NULL AND status = 'COMPLETED')"
)

#: Lo que hace falta para haber gastado material: de que cotizacion se cobro y
#: de que almacen salio. Antes de arrancar los dos pueden faltar —una muestra
#: preliminar se registra sin saberlo todavia— pero una vez arrancada ya no.
#:
#: El PAGO no se puede comprobar aqui: vive en otra tabla y un CHECK no cruza
#: filas. Lo impone el servicio, y hay pruebas contra PostgreSQL que lo
#: demuestran con el inventario intacto detras.
STARTED_REQUIRES_ORIGIN = (
    "status IS NULL"
    " OR status NOT IN ('STARTED', 'COMPLETED')"
    " OR (quotation_id IS NOT NULL AND stock_location_id IS NOT NULL)"
)


class Prototype(Base, TimestampMixin):
    """Una muestra fisica y su historia."""

    __tablename__ = "prototypes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: Como se llama la muestra. Obligatorio incluso sin producto: es lo unico
    #: que la identifica cuando todavia no hay maestro que la nombre.
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    #: Nula mientras la muestra no cuelga de ningun pedido. Es tambien la
    #: unica via de cobro: sin cotizacion no hay pago posible y por tanto no
    #: hay arranque.
    quotation_id: Mapped[int | None] = mapped_column(
        ForeignKey("quotations.id", ondelete="RESTRICT"), index=True
    )

    #: Nulo cuando la pieza todavia no existe en el catalogo. El negocio lo
    #: admite explicitamente: se prototipa para decidir si merece la pena
    #: crearla.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )

    #: De donde sale el material. Nula mientras no se arranca; exigida para
    #: consumir. No hay ubicacion por defecto ni aunque hoy solo exista una:
    #: el dia que haya dos, el default silencioso descontaria del almacen
    #: equivocado sin avisar.
    stock_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_locations.id", ondelete="RESTRICT"), index=True
    )

    #: Cuantas piezas de muestra. Del Excel del taller, que la lleva como
    #: columna propia: no se asume 1.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    status: Mapped[PrototypeStatus] = mapped_column(
        StrEnumType(PrototypeStatus, 16),
        nullable=False,
        default=PrototypeStatus.CREATED,
        index=True,
    )
    approval: Mapped[PrototypeApproval] = mapped_column(
        StrEnumType(PrototypeApproval, 16),
        nullable=False,
        default=PrototypeApproval.PENDING,
        index=True,
    )

    #: Cuando se pidio la muestra y en cuantos dias se espera. Los dias van por
    #: prototipo y no en configuracion: en la hoja del taller cada muestra
    #: lleva el suyo (7, 5, 10) porque depende de la pieza, no de la casa.
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_days: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    notes: Mapped[str | None] = mapped_column(Text)

    #: A que muestra anterior sustituye. Una rechazada no se reescribe: se
    #: crea la siguiente y se dice de cual viene. Asi el historial conserva que
    #: la primera no valio, en vez de fingir que siempre estuvo bien.
    supersedes_prototype_id: Mapped[int | None] = mapped_column(
        ForeignKey("prototypes.id", ondelete="RESTRICT")
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED', 'STARTED', 'COMPLETED', 'CANCELLED')",
            name="status_allowed",
        ),
        CheckConstraint("approval IN ('PENDING', 'APPROVED', 'REJECTED')", name="approval_allowed"),
        CheckConstraint(STATUS_TIMESTAMPS_COHERENT, name="status_timestamps_coherent"),
        CheckConstraint(APPROVAL_COHERENT, name="approval_coherent"),
        CheckConstraint(STARTED_REQUIRES_ORIGIN, name="started_requires_origin"),
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("target_days IS NULL OR target_days > 0", name="target_days_positive"),
        # Una muestra no se sustituye a si misma. Es el ciclo mas corto posible
        # y el unico que la base puede ver sola; los largos los corta el
        # servicio recorriendo la cadena.
        CheckConstraint(
            "supersedes_prototype_id IS NULL OR supersedes_prototype_id <> id",
            name="no_self_supersede",
        ),
        # Un prototipo tiene como mucho UN sucesor. Sin esto, dos iteraciones
        # colgando de la misma rechazada dejarian la cadena bifurcada y
        # «cual es la vigente» pasaria a ser una opinion.
        UniqueConstraint("supersedes_prototype_id", name="uq_prototypes_single_successor"),
        Index("ix_prototypes_quotation_status", "quotation_id", "status"),
    )

    #: `selectin` y no la carga perezosa por defecto: no hay ni una lectura de
    #: una muestra que no necesite sus materiales —la disponibilidad los
    #: recorre, la respuesta los enumera y hasta el resumen los cuenta—, y en
    #: sesion asincrona una carga perezosa no es un viaje de mas sino un
    #: `MissingGreenlet`. Dejarlo en manos de que cada consulta se acuerde de
    #: pedir `selectinload` significa que la que se olvide reviente, y la
    #: primera que se olvido fue crear una muestra sin materiales.
    lines: Mapped[list[PrototypeMaterialLine]] = relationship(
        "PrototypeMaterialLine",
        back_populates="prototype",
        cascade="all, delete-orphan",
        order_by="PrototypeMaterialLine.sort_order",
        lazy="selectin",
    )


class PrototypeMaterialLine(Base, TimestampMixin):
    """Un material que ESTA muestra va a gastar. Elegido, nunca deducido.

    Aqui esta la diferencia con la produccion final. Una orden de produccion
    calcula lo que necesita a partir de la receta de la cotizacion; un
    prototipo lleva lo que alguien decidio ponerle. Una muestra sin esmaltar no
    gasta barniz porque nadie anadio barniz, no porque una regla lo dedujera.
    """

    __tablename__ = "prototype_material_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prototype_id: Mapped[int] = mapped_column(
        ForeignKey("prototypes.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Un producto real del catalogo. Nunca texto libre: lo que se escribe a
    #: mano no tiene saldo del que descontar.
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    quantity: Mapped[Decimal] = mapped_column(stock_quantity_numeric(), nullable=False)
    #: La unidad en que se declaro, que es la base del producto. Se guarda para
    #: que la linea siga diciendo lo que decia si manana el maestro cambia.
    uom_code: Mapped[str] = mapped_column(String(32), nullable=False)

    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    product_internal_reference_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        # El mismo material una sola vez. Dos lineas del mismo insumo se
        # descontarian las dos, y la cantidad real acabaria siendo la suma de
        # dos numeros que nadie escribio juntos.
        UniqueConstraint("prototype_id", "product_id", name="uq_prototype_material_product"),
        UniqueConstraint("prototype_id", "sort_order", name="uq_prototype_material_sort_order"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("length(btrim(uom_code)) > 0", name="uom_not_blank"),
    )

    prototype: Mapped[Prototype] = relationship("Prototype", back_populates="lines")
