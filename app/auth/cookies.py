"""Gestion de las cookies de sesion.

Los tokens de Supabase viven unicamente en cookies ``HttpOnly``: JavaScript no
puede leerlos, por lo que un XSS en el frontend no permite exfiltrar la sesion.
Los atributos ``Secure``, ``SameSite`` y ``Domain`` son configurables por
entorno y nunca se debilitan en silencio.
"""

from __future__ import annotations

from fastapi import Response

from app.core.config import Settings

#: Access token de Supabase.
ACCESS_COOKIE_NAME = "greda_access"
#: Refresh token de Supabase. Nunca sale del backend.
REFRESH_COOKIE_NAME = "greda_refresh"
#: Copia del token CSRF para la validacion double-submit.
CSRF_COOKIE_NAME = "greda_csrf"

COOKIE_PATH = "/"


def _base_kwargs(settings: Settings) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "httponly": True,
        "secure": settings.COOKIE_SECURE,
        "samesite": settings.COOKIE_SAMESITE,
        "path": COOKIE_PATH,
    }
    if settings.COOKIE_DOMAIN:
        kwargs["domain"] = settings.COOKIE_DOMAIN
    return kwargs


def set_session_cookies(
    response: Response,
    settings: Settings,
    *,
    access_token: str,
    refresh_token: str,
    access_max_age: int,
) -> None:
    """Escribe las cookies de sesion tras un login o un refresh."""
    base = _base_kwargs(settings)
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        max_age=access_max_age,
        **base,  # type: ignore[arg-type]
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=settings.REFRESH_COOKIE_MAX_AGE_SECONDS,
        **base,  # type: ignore[arg-type]
    )


def set_csrf_cookie(response: Response, settings: Settings, token: str, max_age: int) -> None:
    """Escribe la cookie CSRF.

    Tambien es ``HttpOnly``: el frontend recibe el token en el cuerpo de
    ``GET /auth/csrf`` y lo guarda en memoria, de modo que nunca necesita leer
    la cookie. Asi ningun script de terceros puede recuperarlo.
    """
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=max_age,
        **_base_kwargs(settings),  # type: ignore[arg-type]
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    """Elimina las cookies de sesion.

    Los atributos deben coincidir con los usados al escribirlas; de lo
    contrario el navegador conserva la cookie original.
    """
    base = _base_kwargs(settings)
    for name in (ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME):
        response.delete_cookie(
            name,
            path=COOKIE_PATH,
            domain=settings.COOKIE_DOMAIN,
            secure=bool(base["secure"]),
            httponly=True,
            samesite=settings.COOKIE_SAMESITE,
        )
