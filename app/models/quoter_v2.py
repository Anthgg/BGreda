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
    from app.models.quoter_v2_labor import V2QuotationLabor

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
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

    # ---- Fase 010D: ilustracion -----------------------------------------
    #: Apagada por defecto. Encenderla es una decision explicita, igual que el
    #: esmalte en 010C.
    illustration_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    illustration_quantity: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Que hay que ilustrar. Texto libre a proposito: inventar categorias
    #: —basica, media, avanzada— seria decidir por el taller una clasificacion
    #: comercial que nadie ha pedido.
    illustration_notes: Mapped[str | None] = mapped_column(Text)

    #: Lo que valia ilustrar cuando se cotizo. Sin esto, subir el jornal de
    #: ilustracion manana reescribiria un precio ya entregado.
    illustration_daily_rate_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    illustration_workday_hours_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    illustration_capacity_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    illustration_hourly_rate_snapshot: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    illustration_hours: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    illustration_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Fase 010D: planificacion ----------------------------------------
    #: Cuantos dias de taller se van a usar. Es una DECISION, no un calculo:
    #: diez horas caben en un dia largo o en dos dias, y quien planifica elige.
    #: NULL significa que todavia no se ha decidido, no que sean cero.
    #:
    #: No confundir con la vigencia de la cotizacion, que es cuanto tiempo se
    #: respeta el precio. 010F cobrara el espacio por ESTOS dias.
    effective_work_days: Mapped[int | None] = mapped_column(Integer)

    # ---- Fase 010E: quema -------------------------------------------------
    #: El horno de ESTA cotizacion. Nace del sugerido por el tipo de produccion
    #: —chico para por menor, grande para por mayor— y se puede cambiar aqui
    #: sin tocar la configuracion global. RESTRICT: retirar un horno del taller
    #: no puede borrar el documento que explica un precio ya dado.
    #: Con indice: la clave foranea es RESTRICT, asi que borrar o dar de baja
    #: un horno obliga a PostgreSQL a comprobar quien lo referencia. Sin
    #: indice esa comprobacion recorre la tabla entera de cotizaciones y la
    #: bloquea mientras tanto.
    kiln_id: Mapped[int | None] = mapped_column(
        ForeignKey("kilns.id", ondelete="RESTRICT"), index=True
    )
    kiln_name_snapshot: Mapped[str | None] = mapped_column(String(120))
    #: La capacidad con la que se calculo, en cm3. Congelada: si manana se
    #: remide el horno, esta cotizacion sigue explicando sus hornadas con el
    #: numero que uso. Es ademas el unico dato de «tamano» que el maestro
    #: tiene: no existe un enum chico/grande, y deducirlo por el nombre
    #: pondria una tarifa de S/200 en el horno equivocado.
    kiln_capacity_snapshot: Mapped[Decimal | None] = mapped_column(quantity_numeric())

    firing_total_volume_cm3: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Puede pasar de 100 y eso no es un error: un 160 % son dos hornadas. Va
    #: en `quantity_numeric` y no en `percentage_numeric` porque aquel tope de
    #: 999,999999 lo alcanzaria una produccion grande en un horno chico.
    firing_occupancy_percent: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Hornadas que pide el volumen: techo de volumen/capacidad. No se
    #: prorratea ninguna: la segunda hornada al 60 % cuesta una entera.
    firing_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    low_fire_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    high_fire_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: Lo que de verdad cuesta encender, por hornada. COSTO, no precio. No
    #: depende del tipo de cliente: el gas vale lo mismo lo queme quien lo
    #: queme.
    gas_cost_low_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    gas_cost_high_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    #: Lo que se COBRA por hornada, ya resuelto segun el tipo de cliente. Se
    #: guarda el importe y no «externo o alumno» porque lo que tiene que dejar
    #: de moverse es el numero.
    commercial_rate_low_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())
    commercial_rate_high_snapshot: Mapped[Decimal | None] = mapped_column(money_numeric())

    #: Si cada uno de los cuatro importes lo decidio una persona dentro de esta
    #: cotizacion. El numero solo no lo distingue, y la diferencia importa: un
    #: acuerdo no se pierde porque alguien reenvie el mismo horno.
    gas_low_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    gas_high_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    commercial_low_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    commercial_high_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    firing_gas_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    firing_commercial_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Lo que deja la quema por si sola. GENERADA, al contrario que la tarifa
    #: por hora de 010D: aqui los dos sumandos viven en esta misma fila, asi
    #: que la base puede calcularla y nunca podra contradecirlos. No es el
    #: margen de la cotizacion: es la diferencia de UN componente.
    firing_difference: Mapped[Decimal] = mapped_column(
        calculation_numeric(),
        Computed("firing_commercial_total - firing_gas_total", persisted=True),
        nullable=False,
    )

    # ---- Fase 010F: el resultado economico -------------------------------
    #: Los totales por componente, congelados. Se guardan y no se derivan al
    #: leer porque una cotizacion emitida tiene que poder explicarse sin
    #: consultar un solo maestro: si manana sube el jornal, el precio que el
    #: cliente acepto sigue siendo reconstruible numero a numero.
    materials_cost_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    labor_cost_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: `dias efectivos x costo del espacio por dia`. Por dias EFECTIVOS de
    #: taller, nunca por dias de vigencia de la oferta: son dos plazos
    #: distintos y confundirlos cobraria espacio por no haber vendido.
    space_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Materiales + mano de obra + ilustracion. Lo que cuesta cada producto por
    #: si mismo, antes de repartir nada.
    direct_cost_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    #: Las dos bases, y la diferencia entre ellas es toda la fase. El COSTO
    #: REAL lleva el gas que de verdad se quema; el COSTO DE PRODUCCION lleva
    #: la tarifa que el taller cobra por encender. Intercambiarlos invierte el
    #: margen entero sin que ningun numero parezca raro.
    real_cost_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    production_cost_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    #: Las tres salidas comerciales. El suelo y el objetivo salen de los
    #: limites que ESTA cotizacion congelo, no de un 2 y un 3 escritos en el
    #: codigo: una emitida cuando el minimo era otro sigue explicandose sola.
    price_min: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    price_target: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    negotiated_price: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    #: Lo que va al documento, ya en la moneda de la cotizacion. El subtotal se
    #: RECONSTRUYE sumando las lineas redondeadas: tomar el precio global
    #: anterior al redondeo daria un total que no coincide con los unitarios
    #: que el cliente esta leyendo.
    subtotal_amount: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    tax_amount: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Lo que el redondeo anadio respecto al precio objetivo. Puede ser
    #: negativo si alguna linea no tiene piezas. Se expone porque, sin el,
    #: nadie sabe por que el subtotal no es exactamente costo x factor.
    rounding_adjustment: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    #: Precio comercial sin IGV menos costo REAL. El IGV no entra: no es
    #: ingreso del taller, es dinero que se recauda para otro. Puede ser
    #: negativo, y entonces hay que poder verlo.
    estimated_profit: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Sobre el PRECIO y no sobre el costo. Un 75 % sobre precio es un 300 %
    #: sobre costo, y confundirlos hace irreconocible la cifra.
    effective_margin_percent: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_by_name: Mapped[str | None] = mapped_column(String(200))

    customer: Mapped[Partner | None] = relationship("Partner", foreign_keys=[customer_id])
    #: Fase 010C. Las lineas de la cotizacion. `selectin` porque el costo
    #: de materiales de la cabecera se arma sumandolas: leerla sin ellas
    #: daria un total menor sin que nada avisara.
    products: Mapped[list[V2QuotationProduct]] = relationship(
        "V2QuotationProduct",
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by=lambda: (
            V2QuotationProduct.sort_order.asc(),
            V2QuotationProduct.id.asc(),
        ),
        lazy="selectin",
    )
    #: Fase 010D. Las tareas de mano de obra. `selectin` por el mismo motivo
    #: que los productos: el costo de la cabecera se arma sumandolas.
    labor: Mapped[list[V2QuotationLabor]] = relationship(
        "V2QuotationLabor",
        back_populates="quotation",
        cascade="all, delete-orphan",
        # En texto y no en `lambda`: la clase vive en otro modulo y solo se
        # importa para los tipos, asi que en tiempo de ejecucion el nombre no
        # existe aqui. SQLAlchemy lo resuelve contra su registro.
        order_by="(V2QuotationLabor.sort_order, V2QuotationLabor.id)",
        lazy="selectin",
    )

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
        # Fase 010D. Apagada es apagada: sin horas y sin costo. Mismo criterio
        # que el esmalte de 010C, y por el mismo motivo: «cantidad cero pero
        # costo distinto de cero» seria cobrar algo que alguien dijo que no.
        CheckConstraint(
            "illustration_enabled OR (illustration_hours = 0 AND illustration_cost = 0)",
            name="illustration_off_costs_nothing",
        ),
        CheckConstraint("illustration_quantity >= 0", name="illustration_quantity_non_negative"),
        CheckConstraint("illustration_hours >= 0", name="illustration_hours_non_negative"),
        CheckConstraint("illustration_cost >= 0", name="illustration_cost_non_negative"),
        CheckConstraint(
            "illustration_capacity_snapshot IS NULL OR illustration_capacity_snapshot > 0",
            name="illustration_capacity_positive",
        ),
        CheckConstraint(
            "illustration_workday_hours_snapshot IS NULL"
            " OR (illustration_workday_hours_snapshot > 0"
            "     AND illustration_workday_hours_snapshot <= 24)",
            name="illustration_workday_range",
        ),
        # Cero dias efectivos con trabajo asignado seria espacio gratis; NULL
        # es otra cosa —todavia no se decidio— y por eso se admite.
        CheckConstraint(
            "effective_work_days IS NULL OR effective_work_days >= 0",
            name="effective_work_days_non_negative",
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
        # ---- Fase 010E ----------------------------------------------------
        CheckConstraint(
            "kiln_capacity_snapshot IS NULL OR kiln_capacity_snapshot > 0",
            name="kiln_capacity_snapshot_positive",
        ),
        CheckConstraint("firing_total_volume_cm3 >= 0", name="firing_volume_non_negative"),
        CheckConstraint("firing_occupancy_percent >= 0", name="firing_occupancy_non_negative"),
        CheckConstraint("firing_count >= 0", name="firing_count_non_negative"),
        # Una quema no puede pedir mas hornadas de las que pide el volumen: el
        # numero de hornadas lo fija la carga, y baja y alta se hacen sobre la
        # MISMA carga. Un conteo mayor seria cobrar un encendido que nadie hizo.
        CheckConstraint(
            "low_fire_count >= 0 AND low_fire_count <= firing_count",
            name="low_fire_count_within_firing_count",
        ),
        CheckConstraint(
            "high_fire_count >= 0 AND high_fire_count <= firing_count",
            name="high_fire_count_within_firing_count",
        ),
        # Apagada es apagada, igual que el esmalte de 010C y la ilustracion de
        # 010D. `coalesce` porque una cotizacion de 010A tiene el campo en NULL
        # —nacio antes de que existiera la quema— y ahi el conteo es cero.
        CheckConstraint(
            "coalesce(low_fire_enabled, false) OR low_fire_count = 0",
            name="low_fire_off_costs_nothing",
        ),
        CheckConstraint(
            "coalesce(high_fire_enabled, false) OR high_fire_count = 0",
            name="high_fire_off_costs_nothing",
        ),
        # Sin horno no hay hornada ni importe. Sin este CHECK, borrar el horno
        # de un borrador dejaria unos totales huerfanos que nadie sabria a que
        # capacidad corresponden.
        CheckConstraint(
            "kiln_id IS NOT NULL"
            " OR (firing_count = 0 AND firing_occupancy_percent = 0"
            "     AND firing_gas_total = 0 AND firing_commercial_total = 0)",
            name="firing_requires_kiln",
        ),
        # Sin un solo encendido no hay importe. Cubre el caso que los dos CHECK
        # anteriores dejan pasar por separado: apagar AMBAS quemas deja los dos
        # conteos en cero y, sin esto, los totales podrian quedarse con el
        # importe de antes de apagarlas.
        CheckConstraint(
            "low_fire_count > 0 OR high_fire_count > 0"
            " OR (firing_gas_total = 0 AND firing_commercial_total = 0)",
            name="no_firing_costs_nothing",
        ),
        CheckConstraint(
            "gas_cost_low_snapshot IS NULL OR gas_cost_low_snapshot >= 0",
            name="gas_low_non_negative",
        ),
        CheckConstraint(
            "gas_cost_high_snapshot IS NULL OR gas_cost_high_snapshot >= 0",
            name="gas_high_non_negative",
        ),
        CheckConstraint(
            "commercial_rate_low_snapshot IS NULL OR commercial_rate_low_snapshot >= 0",
            name="commercial_low_non_negative",
        ),
        CheckConstraint(
            "commercial_rate_high_snapshot IS NULL OR commercial_rate_high_snapshot >= 0",
            name="commercial_high_non_negative",
        ),
        CheckConstraint("firing_gas_total >= 0", name="firing_gas_total_non_negative"),
        # ---- Fase 010F ----------------------------------------------------
        # Los costos y los precios no pueden ser negativos. La GANANCIA y el
        # ajuste por redondeo si: una cotizacion puede venderse a perdida, y
        # esconderlo tras un cero seria mentir sobre el unico numero que
        # importa mirar.
        CheckConstraint("materials_cost_total >= 0", name="materials_total_non_negative"),
        CheckConstraint("labor_cost_total >= 0", name="labor_total_non_negative"),
        CheckConstraint("space_cost >= 0", name="space_cost_non_negative"),
        CheckConstraint("direct_cost_total >= 0", name="direct_total_non_negative"),
        CheckConstraint("real_cost_total >= 0", name="real_cost_non_negative"),
        CheckConstraint("production_cost_total >= 0", name="production_cost_non_negative"),
        CheckConstraint("price_min >= 0", name="price_min_non_negative"),
        CheckConstraint("price_target >= 0", name="price_target_non_negative"),
        CheckConstraint("negotiated_price >= 0", name="negotiated_price_non_negative"),
        CheckConstraint("subtotal_amount >= 0", name="subtotal_non_negative"),
        CheckConstraint("tax_amount >= 0", name="tax_amount_non_negative"),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        # El suelo nunca puede pedir mas que el objetivo: si eso pasara, no
        # existiria ningun factor valido y la cotizacion quedaria sin precio
        # posible.
        CheckConstraint("price_min <= price_target", name="price_min_below_target"),
        CheckConstraint(
            "firing_commercial_total >= 0", name="firing_commercial_total_non_negative"
        ),
        Index("ix_v2_quotations_created_at", "created_at"),
    )


class V2QuotationProduct(Base, TimestampMixin):
    """Una linea de la cotizacion V2: que pieza, cuantas, y de que esta hecha.

    Fase 010C. Aqui vive el material —pasta y esmalte— con TODO lo que hizo
    falta para calcular su costo, copiado en el momento de escribirlo. La linea
    no consulta el maestro para explicarse: se explica sola.

    Esa copia es la regla de la fase. Si el costo por gramo se leyera del
    maestro al renderizar, subir el precio de la arcilla reescribiria el costo
    de todo lo cotizado antes —incluido lo ya enviado— y nadie podria decir con
    que numeros se acordo aquel precio.

    Lo que NO hace esta tabla: descontar existencia. Cotizar consulta el stock
    y puede avisar, pero no lo mueve. El consumo pertenece a produccion, y
    mezclarlos haria que pedir un presupuesto vaciara el almacen.
    """

    __tablename__ = "v2_quotation_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v2_quotation_id: Mapped[int] = mapped_column(
        ForeignKey("v2_quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    #: La pieza que se cotiza. RESTRICT: borrar un producto no puede borrar la
    #: linea que explica un precio ya dado.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    product_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # ---- Fase 010E: geometria y quema ------------------------------------
    #: Medidas de UNA pieza, en centimetros. Anulables porque un borrador a
    #: medio llenar es legitimo: se anade la linea, se elige la pasta y las
    #: medidas llegan despues. Sin ellas la linea no ocupa horno y avisa.
    #:
    #: Se copian del maestro cuando el producto las declara y la linea todavia
    #: no tiene las suyas, igual que el gramaje en 010C. Copiadas y no leidas:
    #: remedir una pieza en el catalogo no puede recalcular lo ya cotizado.
    length_cm: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    width_cm: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    height_cm: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    unit_volume_cm3: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    total_volume_cm3: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Que porcentaje del horno elegido ocupa ESTA linea. Es informacion, no un
    #: multiplicador: en V2 ocupar poco horno no encarece la pieza.
    firing_occupancy_percent: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Participacion en el volumen total de la cotizacion. Es la base con la
    #: que se reparte la quema, y se guarda para poder auditar el reparto.
    firing_volume_share_percent: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    #: Lo que a esta linea le toca de la quema. La suma de todas las lineas es
    #: exactamente el total de la cotizacion: el resto del redondeo se entrega,
    #: no se pierde.
    firing_commercial_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Y lo que le toca del gas REAL. Separado del anterior a proposito: son
    #: dos numeros distintos y mezclarlos borraria la diferencia de la quema.
    firing_gas_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Pasta -----------------------------------------------------------
    body_material_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    body_material_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    #: Lo que lleva UNA pieza, en la unidad base del material.
    body_unit_weight: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    body_uom_snapshot: Mapped[str | None] = mapped_column(String(32))
    #: El costo por unidad con el que se calculo. Puede venir del maestro o de
    #: una decision tomada dentro de esta cotizacion; `body_cost_is_override`
    #: dice cual de las dos, porque el numero solo no lo distingue.
    body_cost_per_unit_snapshot: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    body_cost_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    body_total_weight: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    body_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Esmalte ---------------------------------------------------------
    #: Apagado por defecto. Encenderlo es una decision, no un descuido.
    requires_glaze: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    glaze_material_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    glaze_material_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    #: Si el esmalte lo eligio el sistema —el activo mas caro por gramo— en vez
    #: de una persona. Importa mas alla de la trazabilidad: produccion usara
    #: otro esmalte y el precio NO cambiara por eso, asi que conviene que quede
    #: escrito que este era una referencia de costeo.
    glaze_is_reference: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    glaze_cost_per_unit_snapshot: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    glaze_cost_is_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: La proporcion vigente al cotizar. Congelada: si manana la casa decide
    #: estimar al 18 %, esta cotizacion sigue explicandose con el 15 % que uso.
    glaze_percent_snapshot: Mapped[Decimal | None] = mapped_column(percentage_numeric())
    #: Mililitros por gramo aplicados, y si fueron los de reserva. Un volumen
    #: calculado con 1:1 y otro con la concentracion real se parecen demasiado
    #: como para distinguirlos despues.
    glaze_ml_per_gram_snapshot: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    glaze_conversion_is_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    glaze_total_weight: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    glaze_volume_ml: Mapped[Decimal] = mapped_column(
        quantity_numeric(), nullable=False, server_default=text("0")
    )
    glaze_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    # ---- Fase 010F: el resultado economico de la linea -------------------
    #: Lo que cuesta ESTA pieza por si misma: materiales mas la mano de obra
    #: asignada a ella. Es la base con la que se le reparten los costos
    #: generales, asi que se guarda en vez de recalcularse al leer.
    direct_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Lo que absorbe de los costos que son de la cotizacion entera. El espacio
    #: se reparte por HORAS de trabajo y lo general —administracion,
    #: ilustracion y el personal que apoya al pedido sin producto asignado—
    #: por COSTO DIRECTO. La quema ya venia repartida por VOLUMEN desde 010E.
    #:
    #: Tres bases distintas y no una sola porque tres cosas distintas: el
    #: taller se ocupa por tiempo, el horno por sitio y la administracion
    #: acompana al dinero.
    allocated_space_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    allocated_general_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Las dos bases de la linea, con la misma diferencia que en la cabecera:
    #: una lleva la tarifa de quema y la otra el gas real.
    allocated_production_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    allocated_real_cost: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    #: `costo de produccion asignado x factor`, en moneda base.
    line_price: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Precio de una pieza, ya en la moneda de la cotizacion y todavia sin
    #: redondear. Se guarda junto al redondeado para poder explicar el salto.
    unit_price_raw: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: El que ve el cliente: multiplo del escalon comercial, hacia arriba.
    unit_price: Mapped[Decimal] = mapped_column(
        money_numeric(), nullable=False, server_default=text("0")
    )
    #: Reconstruidos desde el unitario redondeado, que es la unica forma de que
    #: el documento cuadre al sumarlo a mano.
    line_subtotal: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    line_tax: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    line_total: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )
    #: Subtotal de la linea llevado a moneda base, menos su costo real. Puede
    #: ser negativo y entonces hay que poder verlo.
    allocated_profit: Mapped[Decimal] = mapped_column(
        calculation_numeric(), nullable=False, server_default=text("0")
    )

    quotation: Mapped[V2Quotation] = relationship("V2Quotation", back_populates="products")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint(
            "body_unit_weight IS NULL OR body_unit_weight >= 0",
            name="body_unit_weight_non_negative",
        ),
        CheckConstraint(
            "body_cost_per_unit_snapshot IS NULL OR body_cost_per_unit_snapshot >= 0",
            name="body_cost_non_negative",
        ),
        CheckConstraint(
            "glaze_cost_per_unit_snapshot IS NULL OR glaze_cost_per_unit_snapshot >= 0",
            name="glaze_cost_non_negative",
        ),
        CheckConstraint("body_total_weight >= 0", name="body_total_weight_non_negative"),
        CheckConstraint("glaze_total_weight >= 0", name="glaze_total_weight_non_negative"),
        CheckConstraint("body_cost >= 0", name="body_cost_total_non_negative"),
        CheckConstraint("glaze_cost >= 0", name="glaze_cost_total_non_negative"),
        # Sin esmalte no hay nada de esmalte. El CHECK lo impone porque «peso
        # cero pero costo distinto de cero» seria cobrar algo que se apago.
        CheckConstraint(
            "requires_glaze OR (glaze_total_weight = 0 AND glaze_cost = 0 AND glaze_volume_ml = 0)",
            name="glaze_off_costs_nothing",
        ),
        CheckConstraint(
            "glaze_percent_snapshot IS NULL"
            " OR (glaze_percent_snapshot >= 0 AND glaze_percent_snapshot <= 100)",
            name="glaze_percent_range",
        ),
        CheckConstraint(
            "glaze_ml_per_gram_snapshot IS NULL OR glaze_ml_per_gram_snapshot > 0",
            name="glaze_ml_per_gram_positive",
        ),
        # ---- Fase 010E ----------------------------------------------------
        # Una medida en cero no es una pieza plana: es un dato sin poner. NULL
        # lo dice; un cero guardado lo esconderia detras de un volumen cero.
        CheckConstraint("length_cm IS NULL OR length_cm > 0", name="length_positive"),
        CheckConstraint("width_cm IS NULL OR width_cm > 0", name="width_positive"),
        CheckConstraint("height_cm IS NULL OR height_cm > 0", name="height_positive"),
        CheckConstraint("unit_volume_cm3 >= 0", name="unit_volume_non_negative"),
        CheckConstraint("total_volume_cm3 >= 0", name="total_volume_non_negative"),
        CheckConstraint("firing_occupancy_percent >= 0", name="line_firing_occupancy_non_negative"),
        CheckConstraint(
            "firing_volume_share_percent >= 0 AND firing_volume_share_percent <= 100",
            name="line_volume_share_range",
        ),
        CheckConstraint("firing_commercial_cost >= 0", name="line_firing_cost_non_negative"),
        CheckConstraint("firing_gas_cost >= 0", name="line_firing_gas_non_negative"),
        # ---- Fase 010F ----------------------------------------------------
        CheckConstraint("direct_cost >= 0", name="line_direct_cost_non_negative"),
        CheckConstraint("allocated_space_cost >= 0", name="line_space_non_negative"),
        CheckConstraint("allocated_general_cost >= 0", name="line_general_non_negative"),
        CheckConstraint("allocated_production_cost >= 0", name="line_production_cost_non_negative"),
        CheckConstraint("allocated_real_cost >= 0", name="line_real_cost_non_negative"),
        CheckConstraint("line_price >= 0", name="line_price_non_negative"),
        CheckConstraint("unit_price_raw >= 0", name="line_unit_raw_non_negative"),
        CheckConstraint("unit_price >= 0", name="line_unit_price_non_negative"),
        CheckConstraint("line_subtotal >= 0", name="line_subtotal_non_negative"),
        CheckConstraint("line_tax >= 0", name="line_tax_non_negative"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        # Sin piezas no hay importe. Una linea de cantidad cero puede existir
        # en un borrador a medias, pero no puede llevar un subtotal.
        CheckConstraint(
            "quantity > 0 OR (line_subtotal = 0 AND line_tax = 0 AND line_total = 0)",
            name="no_quantity_no_amount",
        ),
        Index("ix_v2_quotation_products_quotation", "v2_quotation_id", "sort_order"),
    )
