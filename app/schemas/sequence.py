"""Contratos de las secuencias documentales."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.sequence_format import SequencePatternError, validate_pattern
from app.models.sequence import (
    MAX_PADDING,
    MAX_PREFIX_LENGTH,
    MIN_PADDING,
    ResetPolicy,
    SequenceType,
)


class SequenceConfigUpdate(BaseModel):
    """Cuerpo de ``PUT /settings/sequences/{tipo}``.

    Cambiar estos valores afecta **solo a los documentos futuros**. Los
    correlativos ya emitidos conservan el texto con el que se generaron.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prefix: Annotated[str, Field(min_length=1, max_length=MAX_PREFIX_LENGTH)]
    pattern: Annotated[str, Field(min_length=1, max_length=120)]
    padding: Annotated[int, Field(ge=MIN_PADDING, le=MAX_PADDING)]
    reset_policy: ResetPolicy
    active: bool
    version: Annotated[int, Field(ge=1)]

    @field_validator("prefix", mode="after")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        prefix = value.strip().upper()
        if not prefix.isalnum():
            raise ValueError("El prefijo solo admite letras y digitos")
        return prefix

    @field_validator("pattern", mode="after")
    @classmethod
    def _validate_pattern(cls, value: str) -> str:
        try:
            return validate_pattern(value)
        except SequencePatternError as exc:
            raise ValueError(str(exc)) from exc


class SequenceConfigOut(BaseModel):
    """Configuracion y estado de una secuencia."""

    sequence_type: SequenceType
    prefix: str
    pattern: str
    padding: int
    reset_policy: ResetPolicy
    active: bool

    #: Ultimo numero entregado en el periodo vigente. 0 = ninguno todavia.
    current_value: int
    period_key: str

    #: Como se veria el proximo correlativo. Es **solo informativo**: consultar
    #: esta configuracion no reserva ni consume ningun numero.
    preview: str

    version: int
    updated_at: datetime


class SequenceListOut(BaseModel):
    sequences: list[SequenceConfigOut]
