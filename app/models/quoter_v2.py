"""Cotizador V2: la cabecera del documento y nada mas.

Fase 010A. Esto es el esqueleto, no el motor. Aqui hay identidad —codigo,
estado, cliente, tipo de produccion— y la marca de motor que hace imposible
confundir este documento con uno del Cotizador historico. No hay una sola
cifra de costo, ni un snapshot economico, ni un factor comercial: eso llega en
010B y siguientes, y adelantarlo ahora significaria inventar un precio que
nadie aprobo.

**Tabla nueva, no una columna mas en `quotations`.** La cabecera Legacy lleva
mas de noventa columnas de costeo con formulas embebidas y snapshots atados al
motor viejo; colgar V2 de ella obligaria a tocar Legacy en cada fase de la
familia 010 y a compartir CHECKs que significan cosas distintas en cada motor.
Lo que si se comparte son los maestros de verdad —`partners` hoy, productos y
materiales despues—, porque un cliente es el mismo cliente cotice quien cotice.

El CHECK de `pricing_engine_version` no es decorativo: es lo que impide que un
INSERT a mano meta aqui una cotizacion Legacy. El CHECK espejo vive en
`quotations` y hace lo contrario.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.masters import Partner

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

from app.core.precision import money_numeric, percentage_numeric, quantity_numeric
from app.core.pricing_engine import PRICING_ENGINE_VERSION_LENGTH, PricingEngineVersion
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType


class V2QuotationStatus(StrEnum):
    """Estado comercial de una cotizacion V2.

    Enum **propio**, no el de Legacy, aunque los tres valores coincidan hoy.
    Compartirlo ataria los dos motores: el dia que V2 anada `EXPIRED` —la
    vigencia es 010H— el cambio no puede aparecer de rebote en documentos
    historicos que nunca supieron vencer.
    """

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class V2ProductionType(StrEnum):
    """Por menor o por mayor. Lo elige la persona, nunca el sistema.

    Es la primera decision de una cotizacion V2 porque de ella cuelga el horno
    sugerido —chico para menor, grande para mayor—. Sugerido: en 010E el horno
    sera editable dentro de la cotizacion y superar la capacidad avisara y
    recomendara, pero jamas cambiara solo el tipo de produccion.

    En 010A el valor se guarda y se devuelve. No entra en ningun calculo,
    porque todavia no hay calculo.
    """

    RETAIL = "RETAIL"
    WHOLESALE = "WHOLESALE"


class V2CustomerKind(StrEnum):
    """A quien se le cotiza, a efectos de TARIFA de horno.

    Fase 010B. Un cliente externo y un alumno pagan la misma quema a precios
    distintos, asi que el horno necesita saber a quien tiene delante.

    No se deduce del nombre del tercero ni de ninguna heuristica de texto: un
    tercero no pasa a ser alumno porque su nombre contenga la palabra
    «taller». Es una eleccion explicita y persistida. Las tarifas de cada uno
    viven en `v2_kiln_rates`, por horno.
    """

    EXTERNAL = "EXTERNAL"
    STUDENT = "STUDENT"


#: Tipo de produccion con el que nace una cotizacion V2 si nadie dice otra cosa.
DEFAULT_V2_PRODUCTION_TYPE = V2ProductionType.RETAIL


class V2Quotation(Base, TimestampMixin):
    """Cabecera de una cotizacion del Cotizador V2."""

    __tablename__ = "v2_quotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: `CTZ-V2-2026-000001`. Talonario propio: agotar cotizaciones Legacy no
    #: puede mover el contador de V2, y un codigo historico nunca podra
    #: confundirse con uno nuevo porque el prefijo no coincide.
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: Siempre 'V2'. El CHECK de la tabla no admite otra cosa.
    pricing_engine_version: Mapped[PricingEngineVersion] = mapped_column(
        StrEnumType(PricingEngineVersion, PRICING_ENGINE_VERSION_LENGTH),
        nullable=False,
        default=PricingEngineVersion.V2,
        server_default=text("'V2'"),
    )

    status: Mapped[V2QuotationStatus] = mapped_column(
        StrEnumType(V2QuotationStatus, 16),
        nullable=False,
        default=V2QuotationStatus.DRAFT,
        server_default=text("'DRAFT'"),
        index=True,
    )

    production_type: Mapped[V2ProductionType] = mapped_column(
        StrEnumType(V2ProductionType, 16),
        nullable=False,
        default=DEFAULT_V2_PRODUCTION_TYPE,
        server_default=text("'RETAIL'"),
    )

    #: Maestro compartido de verdad: un cliente es el mismo cliente cotice
    #: quien cotice. RESTRICT y no CASCADE: borrar un tercero no puede
    #: llevarse por delante un documento comercial.
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("partners.id", ondelete="RESTRICT"), index=True
    )
    #: El nombre con el que se emitio. Que el maestro cambie despues no
    #: reescribe lo que el cliente recibio.
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200))

    name: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)

    # ------------------------------------------------------------------
    # Fase 010B. Snapshot de la configuracion comercial.
    # ------------------------------------------------------------------
    #: Copia, no referencia. Es la mitad del principio de 010B: la
    #: configuracion global define con que nace una cotizacion, y a partir de
    #: ese instante la cotizacion vive de SU copia.
    #:
    #: Si fuera una FK a `v2_commercial_settings`, subir el costo del taller de
    #: 140 a 160 reescribiria el precio de todo lo cotizado el mes pasado —
    #: incluido lo que ya se envio al cliente—. Y al reves: corregir un numero
    #: dentro de una cotizacion cambiaria el default de la casa.
    #:
    #: Son columnas y no un JSON: un JSON opaco no admite CHECK, no se puede
    #: consultar sin desempaquetar y deja que un dia falte una clave sin que
    #: nada avise. Estan las que ya tienen dueno; las de materiales, mano de
    #: obra y quema llegan con su fase, y por eso no se adelantan aqui vacias.
    #:
    #: Anulables porque las cotizaciones creadas en 010A nacieron antes de que
    #: existiera la configuracion: NULL significa «esta es anterior al
    #: snapshot», no «vale cero».
    tax_percent_snapshot: Mapped[Decimal | None] = mapped_column(percentage_numeric())
    currency_code_snapshot: Mapped[str | None] = mapped_column(String(3))
    currency_symbol_snapshot: Mapped[str | None] = mapped_column(String(8))
    #: Solo cuando la moneda no es la base. En PEN no hay nada que convertir, y
    #: un 1 guardado ahi seria un tipo de cambio inventado.
    exchange_rate_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    validity_days_snapshot: Mapped[int | None] = mapped_column(Integer)
    workday_hours_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    space_service_cost_per_day_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    administrative_cost_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    #: El paso del redondeo comercial vigente al crear. Va con el grupo de
    #: moneda e impuesto, no con el de mano de obra: decide el precio FINAL
    #: de cualquier cotizacion, la calcule el motor que la calcule. Sin el,
    #: cambiar el redondeo de 0,50 a 1,00 haria irreproducible el precio de
    #: algo ya emitido. Es el mismo criterio que sigue `prototype_quotations`.
    rounding_step_snapshot: Mapped[Decimal | None] = mapped_column(percentage_numeric())

    #: El factor elegido para ESTA cotizacion, y los limites que regian al
    #: crearla. Los limites viajan con la cotizacion porque autorizan lo que
    #: contiene: si manana el minimo sube a x2.5, una cotizacion emitida a x2.2
    #: sigue siendo valida —se emitio cuando x2 estaba permitido— y debe poder
    #: explicarse sin consultar una configuracion que ya cambio.
    commercial_factor: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    commercial_factor_min_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    commercial_factor_max_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())

    #: A quien se cotiza, a efectos de tarifa de horno. Explicito y persistido:
    #: jamas se deduce del nombre del cliente.
    customer_kind: Mapped[V2CustomerKind | None] = mapped_column(StrEnumType(V2CustomerKind, 16))

    #: Que quemas entran. Cualquiera de las dos puede apagarse, y debe poder
    #: existir solo baja o solo alta.
    low_fire_enabled: Mapped[bool | None] = mapped_column(Boolean)
    high_fire_enabled: Mapped[bool | None] = mapped_column(Boolean)

    #: Que version de la configuracion se copio y cuando. Sin esto, dos
    #: cotizaciones con numeros distintos son indistinguibles de un error de
    #: captura; con esto se sabe que la configuracion cambio en medio.
    settings_version_snapshot: Mapped[int | None] = mapped_column(Integer)
    settings_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(200))

    customer: Mapped[Partner | None] = relationship("Partner", foreign_keys=[customer_id])

    __table_args__ = (
        CheckConstraint(
            f"pricing_engine_version = '{PricingEngineVersion.V2}'",
            name="engine_is_v2",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED', 'CANCELLED')",
            name="status_allowed",
        ),
        CheckConstraint(
            "production_type IN ('RETAIL', 'WHOLESALE')",
            name="production_type_allowed",
        ),
        # Fase 010B. Los snapshots admiten NULL —una cotizacion de 010A nacio
        # sin ellos— pero, si hay valor, tiene que ser un valor posible. Un
        # IGV negativo o un tipo de cambio en cero no son «datos historicos»:
        # son datos rotos que mas adelante producirian un precio roto.
        CheckConstraint(
            "tax_percent_snapshot IS NULL"
            " OR (tax_percent_snapshot >= 0 AND tax_percent_snapshot <= 100)",
            name="tax_percent_snapshot_range",
        ),
        CheckConstraint(
            "exchange_rate_snapshot IS NULL OR exchange_rate_snapshot > 0",
            name="exchange_rate_snapshot_positive",
        ),
        # Moneda y tipo de cambio solo tienen tres combinaciones validas, y las
        # tres se enumeran en vez de prohibir una sola. Las que quedan fuera no
        # son casos raros: son cotizaciones rotas.
        #
        # - sin moneda no puede haber tipo de cambio: seria una tasa huerfana,
        #   sin decir de que a que;
        # - en moneda base no hay nada que convertir, y un 1 ahi es una cifra
        #   inventada que alguien acabaria multiplicando;
        # - en moneda extranjera el tipo de cambio es OBLIGATORIO. Sin el, la
        #   cotizacion nace sin poder convertirse y el motor tendria que leer
        #   el de hoy, que es justo lo que esta fase impide.
        #
        # `upper()` porque la comparacion en SQL distingue mayusculas: sin el,
        # un 'pen' minusculo se colaria como moneda extranjera.
        CheckConstraint(
            "(currency_code_snapshot IS NULL AND exchange_rate_snapshot IS NULL)"
            " OR (upper(currency_code_snapshot) = 'PEN'"
            "     AND exchange_rate_snapshot IS NULL)"
            " OR (upper(currency_code_snapshot) <> 'PEN'"
            "     AND exchange_rate_snapshot IS NOT NULL)",
            name="currency_and_exchange_rate_coherent",
        ),
        CheckConstraint(
            "validity_days_snapshot IS NULL OR validity_days_snapshot > 0",
            name="validity_days_snapshot_positive",
        ),
        # Con tope: un dia tiene 24 horas. Sin el, un 80 tecleado por un 8,0
        # pasa y divide el jornal entre diez.
        CheckConstraint(
            "workday_hours_snapshot IS NULL"
            " OR (workday_hours_snapshot > 0 AND workday_hours_snapshot <= 24)",
            name="workday_hours_snapshot_range",
        ),
        CheckConstraint(
            "rounding_step_snapshot IS NULL OR rounding_step_snapshot > 0",
            name="rounding_step_snapshot_positive",
        ),
        CheckConstraint(
            "settings_version_snapshot IS NULL OR settings_version_snapshot > 0",
            name="settings_version_snapshot_positive",
        ),
        CheckConstraint(
            "space_service_cost_per_day_snapshot IS NULL"
            " OR space_service_cost_per_day_snapshot >= 0",
            name="space_cost_snapshot_non_negative",
        ),
        CheckConstraint(
            "administrative_cost_snapshot IS NULL OR administrative_cost_snapshot >= 0",
            name="admin_cost_snapshot_non_negative",
        ),
        # El suelo de x2 es regla cerrada: ninguna cotizacion puede guardar un
        # factor por debajo del minimo que ella misma copio.
        CheckConstraint(
            "commercial_factor IS NULL OR commercial_factor >= 2",
            name="commercial_factor_floor",
        ),
        # El rango congelado tambien tiene que ser un rango posible. Un minimo
        # de 1,5 contradiria la regla que el propio documento dice respetar, y
        # un minimo mayor que el maximo dejaria la cotizacion en un estado en
        # el que NINGUN factor es valido.
        CheckConstraint(
            "commercial_factor_min_snapshot IS NULL OR commercial_factor_min_snapshot >= 2",
            name="commercial_factor_min_snapshot_floor",
        ),
        CheckConstraint(
            "commercial_factor_min_snapshot IS NULL"
            " OR commercial_factor_max_snapshot IS NULL"
            " OR commercial_factor_min_snapshot <= commercial_factor_max_snapshot",
            name="commercial_factor_snapshot_range_ordered",
        ),
        CheckConstraint(
            "commercial_factor IS NULL OR commercial_factor_min_snapshot IS NULL"
            " OR commercial_factor >= commercial_factor_min_snapshot",
            name="commercial_factor_within_min",
        ),
        CheckConstraint(
            "commercial_factor IS NULL OR commercial_factor_max_snapshot IS NULL"
            " OR commercial_factor <= commercial_factor_max_snapshot",
            name="commercial_factor_within_max",
        ),
        CheckConstraint(
            "customer_kind IS NULL OR customer_kind IN ('EXTERNAL', 'STUDENT')",
            name="customer_kind_allowed",
        ),
        Index("ix_v2_quotations_created_at", "created_at"),
    )
