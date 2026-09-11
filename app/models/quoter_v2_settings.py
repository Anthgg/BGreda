"""Configuracion comercial del Cotizador V2.

Fase 010B. Aqui viven los DEFAULTS con los que nace una cotizacion V2. Lo que
una cotizacion ya emitida usa no se lee de aqui: se lee de su propio snapshot.
Son dos entidades distintas a proposito, y esa es la regla central de la fase:

- editar esta configuracion cambia las cotizaciones FUTURAS;
- editar una cotizacion no toca esta configuracion;
- una cotizacion emitida no cambia porque aqui se mueva un numero.

## Que NO esta aqui, y por que

`tax_percent`, `currency_code`, `currency_symbol` y `rounding_step` siguen
viviendo en `commercial_settings`. Son la politica fiscal y monetaria de la
casa: hay un unico IGV verdadero y un unico simbolo de moneda, los cobre quien
los cobre. Duplicarlos daria dos sitios donde mirar y uno quedaria
desactualizado —y el que quedara mal emitiria facturas incorrectas—.

V2 los CONSUME de alli. Que los consuma no lo acopla al motor Legacy: no hay
ninguna formula de por medio, solo un valor que el negocio declara una vez.

## Que SI esta aqui, y por que

La vigencia, en cambio, es propia. `commercial_settings.quote_validity_days` es
la de Legacy y se queda como esta: durante la transicion los dos motores pueden
querer plazos distintos, y sobre todo —la regla de 010A— tocar la configuracion
de V2 no puede cambiar lo que hace una cotizacion Legacy.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.precision import money_numeric, quantity_numeric
from app.core.quoter_v2_config import (
    DEFAULT_ADMINISTRATIVE_COST_PER_QUOTE,
    DEFAULT_COMMERCIAL_FACTOR,
    DEFAULT_COMMERCIAL_FACTOR_MAX,
    DEFAULT_COMMERCIAL_FACTOR_MIN,
    DEFAULT_EXCHANGE_RATE,
    DEFAULT_ILLUSTRATION_DAILY_RATE,
    DEFAULT_ILLUSTRATION_PIECES_PER_WORKDAY,
    DEFAULT_QUOTATION_VALIDITY_DAYS,
    DEFAULT_SPACE_SERVICE_COST_PER_DAY,
    DEFAULT_WORKDAY_HOURS,
)
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType
from app.models.firings import FiringType, Kiln
from app.models.quoter_v2 import V2CustomerKind, V2ProductionType
from app.models.settings import MAX_VALIDITY_DAYS, SINGLETON_ID, VersionedSingletonMixin


class V2CommercialSettings(Base, VersionedSingletonMixin):
    """Fila unica con los defaults comerciales del Cotizador V2."""

    __tablename__ = "v2_commercial_settings"

    # ---- Jornada y costos generales -------------------------------------
    workday_hours: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text(str(DEFAULT_WORKDAY_HOURS))
    )
    space_service_cost_per_day: Mapped[Decimal] = mapped_column(
        money_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_SPACE_SERVICE_COST_PER_DAY)),
    )
    administrative_cost_per_quote: Mapped[Decimal] = mapped_column(
        money_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_ADMINISTRATIVE_COST_PER_QUOTE)),
    )

    # ---- Factor comercial -----------------------------------------------
    #: Factor, no porcentaje: 3 es x3. Global por cotizacion, nunca por
    #: producto; el reparto entre productos es proporcional al costo y se
    #: implementa en 010F.
    commercial_factor_default: Mapped[Decimal] = mapped_column(
        quantity_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_COMMERCIAL_FACTOR)),
    )
    commercial_factor_min: Mapped[Decimal] = mapped_column(
        quantity_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_COMMERCIAL_FACTOR_MIN)),
    )
    commercial_factor_max: Mapped[Decimal] = mapped_column(
        quantity_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_COMMERCIAL_FACTOR_MAX)),
    )

    # ---- Vigencia --------------------------------------------------------
    quotation_validity_days: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text(str(DEFAULT_QUOTATION_VALIDITY_DAYS)),
    )

    # ---- Moneda ----------------------------------------------------------
    #: Tipo de cambio PEN/USD de referencia para una cotizacion nueva en USD.
    #: MANUAL, como en Legacy: el proyecto no tiene proveedor automatico de FX
    #: y esta fase no inventa uno. La cotizacion se lleva su copia.
    default_exchange_rate: Mapped[Decimal] = mapped_column(
        quantity_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_EXCHANGE_RATE)),
    )

    # ---- Defaults de produccion -----------------------------------------
    #: Con que tipo de produccion nace una cotizacion. La persona puede
    #: cambiarlo; el sistema NUNCA lo cambia solo, ni siquiera si la cantidad
    #: parece de por mayor.
    default_production_type: Mapped[V2ProductionType] = mapped_column(
        StrEnumType(V2ProductionType, 16), nullable=False, server_default=text("'RETAIL'")
    )
    default_customer_kind: Mapped[V2CustomerKind] = mapped_column(
        StrEnumType(V2CustomerKind, 16), nullable=False, server_default=text("'EXTERNAL'")
    )

    #: Horno sugerido para cada tipo de produccion. Sugerido: dentro de la
    #: cotizacion se puede cambiar sin tocar esta configuracion. Anulables
    #: porque una instalacion recien creada no tiene hornos todavia, y obligar
    #: a elegir uno impediria guardar el resto de la configuracion.
    retail_kiln_id: Mapped[int | None] = mapped_column(ForeignKey("kilns.id", ondelete="RESTRICT"))
    wholesale_kiln_id: Mapped[int | None] = mapped_column(
        ForeignKey("kilns.id", ondelete="RESTRICT")
    )

    # ---- Quema -----------------------------------------------------------
    #: Las dos activas por defecto, y las dos apagables por cotizacion. Debe
    #: poder existir solo baja, solo alta o ambas: no hay regla rigida.
    low_fire_enabled_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    high_fire_enabled_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    # ---- Ilustracion -----------------------------------------------------
    #: Jornal y rendimiento. La tarifa por hora NO se guarda: se deriva de
    #: `illustration_daily_rate / workday_hours`. Guardar las dos permitiria
    #: que se contradijeran, y entonces habria que decidir cual manda.
    illustration_daily_rate: Mapped[Decimal] = mapped_column(
        money_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_ILLUSTRATION_DAILY_RATE)),
    )
    illustration_pieces_per_workday: Mapped[Decimal] = mapped_column(
        quantity_numeric(),
        nullable=False,
        server_default=text(str(DEFAULT_ILLUSTRATION_PIECES_PER_WORKDAY)),
    )

    retail_kiln: Mapped[Kiln | None] = relationship("Kiln", foreign_keys=[retail_kiln_id])
    wholesale_kiln: Mapped[Kiln | None] = relationship("Kiln", foreign_keys=[wholesale_kiln_id])

    __table_args__ = (
        CheckConstraint(f"id = {SINGLETON_ID}", name="singleton"),
        CheckConstraint("version > 0", name="version_positive"),
        # Una jornada de cero horas haria una division por cero en cuanto
        # alguien calcule una tarifa horaria.
        CheckConstraint("workday_hours > 0 AND workday_hours <= 24", name="workday_hours_range"),
        CheckConstraint("space_service_cost_per_day >= 0", name="space_cost_non_negative"),
        CheckConstraint("administrative_cost_per_quote >= 0", name="admin_cost_non_negative"),
        # El suelo de x2 es una regla de negocio cerrada, no una preferencia.
        CheckConstraint("commercial_factor_min >= 2", name="factor_min_floor"),
        CheckConstraint("commercial_factor_max >= commercial_factor_min", name="factor_range"),
        CheckConstraint(
            "commercial_factor_default >= commercial_factor_min"
            " AND commercial_factor_default <= commercial_factor_max",
            name="factor_default_within_range",
        ),
        CheckConstraint(
            f"quotation_validity_days > 0 AND quotation_validity_days <= {MAX_VALIDITY_DAYS}",
            name="validity_range",
        ),
        CheckConstraint("default_exchange_rate > 0", name="exchange_rate_positive"),
        CheckConstraint(
            "default_production_type IN ('RETAIL', 'WHOLESALE')",
            name="production_type_allowed",
        ),
        CheckConstraint(
            "default_customer_kind IN ('EXTERNAL', 'STUDENT')",
            name="customer_kind_allowed",
        ),
        CheckConstraint("illustration_daily_rate >= 0", name="illustration_rate_non_negative"),
        CheckConstraint(
            "illustration_pieces_per_workday > 0", name="illustration_capacity_positive"
        ),
    )


class V2KilnRate(Base, TimestampMixin):
    """Costo real de gas y tarifas comerciales de un horno, para el motor V2.

    **Tabla propia, no una fila mas en `kiln_rates`.** El costeo de quema
    Legacy resuelve la tarifa vigente tomando la PRIMERA fila que encuentra
    para cada `(kiln_id, firing_type)`, sin mas discriminante. Anadir aqui las
    tarifas de alumno o el costo del gas la haria elegir una de ellas y cobrar
    una quema Legacy con un numero que no le corresponde, en silencio.

    Tres numeros por horno y tipo de quema, que son tres conceptos distintos:

    - `gas_cost`: lo que de verdad cuesta encender. Es COSTO, no precio.
    - `external_rate` y `student_rate`: lo que se COBRA, segun a quien.

    La diferencia entre lo que se cobra y lo que cuesta el gas es la ganancia
    propia de la quema, y solo se puede calcular si los tres viven separados.

    Sin vigencias `valid_from`/`valid_to` como en Legacy: la historia de V2 no
    se reconstruye desde el maestro sino desde el snapshot de cada cotizacion,
    que es donde de verdad importa que el numero no cambie.
    """

    __tablename__ = "v2_kiln_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kiln_id: Mapped[int] = mapped_column(
        # Sin `index=True`: el UNIQUE de abajo empieza por `kiln_id` y ya
        # sirve para buscar por horno. Un indice suelto seria almacenamiento
        # y coste de escritura a cambio de nada.
        ForeignKey("kilns.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: LOW o HIGH. Se reutiliza el enum del dominio de quemas porque describe
    #: una propiedad fisica del horno —se enciende en baja o en alta—, no una
    #: decision del motor Legacy.
    firing_type: Mapped[FiringType] = mapped_column(StrEnumType(FiringType, 16), nullable=False)

    gas_cost: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    external_rate: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    student_rate: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )

    kiln: Mapped[Kiln] = relationship("Kiln")

    __table_args__ = (
        UniqueConstraint("kiln_id", "firing_type", name="uq_v2_kiln_rates_kiln_id_firing_type"),
        CheckConstraint("gas_cost >= 0", name="gas_cost_non_negative"),
        CheckConstraint("external_rate >= 0", name="external_rate_non_negative"),
        CheckConstraint("student_rate >= 0", name="student_rate_non_negative"),
    )
