"""Esquemas transversales de la API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Detalle de un fallo de validacion. Nunca incluye el valor enviado."""

    field: str = Field(description="Ruta del campo que fallo la validacion")
    reason: str = Field(description="Motivo legible del fallo")
    type: str = Field(description="Identificador tecnico del tipo de fallo")


class ErrorBody(BaseModel):
    code: str = Field(description="Codigo estable del error, apto para logica de cliente")
    message: str = Field(description="Mensaje legible para el usuario final")
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    """Contrato uniforme de error de toda la API."""

    error: ErrorBody


class HealthComponent(BaseModel):
    """Estado de una dependencia. No revela endpoints ni credenciales."""

    name: str
    status: str
    required: bool


class LivenessResponse(BaseModel):
    status: str = "alive"
    app: str
    version: str


class ReadinessResponse(BaseModel):
    status: str
    components: list[HealthComponent]
