"""Normalizacion de la URL de base de datos y convencion de precision."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.config import Settings
from app.core.errors import ServiceUnavailableError
from app.core.precision import MONEY_PRECISION, MONEY_SCALE, money_numeric, quantity_numeric
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
