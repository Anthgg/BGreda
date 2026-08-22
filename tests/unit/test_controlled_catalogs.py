"""Catalogos oficiales y canonizacion de valores de configuracion."""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.models.catalog import CurrencyCatalog, UbigeoDistrict
from app.schemas.catalog import SequencePatternPresetCreate
from app.schemas.settings import CompanySettingsUpdate
from app.services.audit import AuditRecorder
from app.services.settings import InvalidCatalogValueError, SettingsService

DATA_DIR = Path(__file__).resolve().parents[2] / "alembic" / "data"


def _rows(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _service(result_value: object | None) -> SettingsService:
    session = MagicMock()
    session.execute = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = result_value
    session.execute.return_value = result
    return SettingsService(session, AuditRecorder(session))


def test_el_catalogo_contiene_las_176_monedas_vigentes_de_uso() -> None:
    currencies = _rows("iso_4217_2026.csv")

    assert len(currencies) == 176
    assert {row["code"] for row in currencies}.isdisjoint({"XTS", "XXX"})
    assert next(row for row in currencies if row["code"] == "PEN")["symbol"] == "S/"


def test_el_catalogo_contiene_los_1891_distritos_inei() -> None:
    districts = _rows("ubigeo_inei_2022.csv")

    assert len(districts) == 1891
    assert len({row["code"] for row in districts}) == 1891
    assert all(row["department_code"] == row["code"][:2] for row in districts)
    assert all(row["province_code"] == row["code"][:4] for row in districts)


def test_un_ubigeo_debe_tener_seis_digitos() -> None:
    with pytest.raises(ValidationError):
        CompanySettingsUpdate(version=1, ubigeo_code="LIMA")


def test_un_formato_creado_debe_contener_number() -> None:
    with pytest.raises(ValidationError, match="NUMBER"):
        SequencePatternPresetCreate(name="Sin numero", pattern="{PREFIX}-{YYYY}")


@pytest.mark.asyncio
async def test_el_servidor_canoniza_todos_los_nombres_desde_el_ubigeo() -> None:
    district = UbigeoDistrict(
        code="150122",
        department_code="15",
        department_name="LIMA",
        province_code="1501",
        province_name="LIMA",
        district_name="MIRAFLORES",
    )
    incoming: dict[str, object] = {
        "ubigeo_code": "150122",
        "department": "inventado",
        "province": "inventado",
        "district": "inventado",
        "country": "inventado",
    }

    await _service(district)._canonicalize_location(incoming)

    assert incoming == {
        "ubigeo_code": "150122",
        "department": "LIMA",
        "province": "LIMA",
        "district": "MIRAFLORES",
        "country": "Peru",
    }


@pytest.mark.asyncio
async def test_no_se_admiten_nombres_de_ubicacion_sin_ubigeo() -> None:
    incoming: dict[str, object] = {
        "ubigeo_code": None,
        "department": "LIMA",
        "province": None,
        "district": None,
        "country": "Peru",
    }

    with pytest.raises(InvalidCatalogValueError, match="catalogo INEI"):
        await _service(None)._canonicalize_location(incoming)


@pytest.mark.asyncio
async def test_el_simbolo_siempre_proviene_de_la_moneda_catalogada() -> None:
    currency = CurrencyCatalog(
        code="PEN",
        numeric_code="604",
        name="sol peruano",
        symbol="S/",
        minor_units=2,
    )
    incoming: dict[str, object] = {"currency_code": "PEN", "currency_symbol": "inventado"}

    await _service(currency)._canonicalize_currency(incoming)

    assert incoming["currency_symbol"] == "S/"


@pytest.mark.asyncio
async def test_una_moneda_que_no_existe_se_rechaza_en_el_servidor() -> None:
    with pytest.raises(InvalidCatalogValueError, match="ISO 4217"):
        await _service(None)._canonicalize_currency(
            {"currency_code": "ZZZ", "currency_symbol": "Z"}
        )
