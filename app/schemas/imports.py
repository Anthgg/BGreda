"""Contratos del importador de maestros."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.importing import ImportAction, ImportEntity, ImportRowStatus, ImportStatus
from app.models.masters import PartnerRole


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SheetAnalysis(BaseModel):
    name: str
    rows: int
    columns: int
    headers: list[str]
    entity: ImportEntity | None
    detected: int
    warnings: list[str] = []


class ImportSummary(BaseModel):
    """Contadores que el usuario ve antes de confirmar."""

    creates: int = 0
    updates: int = 0
    skips: int = 0
    errors: int = 0
    warnings: int = 0
    review_required: int = 0
    by_entity: dict[str, dict[str, int]] = {}
    sheets: list[SheetAnalysis] = []
    recipes_detected: int = 0
    recipe_lines_detected: int = 0
    recipes_imported: int = 0
    duplicate_file: bool = False
    duplicate_of_batch_id: int | None = None


class ImportBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    file_hash: str
    file_size: int
    status: ImportStatus
    summary: ImportSummary
    error_message: str | None
    created_by: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime
    analyzed_at: datetime | None
    confirmed_at: datetime | None
    completed_at: datetime | None


class ImportBatchList(BaseModel):
    items: list[ImportBatchOut]
    total: int


class ImportRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity: ImportEntity
    sheet_name: str
    source_row: int
    raw: dict[str, Any]
    normalized: dict[str, Any]
    action: ImportAction
    status: ImportRowStatus
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    resolution: dict[str, Any] | None
    target_id: str | None


class ImportPreviewPage(BaseModel):
    batch: ImportBatchOut
    items: list[ImportRowOut]
    total: int
    limit: int
    offset: int


class RowResolution(_In):
    """Decision del usuario sobre una fila que requiere revision.

    Cada campo cubre una de las decisiones posibles del preview; los que no
    aplican a la entidad se ignoran.
    """

    row_id: int
    #: SKIP descarta la fila; RESOLVE la deja lista para el commit.
    action: Annotated[str, Field(pattern="^(RESOLVE|SKIP)$")] = "RESOLVE"
    product_id: int | None = None
    #: Unidad que el archivo no traia. Solo el usuario puede decidirla.
    base_uom_code: Annotated[str | None, Field(max_length=16)] = None
    partner_role: PartnerRole | None = None
    document_number: Annotated[str | None, Field(max_length=20)] = None
    ubigeo_code: Annotated[str | None, Field(min_length=6, max_length=6)] = None
    accept_suggestion: bool = False


class ResolutionRequest(_In):
    resolutions: list[RowResolution] = Field(min_length=1, max_length=1000)


class CommitResultEntity(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0


class ImportCommitResult(BaseModel):
    batch: ImportBatchOut
    by_entity: dict[str, CommitResultEntity]
    total_created: int
    total_updated: int
    total_skipped: int
