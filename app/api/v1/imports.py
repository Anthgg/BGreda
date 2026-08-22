"""Endpoints del importador de maestros.

El flujo es explicito y en pasos: subir -> analizar -> revisar -> resolver ->
confirmar. No hay ningun atajo que escriba los maestros sin pasar por el
preview.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile, status

from app.api.deps import AdminUserDep, CurrentUserDep, DbSessionDep, ImportServiceDep
from app.models.importing import ImportEntity, ImportRowStatus
from app.schemas.common import ErrorResponse
from app.schemas.imports import (
    ImportBatchList,
    ImportBatchOut,
    ImportCommitResult,
    ImportPreviewPage,
    ImportRowOut,
    ResolutionRequest,
)
from app.services.importing.workbook import WorkbookError

router = APIRouter(prefix="/imports", tags=["imports"])

#: Declarado a nivel de modulo: llamar a File() en el valor por defecto de un
#: argumento lo evaluaria en cada importacion.
FILE_UPLOAD = File(...)

#: Un maestro de taller no llega a esto ni de lejos; el limite existe para que
#: una subida absurda no consuma memoria del contenedor.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Sin sesion valida"},
    403: {"model": ErrorResponse, "description": "Requiere rol ADMIN"},
    404: {"model": ErrorResponse, "description": "La importacion no existe"},
    409: {"model": ErrorResponse, "description": "Estado incompatible"},
    422: {"model": ErrorResponse, "description": "Archivo o filas invalidas"},
}

LimitDep = Annotated[int, Query(ge=1, le=500)]
OffsetDep = Annotated[int, Query(ge=0)]


@router.post(
    "/master/upload",
    response_model=ImportBatchOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def upload_master_workbook(
    user: AdminUserDep,
    service: ImportServiceDep,
    session: DbSessionDep,
    file: UploadFile = FILE_UPLOAD,
) -> ImportBatchOut:
    """Sube el libro, lo analiza y deja el staging listo para revisar.

    No escribe ni un solo registro en los maestros.
    """
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise WorkbookError("El archivo supera el tamano maximo admitido")
    batch = await service.upload(
        filename=file.filename or "maestros.xlsx", payload=payload, user=user
    )
    await session.commit()
    await session.refresh(batch)
    return ImportBatchOut.model_validate(batch)


@router.get("", response_model=ImportBatchList, responses=_ERRORS)
async def list_imports(
    _: CurrentUserDep, service: ImportServiceDep, limit: LimitDep = 20
) -> ImportBatchList:
    batches, total = await service.list_batches(limit=limit)
    return ImportBatchList(
        items=[ImportBatchOut.model_validate(item) for item in batches], total=total
    )


@router.get("/{batch_id}", response_model=ImportBatchOut, responses=_ERRORS)
async def read_import(
    batch_id: int, _: CurrentUserDep, service: ImportServiceDep
) -> ImportBatchOut:
    return ImportBatchOut.model_validate(await service.get_batch(batch_id))


@router.get("/{batch_id}/preview", response_model=ImportPreviewPage, responses=_ERRORS)
async def read_preview(
    batch_id: int,
    _: CurrentUserDep,
    service: ImportServiceDep,
    entity: ImportEntity | None = None,
    row_status: ImportRowStatus | None = None,
    limit: LimitDep = 100,
    offset: OffsetDep = 0,
) -> ImportPreviewPage:
    """Consulta el staging. Es de solo lectura: no muta ningun maestro."""
    batch, rows, total = await service.preview(
        batch_id, entity=entity, status=row_status, limit=limit, offset=offset
    )
    return ImportPreviewPage(
        batch=ImportBatchOut.model_validate(batch),
        items=[ImportRowOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{batch_id}/resolve", response_model=ImportBatchOut, responses=_ERRORS)
async def resolve_rows(
    batch_id: int,
    payload: ResolutionRequest,
    _: AdminUserDep,
    service: ImportServiceDep,
    session: DbSessionDep,
) -> ImportBatchOut:
    """Aplica las decisiones del usuario sobre las filas en revision."""
    batch = await service.resolve(batch_id, payload.resolutions)
    await session.commit()
    await session.refresh(batch)
    return ImportBatchOut.model_validate(batch)


@router.post("/{batch_id}/commit", response_model=ImportCommitResult, responses=_ERRORS)
async def commit_import(
    batch_id: int,
    user: AdminUserDep,
    service: ImportServiceDep,
    session: DbSessionDep,
) -> ImportCommitResult:
    """Escribe los maestros en una unica transaccion.

    Si cualquier operacion falla, la sesion se deshace entera: no queda medio
    maestro importado.
    """
    try:
        result = await service.commit(batch_id, user)
    except Exception:
        await session.rollback()
        raise
    await session.commit()
    batch = await service.get_batch(batch_id)
    return ImportCommitResult(
        batch=ImportBatchOut.model_validate(batch),
        by_entity=result,
        total_created=sum(item["created"] for item in result.values()),
        total_updated=sum(item["updated"] for item in result.values()),
        total_skipped=sum(item["skipped"] for item in result.values()),
    )
