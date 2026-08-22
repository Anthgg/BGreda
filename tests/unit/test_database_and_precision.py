"""Normalizacion de la URL de base de datos y convencion de precision."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.core.errors import ServiceUnavailableError
from app.core.precision import (
    MONEY_PRECISION,
    MONEY_SCALE,
    UNIT_COST_PRECISION,
    UNIT_COST_SCALE,
    money_numeric,
    quantity_numeric,
    unit_cost_numeric,
)
from app.db.session import create_engine_from_settings, normalize_database_url


@pytest.mark.parametrize(
    ("entrada", "esperada"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ],
)
def test_la_url_se_normaliza_al_driver_asincrono(entrada: str, esperada: str) -> None:
    assert normalize_database_url(entrada) == esperada


def test_sin_database_url_el_motor_falla_de_forma_explicita() -> None:
    with pytest.raises(ServiceUnavailableError) as excinfo:
        create_engine_from_settings(Settings(DATABASE_URL=""))

    assert excinfo.value.code == "DB_NOT_CONFIGURED"


def test_las_columnas_monetarias_son_numeric_con_decimal() -> None:
    columna = money_numeric()

    assert columna.precision == MONEY_PRECISION
    assert columna.scale == MONEY_SCALE
    assert columna.asdecimal is True
    assert columna.python_type is Decimal


def test_las_columnas_de_cantidad_son_numeric_con_decimal() -> None:
    columna = quantity_numeric()

    assert columna.asdecimal is True
    assert columna.python_type is Decimal


def test_los_costos_unitarios_usan_la_escala_que_exige_el_plan() -> None:
    """El Plan v1.2 la impone: un insumo puede costar menos de S/ 0.01 por gramo."""
    columna = unit_cost_numeric()

    assert columna.precision == UNIT_COST_PRECISION
    assert columna.scale == UNIT_COST_SCALE
    assert columna.asdecimal is True
    assert columna.python_type is Decimal


def test_la_escala_de_costo_unitario_no_redondea_a_cero() -> None:
    """Con dos decimales, 0.0034 por gramo se convertiria en 0."""
    valor = Decimal("0.003400000000")

    assert valor.quantize(Decimal(1).scaleb(-UNIT_COST_SCALE)) == valor
    assert UNIT_COST_SCALE > MONEY_SCALE
