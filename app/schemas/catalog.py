"""Contratos de los catalogos de configuracion."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.sequence_format import SequencePatternError, validate_pattern

_TAG_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z]")


class _CatalogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CurrencyOption(_CatalogOut):
    code: str
    numeric_code: str
    name: str
    symbol: str
    minor_units: int | None


class UbigeoOption(_CatalogOut):
    code: str
    department_code: str
    department_name: str
    province_code: str
    province_name: str
    district_name: str


class SequencePatternPresetOut(_CatalogOut):
    id: int
    name: str
    pattern: str
    is_system: bool


class ReferenceDataOut(BaseModel):
    currencies: list[CurrencyOption]
    districts: list[UbigeoOption]
    sequence_patterns: list[SequencePatternPresetOut]


class SequencePatternPresetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=2, max_length=80)]
    pattern: Annotated[str, Field(min_length=1, max_length=120)]

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if _TAG_PATTERN.search(value):
            raise ValueError("El nombre no admite etiquetas HTML")
        return value

    @field_validator("pattern", mode="after")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        try:
            return validate_pattern(value)
        except SequencePatternError as exc:
            raise ValueError(str(exc)) from exc
