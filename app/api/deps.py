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
from app.services.profiles import ProfileRepository, SqlAlchemyProfileRepository
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
