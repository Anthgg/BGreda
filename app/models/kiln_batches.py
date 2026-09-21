"""Hornadas planificadas, sus asignaciones y la carga interna del taller. Fase 010L.

## Por que una entidad nueva, y por que con este nombre

La hornada es el hecho FISICO del taller: un horno, un ciclo —baja o alta— y
una ejecucion con fecha, en la que viajan piezas de varias ordenes a la vez.
No habia nada que la representara:

- `firings` es la hoja de COSTEO del Cotizador Legacy. Lleva dinero, el factor
  por ocupacion x3 que V2 abandono y estados comerciales (borrador, confirmada,
  anulada), y una sola hoja junta VARIOS hornos y LOS DOS ciclos. Adaptarla
  para planificar habria obligado a tocar Legacy, que sigue intacto hasta 010N;
- las notas `FIRING_NOTE` de una orden son registro de lo que paso, no plan.

El nombre es neutral a proposito —`kiln_batches` y no `v2_kiln_batches`—: una
hornada no es de ningun motor. Lleva piezas de Cotizador V2, de Solo Quema y del
propio taller, y el dia que haya un cuarto origen seguira siendo la misma
hornada.

## La capacidad la garantiza la BASE, no el servicio

Una hornada nunca pasa del 100 %. El servicio lo comprueba bajo bloqueo y
devuelve un 409 explicativo, pero la garantia de verdad esta aqui abajo:

- el volumen de cada asignacion es exactamente el de sus piezas
  (`assigned_volume_cm3 = quantity * unit_volume_snapshot_cm3`, todo positivo);
- un TRIGGER aplica a la hornada el DELTA de volumen activo de cada escritura
  sobre las asignaciones —alta, liberacion, cambio de cantidad, borrado—, asi
  que su contador es por construccion la suma de lo activo;
- un CHECK impide que ese contador supere la capacidad congelada.

Delta y no `SUM(...)`: bajo READ COMMITTED, la actualizacion que espera un
bloqueo se reevalua sobre la ULTIMA version de la fila, y los deltas componen
bien; un `SET = (SELECT SUM ...)` podria leer una foto vieja y perder la suma
del otro. Asi ninguna escritura, venga de donde venga, deja una hornada por
encima de su capacidad.

## Asignar es repartir PIEZAS, no porcentajes

Una asignacion dice cuantas piezas de una linea concreta viajan en una hornada.
El porcentaje se deduce. Eso permite partir una orden del 120 % en 100 % + 20 %,
y le deja a 010M —el mapa— saber QUE piezas hay en cada hornada, no solo cuanto
ocupan.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import quantity_numeric
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType
from app.models.firings import FiringType
from app.models.quoter_v2 import V2FiringMode

#: Longitud de las claves de idempotencia: la misma que el resto del sistema.
IDEMPOTENCY_KEY_LENGTH = 64


class KilnBatchStatus(StrEnum):
    """Ciclo de vida de una hornada.

    Sin un estado READY entre PLANNED y STARTED: en una planificacion que solo
    sabe de volumen, «lista» no significa nada que PLANNED no diga ya. Si 010M
    necesita cerrar el acomodo fisico antes de encender, lo anadira alli, con su
    regla, en vez de heredar aqui un estado vacio.
    """

    PLANNED = "PLANNED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


#: Estados en los que se puede asignar, quitar, mover y reprogramar.
KILN_BATCH_EDITABLE = frozenset({KilnBatchStatus.PLANNED})

#: Coherencia entre estado y fechas, rama a rama. Los `status IS NOT NULL` no
#: sobran: `NULL = 'PLANNED'` es NULL, y un CHECK que evalua a NULL se da por
#: cumplido. Es el agujero que 0017 y 0019 tuvieron que tapar.
KILN_BATCH_STATUS_TIMESTAMPS = (
    "(status IS NOT NULL AND status = 'PLANNED'"
    " AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'STARTED'"
    " AND started_at IS NOT NULL AND completed_at IS NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'COMPLETED'"
    " AND started_at IS NOT NULL AND completed_at IS NOT NULL AND cancelled_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'CANCELLED'"
    " AND started_at IS NULL AND completed_at IS NULL AND cancelled_at IS NOT NULL)"
)


class KilnBatchSourceKind(StrEnum):
    """De donde vienen las piezas de una asignacion.

    Legacy y prototipos NO estan, y es deliberado: no tienen un volumen aprobado
    con separacion al que atar una reserva de capacidad, y Legacy se retira en
    010N. Planificarlos obligaria a inventar un volumen.
    """

    V2_QUOTATION = "V2_QUOTATION"
    FIRING_V2 = "FIRING_V2"
    INTERNAL = "INTERNAL"


class KilnBatchAssignmentStatus(StrEnum):
    """Una asignacion quitada queda RELEASED, no se borra: la historia se lee."""

    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


#: Exactamente un origen de linea y exactamente un padre, y coherentes entre si.
#: Cada rama nombra los CINCO campos: una rama que mirara menos dejaria pasar
#: una fila con el sobrante relleno —una pieza de Solo Quema colgada de una
#: carga interna, por ejemplo—.
ASSIGNMENT_SOURCE_COHERENT = (
    "(source_kind IS NOT NULL AND source_kind = 'V2_QUOTATION'"
    " AND production_order_id IS NOT NULL AND v2_quotation_product_id IS NOT NULL"
    " AND v2_firing_quotation_line_id IS NULL"
    " AND internal_load_id IS NULL AND internal_load_line_id IS NULL)"
    " OR (source_kind IS NOT NULL AND source_kind = 'FIRING_V2'"
    " AND production_order_id IS NOT NULL AND v2_firing_quotation_line_id IS NOT NULL"
    " AND v2_quotation_product_id IS NULL"
    " AND internal_load_id IS NULL AND internal_load_line_id IS NULL)"
    " OR (source_kind IS NOT NULL AND source_kind = 'INTERNAL'"
    " AND internal_load_id IS NOT NULL AND internal_load_line_id IS NOT NULL"
    " AND production_order_id IS NULL"
    " AND v2_quotation_product_id IS NULL AND v2_firing_quotation_line_id IS NULL)"
)

#: Una RELEASED dice cuando se quito; una ACTIVE, que no se ha quitado.
ASSIGNMENT_RELEASE_COHERENT = (
    "(status IS NOT NULL AND status = 'ACTIVE' AND released_at IS NULL)"
    " OR (status IS NOT NULL AND status = 'RELEASED' AND released_at IS NOT NULL)"
)


class KilnBatch(Base, TimestampMixin):
    """Una hornada: un horno, un ciclo y una ejecucion con fecha."""

    __tablename__ = "kiln_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kiln_id: Mapped[int] = mapped_column(
        ForeignKey("kilns.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: UN ciclo por hornada. Baja y alta no se mezclan en una misma ejecucion:
    #: son temperaturas distintas y el horno se enciende para una de ellas.
    firing_type: Mapped[FiringType] = mapped_column(StrEnumType(FiringType, 8), nullable=False)
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[KilnBatchStatus] = mapped_column(
        StrEnumType(KilnBatchStatus, 16),
        nullable=False,
        default=KilnBatchStatus.PLANNED,
        server_default=text("'PLANNED'"),
        index=True,
    )

    #: Congelados al crearla. Si manana se amplia el horno maestro, esta hornada
    #: sigue siendo la que se planifico: no cambia de capacidad por debajo.
    kiln_name_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    capacity_snapshot_cm3: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)

    #: Suma del volumen de las asignaciones ACTIVAS. La mantiene un trigger, no
    #: el servicio: ver el docstring del modulo.
    assigned_volume_cm3: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    #: Una hornada con una orden EXCLUSIVA no admite a nadie mas, y su libre no
    #: se ofrece. Vuelve a `false` cuando se vacia.
    exclusive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Sube con cada cambio de asignacion o de fecha. Es el contrato de
    #: concurrencia optimista que usaran la pantalla y 010M.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    notes: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        CheckConstraint(
            "status IN ('PLANNED', 'STARTED', 'COMPLETED', 'CANCELLED')", name="status_allowed"
        ),
        CheckConstraint("firing_type IN ('LOW', 'HIGH')", name="firing_type_allowed"),
        CheckConstraint(KILN_BATCH_STATUS_TIMESTAMPS, name="status_timestamps_coherent"),
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        CheckConstraint("capacity_snapshot_cm3 > 0", name="capacity_positive"),
        CheckConstraint("assigned_volume_cm3 >= 0", name="assigned_non_negative"),
        # LA garantia: ninguna escritura deja la hornada por encima del 100 %.
        CheckConstraint(
            "assigned_volume_cm3 <= capacity_snapshot_cm3", name="assigned_within_capacity"
        ),
        # Una hornada vacia no puede estar reservada en exclusiva.
        CheckConstraint("NOT exclusive OR assigned_volume_cm3 > 0", name="exclusive_not_empty"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "cancel_reason IS NULL OR length(cancel_reason) <= 500", name="cancel_reason_length"
        ),
        CheckConstraint("notes IS NULL OR length(notes) <= 2000", name="notes_length"),
    )

    assignments: Mapped[list[KilnBatchAssignment]] = relationship(
        "KilnBatchAssignment",
        back_populates="batch",
        order_by=lambda: KilnBatchAssignment.id.asc(),
    )


class InternalLoad(Base, TimestampMixin):
    """Carga interna del taller: piezas propias que ocupan horno sin cotizacion.

    Sin precio, sin factor y sin cliente, a proposito. Son piezas que el taller
    hace para vender el mismo y que aprovechan el espacio que las ordenes dejan
    libre. Inventarles una cotizacion para poder planificarlas seria fabricar un
    documento comercial falso.

    No es una `production_order`: aquella exige exactamente un origen comercial
    y un almacen, y meter aqui un origen «ninguno» abriria el CHECK de origen a
    las ordenes que nacen de la nada.
    """

    __tablename__ = "internal_loads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    #: Que ciclos necesitan sus piezas. Al menos uno.
    low_fire_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    high_fire_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Con la que se calculo el volumen de las lineas; congelada con ellas.
    piece_separation_cm: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("low_fire_required OR high_fire_required", name="some_cycle"),
        CheckConstraint(
            "piece_separation_cm >= 0 AND piece_separation_cm <= 20", name="separation_range"
        ),
        CheckConstraint("notes IS NULL OR length(notes) <= 2000", name="notes_length"),
    )

    lines: Mapped[list[InternalLoadLine]] = relationship(
        "InternalLoadLine",
        back_populates="load",
        cascade="all, delete-orphan",
        order_by=lambda: (InternalLoadLine.sort_order.asc(), InternalLoadLine.id.asc()),
    )


class InternalLoadLine(Base, TimestampMixin):
    """Una pieza —o un grupo de piezas iguales— de una carga interna."""

    __tablename__ = "internal_load_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    load_id: Mapped[int] = mapped_column(
        ForeignKey("internal_loads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    length_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    width_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    height_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: (L+s)(A+s)(H+s), con la separacion de la carga. Congelado.
    unit_volume_cm3: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    total_volume_cm3: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)

    __table_args__ = (
        # Sostiene la FK compuesta de las asignaciones: la base impide colgar la
        # linea de una carga en una asignacion de OTRA carga.
        UniqueConstraint("id", "load_id", name="uq_internal_load_lines_id_load"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "length_cm > 0 AND width_cm > 0 AND height_cm > 0", name="dimensions_positive"
        ),
        CheckConstraint("unit_volume_cm3 > 0", name="unit_volume_positive"),
        CheckConstraint(
            "total_volume_cm3 = unit_volume_cm3 * quantity", name="total_volume_matches"
        ),
    )

    load: Mapped[InternalLoad] = relationship("InternalLoad", back_populates="lines")


class KilnBatchAssignment(Base, TimestampMixin):
    """Cuantas piezas de UNA linea viajan en UNA hornada."""

    __tablename__ = "kiln_batch_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("kiln_batches.id", ondelete="RESTRICT", name="fk_kiln_batch_assignments_batch"),
        nullable=False,
        index=True,
    )
    source_kind: Mapped[KilnBatchSourceKind] = mapped_column(
        StrEnumType(KilnBatchSourceKind, 16), nullable=False
    )
    status: Mapped[KilnBatchAssignmentStatus] = mapped_column(
        StrEnumType(KilnBatchAssignmentStatus, 16),
        nullable=False,
        default=KilnBatchAssignmentStatus.ACTIVE,
        server_default=text("'ACTIVE'"),
    )

    # ---- El padre: una orden de produccion o una carga interna -------------
    production_order_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "production_orders.id", ondelete="RESTRICT", name="fk_kiln_batch_assignments_order"
        ),
        index=True,
    )
    internal_load_id: Mapped[int | None] = mapped_column(
        ForeignKey("internal_loads.id", ondelete="RESTRICT", name="fk_kiln_batch_assignments_load"),
        index=True,
    )

    # ---- La linea: exactamente una de las tres -----------------------------
    v2_quotation_product_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "v2_quotation_products.id",
            ondelete="RESTRICT",
            name="fk_kiln_batch_assignments_v2_line",
        ),
        index=True,
    )
    v2_firing_quotation_line_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "v2_firing_quotation_lines.id",
            ondelete="RESTRICT",
            name="fk_kiln_batch_assignments_fq_line",
        ),
        index=True,
    )
    internal_load_line_id: Mapped[int | None] = mapped_column(Integer, index=True)

    # ---- Lo que ocupa, congelado -------------------------------------------
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Volumen de UNA pieza con su separacion, copiado de la linea de origen.
    unit_volume_snapshot_cm3: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: `quantity * unit_volume_snapshot_cm3`. En la fila y no calculado aparte:
    #: el delta del trigger no puede depender de otra tabla.
    assigned_volume_cm3: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: Del snapshot de la cotizacion. La carga interna es siempre compartida.
    firing_mode: Mapped[V2FiringMode] = mapped_column(StrEnumType(V2FiringMode, 16), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)

    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('V2_QUOTATION', 'FIRING_V2', 'INTERNAL')", name="source_kind_allowed"
        ),
        CheckConstraint("status IN ('ACTIVE', 'RELEASED')", name="status_allowed"),
        CheckConstraint("firing_mode IN ('SHARED', 'EXCLUSIVE')", name="firing_mode_allowed"),
        CheckConstraint(ASSIGNMENT_SOURCE_COHERENT, name="source_coherent"),
        CheckConstraint(ASSIGNMENT_RELEASE_COHERENT, name="release_coherent"),
        # Todo positivo: con un delta negativo el contador de la hornada BAJARIA
        # y abriria hueco para pasarse de su capacidad.
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_volume_snapshot_cm3 > 0", name="unit_volume_positive"),
        CheckConstraint("assigned_volume_cm3 > 0", name="assigned_volume_positive"),
        CheckConstraint(
            "assigned_volume_cm3 = quantity * unit_volume_snapshot_cm3",
            name="assigned_volume_matches",
        ),
        CheckConstraint(
            "internal_load_id IS NULL OR firing_mode = 'SHARED'", name="internal_is_shared"
        ),
        # La linea de una carga interna es DE ESA carga.
        ForeignKeyConstraint(
            ["internal_load_line_id", "internal_load_id"],
            ["internal_load_lines.id", "internal_load_lines.load_id"],
            ondelete="RESTRICT",
            name="fk_kiln_batch_assignments_load_line",
        ),
        # Una linea tiene como mucho UNA asignacion activa por hornada: se ajusta
        # la cantidad, no se duplica la fila.
        Index(
            "uq_kiln_batch_assignments_active_v2",
            "batch_id",
            "v2_quotation_product_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND v2_quotation_product_id IS NOT NULL"),
        ),
        Index(
            "uq_kiln_batch_assignments_active_fq",
            "batch_id",
            "v2_firing_quotation_line_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND v2_firing_quotation_line_id IS NOT NULL"),
        ),
        Index(
            "uq_kiln_batch_assignments_active_int",
            "batch_id",
            "internal_load_line_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND internal_load_line_id IS NOT NULL"),
        ),
    )

    batch: Mapped[KilnBatch] = relationship("KilnBatch", back_populates="assignments")


class KilnBatchOperationKind(StrEnum):
    CREATE_BATCH = "CREATE_BATCH"
    ASSIGN = "ASSIGN"
    RELEASE = "RELEASE"
    MOVE = "MOVE"
    LAYOUT = "LAYOUT"


class KilnBatchOperation(Base, TimestampMixin):
    """Registro de idempotencia de las operaciones de planificacion.

    Asignar, quitar o mover tocan VARIAS filas a la vez, asi que una clave
    unica por fila no sirve para reconocer un reintento. Se registra la
    OPERACION con la huella de su contenido: la misma clave con la misma huella
    es el reintento y devuelve lo que devolvio; la misma clave con otra huella es
    un error de quien llama, y se rechaza en vez de aplicar algo distinto a lo
    que se pidio la primera vez.
    """

    __tablename__ = "kiln_batch_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(IDEMPOTENCY_KEY_LENGTH), nullable=False, unique=True
    )
    kind: Mapped[KilnBatchOperationKind] = mapped_column(
        StrEnumType(KilnBatchOperationKind, 16), nullable=False
    )
    #: SHA-256 del contenido canonico de la peticion.
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("kiln_batches.id", ondelete="RESTRICT", name="fk_kiln_batch_operations_batch"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('CREATE_BATCH', 'ASSIGN', 'RELEASE', 'MOVE', 'LAYOUT')",
            name="kind_allowed",
        ),
        CheckConstraint("length(btrim(idempotency_key)) >= 8", name="idempotency_key_long_enough"),
        CheckConstraint("length(payload_fingerprint) = 64", name="fingerprint_is_sha256"),
    )



# ---------------------------------------------------------------------------
# Layout fisico del horno — Fase 010M
# ---------------------------------------------------------------------------

class KilnBatchLayout(Base, TimestampMixin):
    """Mapa fisico de una hornada: como se acomodan las piezas en el horno.

    Es un snapshot de los datos del horno en el momento de crear el layout.
    Si el maestro Kiln cambia despues, este snapshot NO cambia: el historico
    queda intacto.

    Existe como maximo UN layout por hornada (UNIQUE batch_id).
    Versiones optimistas: `version` sube con cada PUT, y el cliente debe
    mandar `expected_version` para guardar.
    """

    __tablename__ = "kiln_batch_layouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("kiln_batches.id", ondelete="CASCADE", name="fk_kiln_batch_layouts_batch_id"),
        nullable=False,
        unique=True,
    )
    #: Snapshot de las dimensiones utiles del horno al momento de crear el layout.
    kiln_width_cm_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    kiln_depth_cm_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    kiln_height_cm_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: Contador de version para concurrencia optimista.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    __table_args__ = (
        CheckConstraint(
            "kiln_width_cm_snapshot > 0", name="ck_kiln_batch_layouts_width_positive"
        ),
        CheckConstraint(
            "kiln_depth_cm_snapshot > 0", name="ck_kiln_batch_layouts_depth_positive"
        ),
        CheckConstraint(
            "kiln_height_cm_snapshot > 0",
            name="ck_kiln_batch_layouts_height_positive",
        ),
        CheckConstraint("version >= 1", name="ck_kiln_batch_layouts_version_positive"),
    )

    levels: Mapped[list[KilnBatchLayoutLevel]] = relationship(
        "KilnBatchLayoutLevel",
        back_populates="layout",
        cascade="all, delete-orphan",
        order_by=lambda: KilnBatchLayoutLevel.level_index.asc(),
    )
    placements: Mapped[list[KilnBatchLayoutPlacement]] = relationship(
        "KilnBatchLayoutPlacement",
        back_populates="layout",
        cascade="all, delete-orphan",
        order_by=lambda: KilnBatchLayoutPlacement.id.asc(),
    )


class KilnBatchLayoutLevel(Base, TimestampMixin):
    """Un nivel (estante) planificable del layout del horno.

    Cada nivel tiene un indice (level_index), una altura de la base (z_cm)
    y una altura util (usable_height_cm). Los campos de placa son opcionales
    porque no existe todavia una configuracion maestra de placas.
    """

    __tablename__ = "kiln_batch_layout_levels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    layout_id: Mapped[int] = mapped_column(
        ForeignKey(
            "kiln_batch_layouts.id",
            ondelete="CASCADE",
            name="fk_kiln_batch_layout_levels_layout_id",
        ),
        nullable=False,
        index=True,
    )
    #: Orden del nivel de abajo a arriba, empezando en 0.
    level_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Etiqueta descriptiva del nivel (opcional).
    name: Mapped[str | None] = mapped_column(String(120))
    #: Altura de la base del nivel desde el suelo del horno, en cm.
    z_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: Espacio libre entre la base de este nivel y el techo del siguiente (o del horno).
    usable_height_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: Etiqueta de la placa usada en este nivel (opcional; no existe maestro todavia).
    plate_label: Mapped[str | None] = mapped_column(String(100))
    #: Grosor de la placa en cm (nullable: sin maestro de placas todavia).
    plate_thickness_cm: Mapped[Decimal | None] = mapped_column(quantity_numeric())

    __table_args__ = (
        UniqueConstraint(
            "layout_id",
            "level_index",
            name="uq_kiln_batch_layout_levels_layout_level",
        ),
        CheckConstraint(
            "level_index >= 0",
            name="ck_kiln_batch_layout_levels_level_index_non_negative",
        ),
        CheckConstraint("z_cm >= 0", name="ck_kiln_batch_layout_levels_z_non_negative"),
        CheckConstraint(
            "usable_height_cm > 0",
            name="ck_kiln_batch_layout_levels_usable_height_positive",
        ),
        CheckConstraint(
            "plate_thickness_cm IS NULL OR plate_thickness_cm >= 0",
            name="ck_kiln_batch_layout_levels_plate_thickness_non_negative",
        ),
    )

    layout: Mapped[KilnBatchLayout] = relationship("KilnBatchLayout", back_populates="levels")


class KilnBatchLayoutPlacement(Base, TimestampMixin):
    """El acomodo de un grupo de piezas de una asignacion en un nivel del layout.

    Las dimensiones de las piezas se copian del snapshot de la asignacion
    en el momento de guardar el placement. Si despues cambia el producto
    maestro, este snapshot NO cambia.

    rotation_degrees: solo 0 o 90. Geometricamente, 180 y 270 son redundantes
    para bounding boxes rectangulares.
    """

    __tablename__ = "kiln_batch_layout_placements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    layout_id: Mapped[int] = mapped_column(
        ForeignKey(
            "kiln_batch_layouts.id",
            ondelete="CASCADE",
            name="fk_kiln_batch_layout_placements_layout_id",
        ),
        nullable=False,
        index=True,
    )
    batch_assignment_id: Mapped[int] = mapped_column(
        ForeignKey(
            "kiln_batch_assignments.id",
            ondelete="RESTRICT",
            name="fk_kiln_batch_layout_placements_assignment_id",
        ),
        nullable=False,
        index=True,
    )
    #: Indice de grupo dentro de la asignacion (para multiples sub-grupos).
    group_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Indice de unidad dentro del grupo (nullable si el grupo no distingue unidades).
    unit_index: Mapped[int | None] = mapped_column(Integer)
    #: Cantidad de piezas representadas por este placement.
    quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    #: Nivel en el que se ubica el placement (referencia logica al level_index).
    level_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Posicion X en cm desde el borde izquierdo del nivel.
    x_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: Posicion Y en cm desde el borde frontal del nivel.
    y_cm: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: Rotacion en grados. Solo 0 o 90.
    rotation_degrees: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Snapshots de dimensiones de la pieza al momento de guardar.
    piece_length_cm_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    piece_width_cm_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    piece_height_cm_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    separation_cm_snapshot: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )

    __table_args__ = (
        CheckConstraint(
            "quantity > 0",
            name="ck_kiln_batch_layout_placements_quantity_positive",
        ),
        CheckConstraint(
            "level_index >= 0",
            name="ck_kiln_batch_layout_placements_level_index_non_negative",
        ),
        CheckConstraint("x_cm >= 0", name="ck_kiln_batch_layout_placements_x_non_negative"),
        CheckConstraint("y_cm >= 0", name="ck_kiln_batch_layout_placements_y_non_negative"),
        CheckConstraint(
            "rotation_degrees IN (0, 90)",
            name="ck_kiln_batch_layout_placements_rotation_allowed",
        ),
        CheckConstraint(
            "piece_length_cm_snapshot > 0",
            name="ck_kiln_batch_layout_placements_length_positive",
        ),
        CheckConstraint(
            "piece_width_cm_snapshot > 0",
            name="ck_kiln_batch_layout_placements_width_positive",
        ),
        CheckConstraint(
            "piece_height_cm_snapshot > 0",
            name="ck_kiln_batch_layout_placements_height_positive",
        ),
        CheckConstraint(
            "separation_cm_snapshot >= 0",
            name="ck_kiln_batch_layout_placements_separation_non_negative",
        ),
        CheckConstraint(
            "group_index >= 0",
            name="ck_kiln_batch_layout_placements_group_index_non_negative",
        ),
        CheckConstraint(
            "unit_index IS NULL OR unit_index >= 0",
            name="ck_kiln_batch_layout_placements_unit_index_non_negative",
        ),
    )

    layout: Mapped[KilnBatchLayout] = relationship("KilnBatchLayout", back_populates="placements")
    assignment: Mapped[KilnBatchAssignment] = relationship("KilnBatchAssignment")


# ---------------------------------------------------------------------------
# El trigger del volumen
# ---------------------------------------------------------------------------
#: Aplica a la hornada el DELTA de volumen activo de cada escritura sobre las
#: asignaciones. `vol_activo(fila)` es su volumen si esta ACTIVE y 0 si no, y 0
#: si la fila no existe (OLD en un INSERT, NEW en un DELETE).
#:
#: Vive TAMBIEN aqui, y no solo en la migracion 0040, porque las bases que se
#: crean desde los modelos —las de prueba— no ejecutan migraciones. Sin esto el
#: contador de capacidad no se moveria en ellas y las pruebas del servicio
#: pasarian contra una base que no garantiza nada. La migracion conserva su
#: propia copia, escrita a mano, y una prueba exige que las dos sean identicas.
KILN_BATCH_VOLUME_FUNCTION = """
CREATE FUNCTION kiln_batch_assignments_apply_volume() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    viejo numeric := 0;
    nuevo numeric := 0;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND OLD.status = 'ACTIVE' THEN
        viejo := OLD.assigned_volume_cm3;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND NEW.status = 'ACTIVE' THEN
        nuevo := NEW.assigned_volume_cm3;
    END IF;

    IF TG_OP = 'UPDATE' AND OLD.batch_id IS DISTINCT FROM NEW.batch_id THEN
        IF viejo <> 0 THEN
            UPDATE kiln_batches
               SET assigned_volume_cm3 = assigned_volume_cm3 - viejo,
                   exclusive = exclusive AND (assigned_volume_cm3 - viejo) > 0
             WHERE id = OLD.batch_id;
        END IF;
        IF nuevo <> 0 THEN
            UPDATE kiln_batches
               SET assigned_volume_cm3 = assigned_volume_cm3 + nuevo
             WHERE id = NEW.batch_id;
        END IF;
    ELSIF nuevo - viejo <> 0 THEN
        -- Una hornada que se queda VACIA deja de estar reservada: la exclusiva
        -- era de las piezas que ya no estan.
        UPDATE kiln_batches
           SET assigned_volume_cm3 = assigned_volume_cm3 + (nuevo - viejo),
               exclusive = exclusive AND (assigned_volume_cm3 + (nuevo - viejo)) > 0
         WHERE id = COALESCE(NEW.batch_id, OLD.batch_id);
    END IF;
    RETURN NULL;
END;
$$;
"""

KILN_BATCH_VOLUME_TRIGGER = """
CREATE TRIGGER trg_kiln_batch_assignments_volume
AFTER INSERT OR UPDATE OR DELETE ON kiln_batch_assignments
FOR EACH ROW EXECUTE FUNCTION kiln_batch_assignments_apply_volume();
"""

#: La guarda del contador: solo el trigger de las asignaciones lo mueve. Ver el
#: comentario gemelo en la migracion 0040.
KILN_BATCH_GUARD_FUNCTION = """
CREATE FUNCTION kiln_batches_guard_assigned_volume() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Profundidad 2 o mas: la escritura viene del trigger de las asignaciones,
    -- que es el UNICO que puede mover el contador.
    IF pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.assigned_volume_cm3 <> 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'Una hornada nace vacia: su volumen lo ponen sus asignaciones';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.assigned_volume_cm3 IS DISTINCT FROM OLD.assigned_volume_cm3 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'El volumen asignado de una hornada solo lo cambian sus asignaciones';
    END IF;
    RETURN NEW;
END;
$$;
"""

KILN_BATCH_GUARD_TRIGGER = """
CREATE TRIGGER trg_kiln_batches_guard_assigned_volume
BEFORE INSERT OR UPDATE ON kiln_batches
FOR EACH ROW EXECUTE FUNCTION kiln_batches_guard_assigned_volume();
"""

event.listen(KilnBatch.__table__, "after_create", DDL(KILN_BATCH_GUARD_FUNCTION))
event.listen(KilnBatch.__table__, "after_create", DDL(KILN_BATCH_GUARD_TRIGGER))
event.listen(
    KilnBatch.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS kiln_batches_guard_assigned_volume()"),
)
event.listen(KilnBatchAssignment.__table__, "after_create", DDL(KILN_BATCH_VOLUME_FUNCTION))
event.listen(KilnBatchAssignment.__table__, "after_create", DDL(KILN_BATCH_VOLUME_TRIGGER))
event.listen(
    KilnBatchAssignment.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS kiln_batch_assignments_apply_volume()"),
)


__all__ = [
    "ASSIGNMENT_RELEASE_COHERENT",
    "ASSIGNMENT_SOURCE_COHERENT",
    "IDEMPOTENCY_KEY_LENGTH",
    "KILN_BATCH_EDITABLE",
    "KILN_BATCH_GUARD_FUNCTION",
    "KILN_BATCH_GUARD_TRIGGER",
    "KILN_BATCH_STATUS_TIMESTAMPS",
    "KILN_BATCH_VOLUME_FUNCTION",
    "KILN_BATCH_VOLUME_TRIGGER",
    "InternalLoad",
    "InternalLoadLine",
    "KilnBatch",
    "KilnBatchAssignment",
    "KilnBatchAssignmentStatus",
    "KilnBatchLayout",
    "KilnBatchLayoutLevel",
    "KilnBatchLayoutPlacement",
    "KilnBatchOperation",
    "KilnBatchOperationKind",
    "KilnBatchSourceKind",
    "KilnBatchStatus",
]
