"""Contrato publico de la configuracion comercial del Cotizador V2.

La lectura devuelve la configuracion EFECTIVA: lo propio de V2 mas lo que sigue
siendo politica canonica de la casa —IGV, moneda, simbolo—, marcado con su
origen para que la pantalla pueda decir donde se edita cada cosa. La escritura,
en cambio, solo admite lo propio de V2: el IGV se edita donde siempre.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.firings import FiringType
from app.models.quoter_v2 import V2CustomerKind, V2ProductionType

#: Tope alto y deliberadamente holgado. Existe para frenar un error de captura
#: —un cero de mas— no para opinar sobre cuanto puede cobrar el taller.
MAX_MONEY = Decimal("1000000")
#: Un factor comercial por encima de x100 es un error de tecleo, no un margen.
MAX_FACTOR = Decimal("100")


class V2KilnRateIn(BaseModel):
    """Los tres numeros de un horno para un tipo de quema."""

    model_config = ConfigDict(extra="forbid")

    #: Lo que cuesta encender. COSTO, no precio.
    gas_cost: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    #: Lo que se cobra a un cliente externo.
    external_rate: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    #: Lo que se cobra a un alumno.
    student_rate: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)

    @model_validator(mode="after")
    def _al_menos_uno(self) -> V2KilnRateIn:
        """Los tres son opcionales, pero los tres a la vez no.

        Se admite el envio parcial —corregir solo el gas es legitimo— y por eso
        ninguno es obligatorio. Un cuerpo vacio, en cambio, crearia una fila de
        ceros que nadie pidio y que luego parece una tarifa configurada.
        """
        if self.gas_cost is None and self.external_rate is None and self.student_rate is None:
            raise ValueError("Indique al menos uno de los tres importes")
        return self


class V2KilnRateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kiln_id: int
    kiln_code: str
    kiln_name: str
    firing_type: FiringType
    gas_cost: Decimal
    external_rate: Decimal
    student_rate: Decimal
    #: Si alguien la puso. Un cero sin configurar no es un cero elegido, y la
    #: pantalla tiene que poder distinguirlos.
    configured: bool


class V2SettingsUpdateIn(BaseModel):
    """Solo lo que pertenece a V2.

    El IGV, la moneda y el simbolo NO estan aqui a proposito: se editan en la
    configuracion comercial de la empresa, que es su unica fuente. Aceptarlos
    tambien por esta puerta crearia dos sitios donde cambiarlos y uno quedaria
    desactualizado.
    """

    model_config = ConfigDict(extra="forbid")

    #: La version que el cliente leyo. Si no coincide, la escritura se rechaza
    #: en vez de pisar en silencio un cambio mas reciente de otra persona.
    expected_version: int = Field(ge=1)

    workday_hours: Decimal | None = Field(default=None, gt=0, le=24)
    space_service_cost_per_day: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    administrative_cost_per_quote: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)

    #: El suelo de x2 es regla de negocio cerrada, no una preferencia.
    commercial_factor_min: Decimal | None = Field(default=None, ge=2, le=MAX_FACTOR)
    commercial_factor_default: Decimal | None = Field(default=None, ge=2, le=MAX_FACTOR)
    commercial_factor_max: Decimal | None = Field(default=None, ge=2, le=MAX_FACTOR)

    quotation_validity_days: int | None = Field(default=None, gt=0, le=3650)
    default_exchange_rate: Decimal | None = Field(default=None, gt=0, le=MAX_MONEY)

    default_production_type: V2ProductionType | None = None
    default_customer_kind: V2CustomerKind | None = None
    retail_kiln_id: int | None = Field(default=None, ge=1)
    wholesale_kiln_id: int | None = Field(default=None, ge=1)

    low_fire_enabled_default: bool | None = None
    high_fire_enabled_default: bool | None = None

    illustration_daily_rate: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY)
    illustration_pieces_per_workday: Decimal | None = Field(default=None, gt=0, le=MAX_MONEY)


class V2SettingsOut(BaseModel):
    """La configuracion efectiva, con el origen de cada grupo."""

    model_config = ConfigDict(from_attributes=True)

    version: int
    updated_at: datetime

    # ---- Propio de V2 ----------------------------------------------------
    workday_hours: Decimal
    space_service_cost_per_day: Decimal
    administrative_cost_per_quote: Decimal
    commercial_factor_default: Decimal
    commercial_factor_min: Decimal
    commercial_factor_max: Decimal
    quotation_validity_days: int
    default_exchange_rate: Decimal
    default_production_type: V2ProductionType
    default_customer_kind: V2CustomerKind
    retail_kiln_id: int | None
    wholesale_kiln_id: int | None
    low_fire_enabled_default: bool
    high_fire_enabled_default: bool
    illustration_daily_rate: Decimal
    illustration_pieces_per_workday: Decimal

    #: Derivados, no almacenados: guardarlos permitiria que contradijeran a sus
    #: fuentes y habria que decidir cual manda.
    illustration_hourly_rate: Decimal
    illustration_pieces_per_hour: Decimal

    # ---- Canonico de la empresa, solo lectura desde aqui ------------------
    #: Se muestran para que la pantalla de V2 no obligue a ir a buscarlos, pero
    #: se editan en Configuracion comercial: son la unica fuente del proyecto.
    tax_percent: Decimal | None
    currency_code: str | None
    currency_symbol: str | None
    #: Donde se editan los tres de arriba. La pantalla lo usa para enlazar en
    #: vez de ofrecer un campo que no guardaria nada.
    canonical_source: str = "commercial_settings"


class V2SettingsPage(BaseModel):
    """Configuracion y tarifas de horno en una sola lectura."""

    settings: V2SettingsOut
    #: Los hornos activos por baja y por alta, tengan tarifa o no. La rejilla
    #: viaja COMPLETA: las tarifas nacen vacias y una lista con solo lo ya
    #: guardado no dejaria por donde crear la primera.
    kiln_rates: list[V2KilnRateOut]
    #: Los valores aprobados para un horno chico y uno grande.
    #:
    #: Se ofrecen como REFERENCIA, no se aplican solos: el sistema no sabe cual
    #: de los hornos del taller es «el chico», y adivinarlo por capacidad
    #: pondria una tarifa de 200 soles en el horno equivocado. Que lo diga una
    #: persona.
    reference_rates: dict[str, dict[str, Decimal]]
