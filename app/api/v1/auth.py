"""Endpoints de autenticacion.

Supabase Auth se consume unicamente desde aqui. El frontend jamas recibe un
access token ni un refresh token: solo cookies ``HttpOnly`` y los datos de
usuario estrictamente necesarios para pintar la interfaz.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import (
    CurrentUserDep,
    ProfileRepositoryDep,
    SettingsDep,
    SupabaseAuthDep,
    resolve_profile,
)
from app.auth import csrf
from app.auth.cookies import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    clear_session_cookies,
    set_csrf_cookie,
    set_session_cookies,
)
from app.core.errors import AuthNotAuthenticatedError, AuthSessionExpiredError
from app.schemas.auth import (
    CsrfTokenResponse,
    LoginRequest,
    LogoutResponse,
    SessionResponse,
)
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse, "description": "Sin sesion valida"},
    403: {"model": ErrorResponse, "description": "CSRF invalido o acceso denegado"},
    502: {"model": ErrorResponse, "description": "Proveedor de identidad no disponible"},
}


@router.get(
    "/csrf",
    response_model=CsrfTokenResponse,
    summary="Emitir token CSRF",
)
async def issue_csrf_token(response: Response, settings: SettingsDep) -> CsrfTokenResponse:
    """Entrega un token CSRF y deja su copia en una cookie ``HttpOnly``.

    El frontend guarda el valor solo en memoria y lo envia en la cabecera
    ``X-CSRF-Token`` de cada operacion mutadora.
    """
    token = csrf.issue_token(settings)
    set_csrf_cookie(response, settings, token, settings.CSRF_TOKEN_TTL_SECONDS)
    return CsrfTokenResponse(csrf_token=token, expires_in=settings.CSRF_TOKEN_TTL_SECONDS)


@router.post(
    "/login",
    response_model=SessionResponse,
    responses=_ERROR_RESPONSES,
    summary="Iniciar sesion",
)
async def login(
    payload: LoginRequest,
    response: Response,
    settings: SettingsDep,
    supabase: SupabaseAuthDep,
    profiles: ProfileRepositoryDep,
) -> SessionResponse:
    """Autentica contra Supabase y abre la sesion mediante cookies."""
    session = await supabase.sign_in_with_password(
        email=payload.email,
        password=payload.password.get_secret_value(),
    )
    # El perfil se resuelve antes de emitir cookies: si el usuario no esta
    # habilitado en la aplicacion, no se abre sesion alguna.
    user = await resolve_profile(session.user_id, session.email or payload.email, profiles)

    set_session_cookies(
        response,
        settings,
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        access_max_age=session.expires_in,
    )
    # Se rota el token CSRF al abrir sesion para anular cualquier token que un
    # atacante hubiese fijado antes del login.
    set_csrf_cookie(response, settings, csrf.issue_token(settings), settings.CSRF_TOKEN_TTL_SECONDS)
    logger.info("Sesion iniciada para el usuario %s", user.id)
    return SessionResponse(user=user)


@router.post(
    "/refresh",
    response_model=SessionResponse,
    responses=_ERROR_RESPONSES,
    summary="Renovar sesion",
)
async def refresh(
    request: Request,
    response: Response,
    settings: SettingsDep,
    supabase: SupabaseAuthDep,
    profiles: ProfileRepositoryDep,
) -> SessionResponse | JSONResponse:
    """Renueva la sesion usando el refresh token guardado en la cookie.

    El refresh token nunca se devuelve al frontend: solo se reemplazan las
    cookies. Si Supabase lo rechaza, las cookies se limpian para que el cliente
    no reintente indefinidamente.
    """
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise AuthNotAuthenticatedError()

    try:
        session = await supabase.refresh_session(refresh_token)
    except AuthSessionExpiredError as exc:
        expired = JSONResponse(status_code=exc.status_code, content=exc.to_payload())
        clear_session_cookies(expired, settings)
        return expired

    user = await resolve_profile(session.user_id, session.email, profiles)
    set_session_cookies(
        response,
        settings,
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        access_max_age=session.expires_in,
    )
    return SessionResponse(user=user)


@router.post(
    "/logout",
    response_model=LogoutResponse,
    responses={403: _ERROR_RESPONSES[403]},
    summary="Cerrar sesion",
)
async def logout(
    request: Request,
    response: Response,
    settings: SettingsDep,
    supabase: SupabaseAuthDep,
) -> LogoutResponse:
    """Cierra la sesion y borra las cookies.

    La revocacion en Supabase es best-effort: aunque falle, las cookies se
    eliminan igual, de modo que ``/auth/me`` deja de autenticar al usuario.
    """
    access_token = request.cookies.get(ACCESS_COOKIE_NAME)
    if access_token:
        await supabase.sign_out(access_token)
    clear_session_cookies(response, settings)
    return LogoutResponse()


@router.get(
    "/me",
    response_model=SessionResponse,
    responses=_ERROR_RESPONSES,
    summary="Sesion actual",
)
async def me(user: CurrentUserDep) -> SessionResponse:
    """Unica fuente de verdad de la sesion para el frontend.

    React no decodifica ni interpreta ningun JWT: pregunta aqui.
    """
    return SessionResponse(user=user)
