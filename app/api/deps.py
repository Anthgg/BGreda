"""Dependencias compartidas de la API.

Aqui vive la resolucion de la sesion: leer la cookie, verificar el token contra
Supabase y comprobar que el usuario tiene un perfil habilitado. Ninguna de esas
comprobaciones se delega al frontend.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.cookies import ACCESS_COOKIE_NAME
from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthAccountInactiveError,
    AuthInsufficientRoleError,
    AuthNotAuthenticatedError,
    AuthProfileNotProvisionedError,
    ServiceUnavailableError,
)
from app.db.session import get_db_session
from app.models.profile import UserRole
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.catalogs import CatalogService
from app.services.importing import ImportService
from app.services.importing.recipes import RecipeImportService
from app.services.inventory import InventoryService
from app.services.masters import MasterDataService
from app.services.profiles import ProfileRepository, SqlAlchemyProfileRepository
from app.services.recipes import RecipeService
from app.services.sequences import SequenceService
from app.services.settings import SettingsService
from app.services.storage import ObjectStorage, StorageUnavailableError
from app.services.supabase_auth import SupabaseAuthClient

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_supabase_auth_client(request: Request) -> SupabaseAuthClient:
    """Devuelve el cliente de Supabase creado durante el arranque."""
    client: SupabaseAuthClient | None = getattr(request.app.state, "supabase_auth", None)
    if client is None:
        raise ServiceUnavailableError(
            "El proveedor de identidad no esta configurado",
            code="SUPABASE_NOT_CONFIGURED",
        )
    return client


SupabaseAuthDep = Annotated[SupabaseAuthClient, Depends(get_supabase_auth_client)]


async def get_profile_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AsyncIterator[ProfileRepository]:
    """Repositorio de perfiles respaldado por PostgreSQL."""
    yield SqlAlchemyProfileRepository(session)


ProfileRepositoryDep = Annotated[ProfileRepository, Depends(get_profile_repository)]


async def resolve_profile(
    user_id: uuid.UUID,
    email: str,
    profiles: ProfileRepository,
) -> AuthenticatedUser:
    """Convierte una identidad ya verificada en un usuario de aplicacion.

    Reglas aplicadas:

    1. El usuario debe tener un perfil aprovisionado. No hay registro publico:
       existir en Supabase Auth no otorga acceso a la aplicacion.
    2. El perfil debe estar activo.
    """
    profile = await profiles.get_by_id(user_id)
    if profile is None:
        raise AuthProfileNotProvisionedError()
    if not profile.active:
        raise AuthAccountInactiveError()
    return AuthenticatedUser(
        id=profile.id,
        email=email,
        display_name=profile.display_name,
        role=UserRole(profile.role),
    )


async def resolve_user_from_token(
    access_token: str,
    *,
    supabase: SupabaseAuthClient,
    profiles: ProfileRepository,
) -> AuthenticatedUser:
    """Verifica un access token contra Supabase y resuelve el perfil."""
    identity = await supabase.get_user(access_token)
    return await resolve_profile(identity.id, identity.email, profiles)


def require_access_cookie(request: Request) -> str:
    """Exige la cookie de acceso antes de tocar cualquier dependencia externa.

    Se declara como dependencia propia y en primer lugar para que una peticion
    sin sesion responda 401 sin intentar abrir conexiones a Supabase ni a
    PostgreSQL.
    """
    access_token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not access_token:
        raise AuthNotAuthenticatedError()
    return access_token


AccessCookieDep = Annotated[str, Depends(require_access_cookie)]


async def get_current_user(
    access_token: AccessCookieDep,
    supabase: SupabaseAuthDep,
    profiles: ProfileRepositoryDep,
) -> AuthenticatedUser:
    """Usuario autenticado de la peticion actual.

    La unica evidencia aceptada es la cookie ``HttpOnly`` verificada contra
    Supabase. La existencia de la cookie por si sola no autentica a nadie.
    """
    return await resolve_user_from_token(access_token, supabase=supabase, profiles=profiles)


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require_roles(
    *roles: UserRole,
) -> Callable[[AuthenticatedUser], Coroutine[Any, Any, AuthenticatedUser]]:
    """Dependencia de autorizacion por rol.

    Es la primitiva que usaran los modulos de negocio de fases posteriores. La
    autoridad sobre los permisos es siempre del backend: ocultar un boton en
    React no es una medida de seguridad.
    """
    allowed = frozenset(roles)

    async def _dependency(user: CurrentUserDep) -> AuthenticatedUser:
        if user.role not in allowed:
            raise AuthInsufficientRoleError()
        return user

    return _dependency


# ---------------------------------------------------------------------------
# Fase 2: configuracion, secuencias, auditoria y almacenamiento
# ---------------------------------------------------------------------------
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]

#: Solo ADMIN modifica la configuracion. La restriccion la impone el backend:
#: ocultar el boton en React no es una medida de seguridad.
AdminUserDep = Annotated[AuthenticatedUser, Depends(require_roles(UserRole.ADMIN))]


async def get_audit_recorder(session: DbSessionDep) -> AuditRecorder:
    return AuditRecorder(session)


AuditRecorderDep = Annotated[AuditRecorder, Depends(get_audit_recorder)]


async def get_catalog_service(
    session: DbSessionDep,
    audit: AuditRecorderDep,
) -> CatalogService:
    return CatalogService(session, audit)


CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]


async def get_settings_service(
    session: DbSessionDep,
    audit: AuditRecorderDep,
) -> SettingsService:
    return SettingsService(session, audit)


SettingsServiceDep = Annotated[SettingsService, Depends(get_settings_service)]


async def get_sequence_service(session: DbSessionDep) -> SequenceService:
    return SequenceService(session)


SequenceServiceDep = Annotated[SequenceService, Depends(get_sequence_service)]


# ---------------------------------------------------------------------------
# Fase 3: maestros, inventario e importador
# ---------------------------------------------------------------------------
async def get_master_data_service(
    session: DbSessionDep,
    audit: AuditRecorderDep,
    sequences: SequenceServiceDep,
) -> MasterDataService:
    return MasterDataService(session, audit, sequences)


MasterDataServiceDep = Annotated[MasterDataService, Depends(get_master_data_service)]


async def get_inventory_service(session: DbSessionDep) -> InventoryService:
    return InventoryService(session)


InventoryServiceDep = Annotated[InventoryService, Depends(get_inventory_service)]


async def get_import_service(
    session: DbSessionDep,
    inventory: InventoryServiceDep,
    sequences: SequenceServiceDep,
) -> ImportService:
    return ImportService(session, inventory, sequences)


ImportServiceDep = Annotated[ImportService, Depends(get_import_service)]


def get_object_storage(request: Request) -> ObjectStorage:
    """Cliente de almacenamiento creado durante el arranque."""
    storage: ObjectStorage | None = getattr(request.app.state, "object_storage", None)
    if storage is None:
        raise StorageUnavailableError()
    return storage


ObjectStorageDep = Annotated[ObjectStorage, Depends(get_object_storage)]


# ---------------------------------------------------------------------------
# Fase 3.5: recetas, versiones, calculos e importador de formulas
# ---------------------------------------------------------------------------
async def get_recipe_service(
    session: DbSessionDep,
    audit: AuditRecorderDep,
) -> RecipeService:
    return RecipeService(session, audit)


RecipeServiceDep = Annotated[RecipeService, Depends(get_recipe_service)]


async def get_recipe_import_service(
    session: DbSessionDep,
    recipe_service: RecipeServiceDep,
    audit: AuditRecorderDep,
) -> RecipeImportService:
    return RecipeImportService(session, recipe_service, audit)


RecipeImportServiceDep = Annotated[RecipeImportService, Depends(get_recipe_import_service)]
