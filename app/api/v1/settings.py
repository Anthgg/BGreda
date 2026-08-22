"""Endpoints de configuracion.

Reparto de permisos, impuesto siempre en el backend:

- **Lectura**: cualquier usuario autenticado. OPERATOR necesita conocer moneda,
  IGV y textos para trabajar.
- **Escritura**: exclusivamente ADMIN.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Response, UploadFile, status
from fastapi.responses import Response as FileResponse

from app.api.deps import (
    AdminUserDep,
    AuditRecorderDep,
    CatalogServiceDep,
    CurrentUserDep,
    DbSessionDep,
    ObjectStorageDep,
    SequenceServiceDep,
    SettingsDep,
    SettingsServiceDep,
)
from app.models.audit import AuditAction
from app.models.sequence import DocumentSequence, SequenceType
from app.models.settings import CommercialSettings, CompanySettings
from app.schemas.audit import AuditEventOut, AuditEventPage
from app.schemas.catalog import (
    CurrencyOption,
    ReferenceDataOut,
    SequencePatternPresetCreate,
    SequencePatternPresetOut,
    UbigeoOption,
)
from app.schemas.common import ErrorResponse
from app.schemas.sequence import SequenceConfigOut, SequenceConfigUpdate, SequenceListOut
from app.schemas.settings import (
    BankAccountOut,
    CommercialSettingsOut,
    CommercialSettingsUpdate,
    CompanySettingsOut,
    CompanySettingsUpdate,
    LogoInfo,
)
from app.services.sequences import preview_for
from app.services.settings import ENTITY_COMPANY
from app.services.storage import build_logo_path, validate_logo

router = APIRouter(prefix="/settings", tags=["settings"])

LOGO_URL = "/api/v1/settings/company/logo"

#: Declarado a nivel de modulo: llamar a File() en el valor por defecto de
#: un argumento lo evaluaria en cada importacion.
LOGO_UPLOAD = File(...)

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Sin sesion valida"},
    403: {"model": ErrorResponse, "description": "Requiere rol ADMIN"},
    409: {"model": ErrorResponse, "description": "La configuracion cambio entretanto"},
    422: {"model": ErrorResponse, "description": "Datos invalidos"},
}


# ---------------------------------------------------------------------------
# Serializacion
# ---------------------------------------------------------------------------
def _company_out(settings: CompanySettings) -> CompanySettingsOut:
    logo = None
    if settings.logo_object_path:
        logo = LogoInfo(
            content_type=settings.logo_content_type or "application/octet-stream",
            size_bytes=settings.logo_size_bytes or 0,
            # Ruta al backend. El frontend jamas construye una URL de Storage.
            url=LOGO_URL,
        )
    return CompanySettingsOut(
        **{
            name: getattr(settings, name)
            for name in CompanySettingsOut.model_fields
            if name not in {"version", "updated_at", "logo"}
        },
        version=settings.version,
        updated_at=settings.updated_at,
        logo=logo,
    )


def _commercial_out(settings: CommercialSettings) -> CommercialSettingsOut:
    return CommercialSettingsOut(
        **{
            name: getattr(settings, name)
            for name in CommercialSettingsOut.model_fields
            if name not in {"version", "updated_at", "bank_accounts"}
        },
        version=settings.version,
        updated_at=settings.updated_at,
        bank_accounts=[
            BankAccountOut(
                id=account.id,
                is_primary=account.is_primary,
                bank_name=account.bank_name,
                account_holder=account.account_holder,
                account_number=account.account_number,
                cci=account.cci,
                notes=account.notes,
            )
            for account in settings.bank_accounts
        ],
    )


def _sequence_out(sequence: DocumentSequence) -> SequenceConfigOut:
    return SequenceConfigOut(
        sequence_type=SequenceType(sequence.sequence_type),
        prefix=sequence.prefix,
        pattern=sequence.pattern,
        padding=sequence.padding,
        reset_policy=sequence.reset_policy,
        active=sequence.active,
        current_value=sequence.current_value,
        period_key=sequence.period_key,
        # Calculado en memoria: consultar nunca consume un correlativo.
        preview=preview_for(sequence),
        version=sequence.version,
        updated_at=sequence.updated_at,
    )


# ---------------------------------------------------------------------------
# Catalogos controlados
# ---------------------------------------------------------------------------
@router.get("/reference-data", response_model=ReferenceDataOut, responses=_ERRORS)
async def read_reference_data(
    _: CurrentUserDep,
    service: CatalogServiceDep,
) -> ReferenceDataOut:
    """Monedas ISO, ubigeos INEI y formatos reutilizables almacenados en BD."""
    currencies = await service.list_currencies()
    districts = await service.list_districts()
    patterns = await service.list_sequence_patterns()
    return ReferenceDataOut(
        currencies=[CurrencyOption.model_validate(item) for item in currencies],
        districts=[UbigeoOption.model_validate(item) for item in districts],
        sequence_patterns=[SequencePatternPresetOut.model_validate(item) for item in patterns],
    )


@router.post(
    "/sequence-patterns",
    response_model=SequencePatternPresetOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
async def create_sequence_pattern(
    payload: SequencePatternPresetCreate,
    user: AdminUserDep,
    service: CatalogServiceDep,
    session: DbSessionDep,
) -> SequencePatternPresetOut:
    preset = await service.create_sequence_pattern(payload, user)
    await session.commit()
    await session.refresh(preset)
    return SequencePatternPresetOut.model_validate(preset)


# ---------------------------------------------------------------------------
# Empresa
# ---------------------------------------------------------------------------
@router.get("/company", response_model=CompanySettingsOut, responses=_ERRORS)
async def read_company(_: CurrentUserDep, service: SettingsServiceDep) -> CompanySettingsOut:
    return _company_out(await service.get_company())


@router.put("/company", response_model=CompanySettingsOut, responses=_ERRORS)
async def update_company(
    payload: CompanySettingsUpdate,
    user: AdminUserDep,
    service: SettingsServiceDep,
    session: DbSessionDep,
) -> CompanySettingsOut:
    settings = await service.update_company(payload, user)
    await session.commit()
    await session.refresh(settings)
    return _company_out(settings)


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------
@router.get("/company/logo", responses=_ERRORS, response_class=FileResponse)
async def read_logo(
    _: CurrentUserDep,
    service: SettingsServiceDep,
    storage: ObjectStorageDep,
) -> Response:
    """Sirve el logo desde el backend.

    El bucket es privado a proposito: el navegador nunca contacta con Supabase.
    """
    settings = await service.get_company()
    if not settings.logo_object_path:
        return Response(status_code=404)
    data = await storage.download(settings.logo_object_path)
    return Response(
        content=data,
        media_type=settings.logo_content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.post("/company/logo", response_model=CompanySettingsOut, responses=_ERRORS)
async def upload_logo(
    user: AdminUserDep,
    service: SettingsServiceDep,
    storage: ObjectStorageDep,
    session: DbSessionDep,
    audit: AuditRecorderDep,
    app_settings: SettingsDep,
    file: UploadFile = LOGO_UPLOAD,
) -> CompanySettingsOut:
    """Sube el logo. El nombre original nunca determina la ruta interna."""
    data = await file.read()
    content_type = validate_logo(
        data=data,
        filename=file.filename,
        declared_content_type=file.content_type,
        max_bytes=app_settings.LOGO_MAX_BYTES,
    )

    settings = await service.get_company()
    previous_path = settings.logo_object_path

    path = build_logo_path(content_type)
    await storage.upload(path, data, content_type)

    settings.logo_object_path = path
    settings.logo_content_type = content_type
    settings.logo_size_bytes = len(data)
    settings.version += 1

    # Se audita el cambio de referencia y el tamano, jamas el binario.
    audit.record_action(
        entity_type=ENTITY_COMPANY,
        entity_id=str(settings.id),
        action=AuditAction.UPDATE,
        user_id=user.id,
        user_display_name=user.display_name,
        metadata={"field": "logo", "content_type": content_type, "size_bytes": len(data)},
    )
    await session.commit()

    if previous_path and previous_path != path:
        # Se borra despues de confirmar: si el commit fallara, el logo anterior
        # seguiria siendo el vigente y debe continuar existiendo.
        await storage.delete(previous_path)

    await session.refresh(settings)
    return _company_out(settings)


@router.delete("/company/logo", response_model=CompanySettingsOut, responses=_ERRORS)
async def delete_logo(
    user: AdminUserDep,
    service: SettingsServiceDep,
    storage: ObjectStorageDep,
    session: DbSessionDep,
    audit: AuditRecorderDep,
) -> CompanySettingsOut:
    settings = await service.get_company()
    previous_path = settings.logo_object_path

    if previous_path:
        settings.logo_object_path = None
        settings.logo_content_type = None
        settings.logo_size_bytes = None
        settings.version += 1
        audit.record_action(
            entity_type=ENTITY_COMPANY,
            entity_id=str(settings.id),
            action=AuditAction.DELETE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"field": "logo"},
        )
        await session.commit()
        await storage.delete(previous_path)
        await session.refresh(settings)

    return _company_out(settings)


# ---------------------------------------------------------------------------
# Comercial
# ---------------------------------------------------------------------------
@router.get("/commercial", response_model=CommercialSettingsOut, responses=_ERRORS)
async def read_commercial(_: CurrentUserDep, service: SettingsServiceDep) -> CommercialSettingsOut:
    return _commercial_out(await service.get_commercial())


@router.put("/commercial", response_model=CommercialSettingsOut, responses=_ERRORS)
async def update_commercial(
    payload: CommercialSettingsUpdate,
    user: AdminUserDep,
    service: SettingsServiceDep,
    session: DbSessionDep,
) -> CommercialSettingsOut:
    await service.update_commercial(payload, user)
    await session.commit()
    # Se relee para devolver las cuentas bancarias ya materializadas.
    return _commercial_out(await service.get_commercial())


# ---------------------------------------------------------------------------
# Secuencias
# ---------------------------------------------------------------------------
@router.get("/sequences", response_model=SequenceListOut, responses=_ERRORS)
async def read_sequences(_: CurrentUserDep, service: SequenceServiceDep) -> SequenceListOut:
    """Configuracion de las secuencias.

    ``preview`` es solo una muestra del formato: leer este endpoint **no**
    reserva ni consume ningun correlativo.
    """
    sequences = await service.list_sequences()
    return SequenceListOut(sequences=[_sequence_out(item) for item in sequences])


@router.put("/sequences/{sequence_type}", response_model=SequenceConfigOut, responses=_ERRORS)
async def update_sequence(
    sequence_type: SequenceType,
    payload: SequenceConfigUpdate,
    user: AdminUserDep,
    service: SequenceServiceDep,
    session: DbSessionDep,
    audit: AuditRecorderDep,
) -> SequenceConfigOut:
    """Actualiza el formato de una secuencia.

    Solo afecta a documentos futuros: los correlativos ya emitidos conservan el
    texto con el que se generaron.
    """
    from app.services.audit import diff_model
    from app.services.settings import SettingsVersionConflictError

    sequence = await service.get(sequence_type)
    if sequence.version != payload.version:
        raise SettingsVersionConflictError()

    incoming = payload.model_dump(exclude={"version"})
    fields = ["prefix", "pattern", "padding", "reset_policy", "active"]
    changes = diff_model(sequence, incoming, fields)

    for field, (_, new) in changes.items():
        setattr(sequence, field, new)

    if changes:
        sequence.version += 1
        audit.record_changes(
            entity_type="document_sequence",
            entity_id=str(sequence.sequence_type),
            changes=changes,
            user_id=user.id,
            user_display_name=user.display_name,
        )
    await session.commit()
    await session.refresh(sequence)
    return _sequence_out(sequence)


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------
@router.get("/audit", response_model=AuditEventPage, responses=_ERRORS)
async def read_audit(
    _: AdminUserDep,
    audit: AuditRecorderDep,
    entity_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AuditEventPage:
    """Historial de cambios de configuracion. Reservado a ADMIN."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    items, total = await audit.list_events(entity_type=entity_type, limit=limit, offset=offset)
    return AuditEventPage(
        items=[AuditEventOut.model_validate(item, from_attributes=True) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
