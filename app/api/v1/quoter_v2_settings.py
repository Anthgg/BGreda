"""Superficie HTTP de la configuracion comercial del Cotizador V2.

Ruta propia bajo `/quoter-v2/settings`, separada de `/settings` —la
configuracion de la empresa— y de `/quotations-v2` —los documentos—. Tres
espacios distintos porque son tres cosas distintas, y mezclarlos haria que una
peticion de configuracion pudiera resolverse como si fuera un documento.

Configurar es administracion: define los precios con los que va a cotizar todo
el taller.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import AdminUserDep, DbSessionDep, V2SettingsServiceDep
from app.core.quoter_v2_config import V2_REFERENCE_KILN_RATES
from app.models.firings import FiringType
from app.models.quoter_v2_settings import V2CommercialSettings, V2KilnRate
from app.schemas.quoter_v2_settings import (
    V2KilnRateIn,
    V2KilnRateOut,
    V2SettingsOut,
    V2SettingsPage,
    V2SettingsUpdateIn,
)
from app.services.quoter_v2_settings import V2SettingsService

router = APIRouter(prefix="/quoter-v2/settings", tags=["cotizador-v2"])


def _rate_out(fila: V2KilnRate) -> V2KilnRateOut:
    return V2KilnRateOut(
        kiln_id=fila.kiln_id,
        kiln_code=fila.kiln.code,
        kiln_name=fila.kiln.name,
        firing_type=fila.firing_type,
        gas_cost=fila.gas_cost,
        external_rate=fila.external_rate,
        student_rate=fila.student_rate,
    )


def _settings_out(
    fila: V2CommercialSettings,
    tax: Decimal | None,
    code: str | None,
    symbol: str | None,
) -> V2SettingsOut:
    """Arma la salida, derivando lo que no debe almacenarse.

    La tarifa horaria y el rendimiento por hora de ilustracion se calculan
    aqui. Guardarlos permitiria que contradijeran al jornal y a la jornada, y
    entonces habria que decidir cual de los dos manda.
    """
    horas = fila.workday_hours
    return V2SettingsOut(
        version=fila.version,
        updated_at=fila.updated_at,
        workday_hours=horas,
        space_service_cost_per_day=fila.space_service_cost_per_day,
        administrative_cost_per_quote=fila.administrative_cost_per_quote,
        commercial_factor_default=fila.commercial_factor_default,
        commercial_factor_min=fila.commercial_factor_min,
        commercial_factor_max=fila.commercial_factor_max,
        quotation_validity_days=fila.quotation_validity_days,
        default_exchange_rate=fila.default_exchange_rate,
        default_production_type=fila.default_production_type,
        default_customer_kind=fila.default_customer_kind,
        retail_kiln_id=fila.retail_kiln_id,
        wholesale_kiln_id=fila.wholesale_kiln_id,
        low_fire_enabled_default=fila.low_fire_enabled_default,
        high_fire_enabled_default=fila.high_fire_enabled_default,
        illustration_daily_rate=fila.illustration_daily_rate,
        illustration_pieces_per_workday=fila.illustration_pieces_per_workday,
        illustration_hourly_rate=(
            fila.illustration_daily_rate / horas if horas > Decimal(0) else Decimal(0)
        ),
        illustration_pieces_per_hour=(
            fila.illustration_pieces_per_workday / horas if horas > Decimal(0) else Decimal(0)
        ),
        tax_percent=tax,
        currency_code=code,
        currency_symbol=symbol,
    )


async def _page(service: V2SettingsService) -> V2SettingsPage:
    fila = await service.get()
    politica = await service.commercial_policy()
    return V2SettingsPage(
        settings=_settings_out(
            fila, politica.tax_percent, politica.currency_code, politica.currency_symbol
        ),
        kiln_rates=[_rate_out(rate) for rate in await service.kiln_rates()],
        reference_rates=V2_REFERENCE_KILN_RATES,
    )


@router.get("", response_model=V2SettingsPage)
async def read_v2_settings(
    service: V2SettingsServiceDep,
    _: AdminUserDep,
) -> V2SettingsPage:
    return await _page(service)


@router.put("", response_model=V2SettingsPage)
async def update_v2_settings(
    payload: V2SettingsUpdateIn,
    service: V2SettingsServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2SettingsPage:
    datos = payload.model_dump(exclude_unset=True)
    esperada = datos.pop("expected_version")
    await service.update(datos, expected_version=esperada, user=admin)
    resultado = await _page(service)
    await session.commit()
    return resultado


@router.put("/kiln-rates/{kiln_id}/{firing_type}", response_model=V2SettingsPage)
async def set_v2_kiln_rate(
    kiln_id: Annotated[int, Path(ge=1)],
    firing_type: FiringType,
    payload: V2KilnRateIn,
    service: V2SettingsServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2SettingsPage:
    """Fija los tres numeros de un horno, para baja o para alta.

    Devuelve la pagina entera y no solo la tarifa: la pantalla muestra la
    tabla completa y con una respuesta parcial tendria que pedirla otra vez.
    """
    await service.set_kiln_rate(
        kiln_id, firing_type, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = await _page(service)
    await session.commit()
    return resultado
