"""Fase 010B — la configuracion V2 no duplica lo que ya tiene dueno.

El riesgo concreto de esta fase no es que falte un campo: es que sobre. Un
segundo IGV, una segunda moneda o una segunda tabla de tarifas de horno no dan
error — dan dos numeros distintos para la misma pregunta, y el dia que alguien
edite el que no es, el documento sale mal sin que nada avise.

Estas pruebas fijan las fronteras de la configuracion sin necesitar base de
datos.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.quoter_v2_config import (
    DEFAULT_ADMINISTRATIVE_COST_PER_QUOTE,
    DEFAULT_COMMERCIAL_FACTOR,
    DEFAULT_COMMERCIAL_FACTOR_MIN,
    DEFAULT_ILLUSTRATION_DAILY_RATE,
    DEFAULT_ILLUSTRATION_PIECES_PER_WORKDAY,
    DEFAULT_QUOTATION_VALIDITY_DAYS,
    DEFAULT_SPACE_SERVICE_COST_PER_DAY,
    DEFAULT_WORKDAY_HOURS,
    V2_REFERENCE_KILN_RATES,
)
from app.models.quoter_v2 import V2CustomerKind
from app.models.quoter_v2_settings import V2CommercialSettings, V2KilnRate
from app.models.settings import CommercialSettings
from app.schemas.quoter_v2_settings import V2SettingsUpdateIn


# ---------------------------------------------------------------------------
# 1. Lo que NO se duplica
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("columna", ["tax_percent", "currency_code", "rounding_step"])
def test_v2_no_tiene_su_propia_politica_fiscal(columna: str) -> None:
    """El IGV, la moneda y el redondeo siguen teniendo un unico dueno.

    Si V2 declarara los suyos, habria dos sitios donde editarlos y el que
    quedara desactualizado emitiria documentos con el impuesto equivocado.
    """
    assert columna in CommercialSettings.__table__.c, (
        f"{columna} deberia seguir en commercial_settings"
    )
    assert columna not in V2CommercialSettings.__table__.c, (
        f"V2 declaro su propio {columna}: hay dos fuentes para el mismo dato"
    )


def test_el_contrato_de_escritura_no_admite_el_igv() -> None:
    """Aceptarlo por esta puerta crearia una segunda forma de cambiarlo."""
    campos = set(V2SettingsUpdateIn.model_fields)
    assert not campos & {"tax_percent", "currency_code", "currency_symbol", "rounding_step"}


def test_las_tarifas_v2_viven_en_su_propia_tabla() -> None:
    """No en `kiln_rates`.

    El costeo Legacy toma la PRIMERA tarifa que encuentra para cada
    `(kiln_id, firing_type)`, sin mas discriminante. Una fila de alumno o de
    gas ahi dentro le haria cobrar una quema con el numero que no era.
    """
    assert V2KilnRate.__tablename__ == "v2_kiln_rates"
    columnas = set(V2KilnRate.__table__.c.keys())
    # Los tres conceptos, separados: lo que cuesta y lo que se cobra a cada uno.
    assert {"gas_cost", "external_rate", "student_rate"} <= columnas


def test_la_tarifa_por_hora_de_ilustracion_no_se_almacena() -> None:
    """Se deriva del jornal y la jornada.

    Guardarla ademas permitiria que contradijera a sus fuentes, y entonces
    habria que decidir cual manda.
    """
    columnas = set(V2CommercialSettings.__table__.c.keys())
    assert "illustration_hourly_rate" not in columnas
    assert "illustration_workday_hours" not in columnas
    assert {"illustration_daily_rate", "illustration_pieces_per_workday"} <= columnas


# ---------------------------------------------------------------------------
# 2. Los valores aprobados
# ---------------------------------------------------------------------------
def test_los_defaults_son_los_aprobados() -> None:
    """Los numeros del Excel «Cotizador Greda V2», no otros."""
    assert DEFAULT_WORKDAY_HOURS == Decimal("8")
    assert DEFAULT_SPACE_SERVICE_COST_PER_DAY == Decimal("140")
    assert DEFAULT_ADMINISTRATIVE_COST_PER_QUOTE == Decimal("200")
    assert DEFAULT_QUOTATION_VALIDITY_DAYS == 20
    assert DEFAULT_ILLUSTRATION_DAILY_RATE == Decimal("110")
    assert DEFAULT_ILLUSTRATION_PIECES_PER_WORKDAY == Decimal("50")


def test_el_factor_es_un_factor_y_no_un_porcentaje() -> None:
    """x3, no 300.

    El negocio lo dice como factor y el Excel lo define como factor. Guardarlo
    como porcentaje obligaria a traducir en cada frontera y a acertar siempre.
    """
    assert DEFAULT_COMMERCIAL_FACTOR == Decimal("3")
    assert DEFAULT_COMMERCIAL_FACTOR_MIN == Decimal("2")


def test_las_tarifas_de_referencia_son_las_aprobadas() -> None:
    """Gas real, externo y alumno, para horno chico y grande."""
    chico = V2_REFERENCE_KILN_RATES["SMALL"]
    grande = V2_REFERENCE_KILN_RATES["LARGE"]

    assert (chico["gas_cost_low"], chico["gas_cost_high"]) == (Decimal("35"), Decimal("70"))
    assert (grande["gas_cost_low"], grande["gas_cost_high"]) == (Decimal("55"), Decimal("110"))
    assert (chico["external_rate_low"], chico["external_rate_high"]) == (
        Decimal("200"),
        Decimal("250"),
    )
    assert (grande["external_rate_low"], grande["external_rate_high"]) == (
        Decimal("700"),
        Decimal("1200"),
    )
    assert (chico["student_rate_low"], chico["student_rate_high"]) == (
        Decimal("90"),
        Decimal("180"),
    )
    assert (grande["student_rate_low"], grande["student_rate_high"]) == (
        Decimal("1000"),
        Decimal("2000"),
    )


def test_el_gas_no_se_confunde_con_la_tarifa_comercial() -> None:
    """Son conceptos distintos y por eso los numeros no coinciden.

    La diferencia entre lo que se cobra y lo que cuesta el gas es la ganancia
    propia de la quema. Si el motor los mezclara, esa ganancia desapareceria
    del calculo sin que nadie lo notara.
    """
    for tamano in ("SMALL", "LARGE"):
        tarifas = V2_REFERENCE_KILN_RATES[tamano]
        assert tarifas["external_rate_low"] > tarifas["gas_cost_low"]
        assert tarifas["external_rate_high"] > tarifas["gas_cost_high"]


# ---------------------------------------------------------------------------
# 3. El tipo de cliente es explicito
# ---------------------------------------------------------------------------
def test_solo_existen_dos_tipos_de_cliente() -> None:
    assert [k.value for k in V2CustomerKind] == ["EXTERNAL", "STUDENT"]


def test_el_tipo_de_cliente_se_persiste_en_la_cotizacion() -> None:
    """Se elige y se guarda; nunca se deduce del nombre del tercero."""
    from app.models.quoter_v2 import V2Quotation

    assert "customer_kind" in V2Quotation.__table__.c


# ---------------------------------------------------------------------------
# 4. Validaciones del contrato de escritura
# ---------------------------------------------------------------------------
def test_el_contrato_rechaza_un_factor_por_debajo_del_suelo() -> None:
    """x2 es una regla cerrada, no una preferencia configurable."""
    with pytest.raises(ValueError):
        V2SettingsUpdateIn(expected_version=1, commercial_factor_min=Decimal("1.5"))


def test_el_contrato_rechaza_una_jornada_de_cero_horas() -> None:
    """Seria una division por cero en cuanto alguien calcule una tarifa hora."""
    with pytest.raises(ValueError):
        V2SettingsUpdateIn(expected_version=1, workday_hours=Decimal("0"))


def test_el_contrato_rechaza_costos_negativos() -> None:
    with pytest.raises(ValueError):
        V2SettingsUpdateIn(expected_version=1, space_service_cost_per_day=Decimal("-1"))


def test_el_contrato_rechaza_una_vigencia_de_cero_dias() -> None:
    with pytest.raises(ValueError):
        V2SettingsUpdateIn(expected_version=1, quotation_validity_days=0)


def test_el_contrato_rechaza_campos_desconocidos() -> None:
    """`extra=forbid`: un campo mal escrito falla en vez de ignorarse.

    Silenciarlo haria creer que se guardo un cambio que no se guardo.
    """
    with pytest.raises(ValueError):
        V2SettingsUpdateIn(expected_version=1, tax_percent=Decimal("18"))
