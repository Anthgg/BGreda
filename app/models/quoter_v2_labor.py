"""Mano de obra del Cotizador V2: trabajadores, tecnicas y asignaciones.

Fase 010D. Tres tablas, y la separacion entre ellas es la del negocio:

1. **`v2_workers`** — quien trabaja y cuanto cuesta su jornada. Un maestro, no
   una lista dentro de una cotizacion.
2. **`v2_techniques`** — que se sabe hacer y cuanto rinde una jornada de cada
   cosa. Rendimiento, no precio.
3. **`v2_quotation_labor`** — quien hace que en una cotizacion concreta, con
   todo lo que hizo falta para calcular su costo ya congelado.

## Por que el catalogo de tecnicas es propio y no el de Legacy

Porque significan cosas distintas. `techniques`, el del Cotizador historico,
guarda un **precio por tecnica** (`unit_price`) y unos factores de formula: el
costo de tornear sale de ahi. El de V2 guarda un **rendimiento estandar** y
ningun precio, porque en V2 el costo sale de quien lo hace y de cuanto tarda.
Reutilizar la tabla obligaria a que V2 conviviera con una columna de precio que
no usa y que Legacy si cobra, y ademas importar `app.models.quotations`, que es
justo lo que la prueba de aislamiento impide desde 010A.

## Por que la tarifa por hora NO es una columna

Es `jornal / jornada`, y la jornada puede venir de la configuracion global de
010B cuando el trabajador no declara una propia. Una columna generada no puede
leer otra tabla, y una columna corriente se desincronizaria en cuanto alguien
editara el jornal por otra via. Se deriva al leer —`app.core.quoter_v2_labor`—
y lo unico que se congela es el resultado dentro de la linea de la cotizacion,
que es donde tiene que dejar de moverse.

## Que NO hay aqui

No hay productividad aprendida. El rendimiento de una tecnica es un estandar
que alguien escribio; que un dia se hagan 70 piezas en vez de 50 no lo sube, y
que se hagan 40 no lo baja. Medir a las personas es otro producto.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import (
    calculation_numeric,
    money_numeric,
    quantity_numeric,
    unit_cost_numeric,
)
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType

if TYPE_CHECKING:
    from app.models.quoter_v2 import V2Quotation, V2QuotationProduct


class V2WorkerType(StrEnum):
    """De donde sale quien trabaja.

    Explicito y persistido, nunca deducido del nombre. Y la distincion no
    cambia si el tiempo se valoriza: un trabajador interno tiene sueldo y aun
    asi sus horas cuestan. Saber cuanto es la unica forma de descubrir que una
    linea de productos daba perdidas.
    """

    #: Del taller.
    INTERNAL = "INTERNAL"
    #: Contratado para el trabajo, o proveedor.
    EXTERNAL = "EXTERNAL"


class V2Worker(Base, TimestampMixin):
    """Quien trabaja, y cuanto cuesta su jornada."""

    __tablename__ = "v2_workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    worker_type: Mapped[V2WorkerType] = mapped_column(StrEnumType(V2WorkerType, 16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    #: Lo que cuesta un dia de esta persona. De aqui sale todo lo demas.
    daily_rate: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)

    #: Jornada propia. NULL no es cero: significa «la del taller», la que
    #: configura 010B. Tener la global copiada en cada fila permitiria que se
    #: separaran sin que nadie lo pidiera, y entonces habria dos jornadas.
    workday_hours: Mapped[Decimal | None] = mapped_column(quantity_numeric())

    notes: Mapped[str | None] = mapped_column(Text)

    #: Concurrencia optimista, como en 010B y 010C. La tarifa de una persona se
    #: edita desde un formulario que se manda entero: sin version, dos
    #: administradores se pisan sin conflicto ni rastro.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("daily_rate >= 0", name="daily_rate_non_negative"),
        # NULL se admite —es «la jornada del taller»—; lo que no cabe es una
        # jornada de cero horas, que seria una division por cero en cuanto
        # alguien calcule su tarifa.
        CheckConstraint(
            "workday_hours IS NULL OR (workday_hours > 0 AND workday_hours <= 24)",
            name="workday_hours_range",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("worker_type IN ('INTERNAL', 'EXTERNAL')", name="worker_type_allowed"),
        Index("ix_v2_workers_active", "active"),
    )


class V2Technique(Base, TimestampMixin):
    """Que se sabe hacer, y cuanto rinde una jornada de hacerlo.

    Sin precio, a proposito. El catalogo dice que el vidriado rinde 50 piezas
    por jornada; lo que cuesta vidriar 50 piezas depende de quien las vidrie.
    """

    __tablename__ = "v2_techniques"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    #: Lo que rinde UNA jornada de esta tecnica. Estandar configurado, no
    #: medicion: el sistema no aprende de la productividad de nadie.
    default_capacity_per_workday: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False
    )
    #: En que se mide ese rendimiento. Texto libre porque el taller mide en
    #: piezas, pero podria medir en metros o en kilos sin cambiar codigo.
    unit: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'piezas'"))

    #: Si esta tecnica solo tiene sentido sobre una pieza esmaltada.
    #:
    #: Es una marca del catalogo y no una lista de nombres en el codigo: asi
    #: 010D respeta la regla de 010C —si la pieza no lleva esmalte, la tecnica
    #: de vidriado nace apagada— sin que «vidriado» tenga que estar escrito en
    #: ningun sitio. Manana se anade «aspersion» y la regla la sigue sola.
    requires_glaze: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        # Cero rendimiento no es un dato pobre: es una division por cero en la
        # formula de horas, y un rendimiento negativo daria horas negativas.
        CheckConstraint("default_capacity_per_workday > 0", name="capacity_positive"),
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_v2_techniques_active", "active"),
    )


class V2QuotationLabor(Base, TimestampMixin):
    """Una tarea de una cotizacion: quien, que tecnica, cuanto y a que precio.

    Todo lo que hizo falta para el calculo queda congelado en la fila. Subir
    manana el jornal de una persona no puede cambiar un precio ya entregado, y
    dentro de un ano esta fila tiene que poder explicarse sola aunque el
    trabajador ya no este y la tecnica se haya retirado.
    """

    __tablename__ = "v2_quotation_labor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: La cotizacion a la que pertenece. Se guarda aunque la tarea cuelgue de un
    #: producto: la jornada compartida se mira por trabajador y cotizacion
    #: ENTERA, no por producto, o tres tareas de la misma persona en tres
    #: productos distintos pareceria que no comparten jornada.
    v2_quotation_id: Mapped[int] = mapped_column(
        ForeignKey("v2_quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: El producto, cuando la tarea es de un producto concreto. NULL es
    #: legitimo: el personal adicional puede apoyar al pedido entero.
    v2_quotation_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("v2_quotation_products.id", ondelete="CASCADE"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # ---- Quien, congelado -------------------------------------------------
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("v2_workers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    worker_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    worker_type_snapshot: Mapped[V2WorkerType] = mapped_column(
        StrEnumType(V2WorkerType, 16), nullable=False
    )
    daily_rate_snapshot: Mapped[Decimal] = mapped_column(money_numeric(), nullable=False)
    workday_hours_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)
    #: Derivada al congelar. Aqui SI se guarda, porque esta fila tiene que poder
    #: explicarse sin volver a consultar la jornada global de entonces.
    hourly_rate_snapshot: Mapped[Decimal] = mapped_column(unit_cost_numeric(), nullable=False)

    # ---- Que, congelado ---------------------------------------------------
    technique_id: Mapped[int] = mapped_column(
        ForeignKey("v2_techniques.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    technique_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    technique_unit_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    standard_capacity_snapshot: Mapped[Decimal] = mapped_column(quantity_numeric(), nullable=False)

    # ---- Cuanto -----------------------------------------------------------
    quantity: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Lo que sale del rendimiento estandar.
    calculated_hours: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Lo que se va a cobrar. Nace igual a lo calculado y puede ajustarse a mano
    #: para ESTA cotizacion: un encargo dificil lleva mas horas que el estandar
    #: y eso no cambia el estandar de manana.
    final_hours: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    hours_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    rate_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    #: Personal traido para este pedido. Suma costo y NO reduce el plazo: que
    #: dos personas tarden la mitad es una decision de quien planifica, no una
    #: consecuencia de contar cabezas.
    is_additional_personnel: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    labor_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    quotation: Mapped[V2Quotation] = relationship("V2Quotation", back_populates="labor")
    product: Mapped[V2QuotationProduct | None] = relationship("V2QuotationProduct")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("calculated_hours >= 0", name="calculated_hours_non_negative"),
        CheckConstraint("final_hours >= 0", name="final_hours_non_negative"),
        CheckConstraint("labor_cost >= 0", name="labor_cost_non_negative"),
        CheckConstraint("daily_rate_snapshot >= 0", name="daily_rate_non_negative"),
        CheckConstraint("hourly_rate_snapshot >= 0", name="hourly_rate_non_negative"),
        CheckConstraint(
            "workday_hours_snapshot > 0 AND workday_hours_snapshot <= 24",
            name="workday_hours_range",
        ),
        CheckConstraint("standard_capacity_snapshot > 0", name="capacity_positive"),
        Index("ix_v2_quotation_labor_quotation", "v2_quotation_id", "sort_order"),
        # La jornada compartida se calcula agrupando por trabajador dentro de
        # una cotizacion, y es la consulta que se hace en cada guardado.
        Index("ix_v2_quotation_labor_worker", "v2_quotation_id", "worker_id"),
    )
