"""Contexto publico de seguimiento: el token del QR, fuera de la barra de direcciones.

El QR de una hoja de taller lleva un token opaco de 43 caracteres. Ese token es
una llave: quien lo tiene puede consultar el estado de esa orden sin cuenta, que
es exactamente para lo que existe.

El problema no es la llave, es donde queda apoyada. Si la vista publica vive en
``/seguimiento/<token>``, el token acaba en la barra de direcciones, en el
historial del navegador, en el portapapeles de quien comparte el enlace y en los
registros de acceso del servidor. Un enlace que alguien reenvia «para que veas
como va» entrega el acceso permanente sin que nadie lo decida.

Asi que el token se cambia una sola vez por una cookie y desaparece de la URL:

    GET /api/v1/tracking/production-orders/scan/<token>
        -> valida el token
        -> Set-Cookie greda_tracking (HttpOnly, Secure, SameSite, Path acotado)
    la SPA reemplaza la URL por /seguimiento, sin token
    GET /api/v1/tracking/production-orders/current   <- la cookie hace el resto

La cookie **es** el token, movido a un sitio que JavaScript no puede leer y que
no se copia al reenviar un enlace. No se firma ni se cifra porque no anade nada:
el token ya es opaco, aleatorio y no derivado del identificador de la orden, y
firmarlo solo sumaria un secreto mas que rotar.

Dos limites deliberados:

- ``Path`` acotado a la superficie de seguimiento. La cookie no viaja con
  ninguna peticion interna, asi que no puede confundirse con una sesion.
- ``max_age`` de doce horas. **No es una caducidad del token** —el token no
  caduca y 009I.1 no inventa una regla de negocio que no existe— sino la
  duracion de esta consulta en este navegador. Pasadas doce horas se vuelve a
  escanear el papel, que es lo que se tiene en la mano.
"""

from __future__ import annotations

from fastapi import Request, Response

from app.core.config import Settings
from app.models.production import QR_TOKEN_LENGTH, QR_TOKEN_MIN_LENGTH

#: Nombre de la cookie del contexto publico.
TRACKING_COOKIE_NAME = "greda_tracking"

#: Ruta a la que queda acotada. Debe coincidir con el prefijo del router de
#: seguimiento: si se amplia, la llave empieza a viajar donde no hace falta.
TRACKING_COOKIE_PATH = "/api/v1/tracking"

#: Doce horas: un turno de taller. Ver la nota del modulo — esto NO caduca el
#: token, caduca esta consulta.
TRACKING_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60


def set_tracking_cookie(response: Response, settings: Settings, token: str) -> None:
    """Guarda el token del QR como contexto publico de este navegador."""
    kwargs: dict[str, object] = {
        "httponly": True,
        "secure": settings.COOKIE_SECURE,
        "samesite": settings.COOKIE_SAMESITE,
        "path": TRACKING_COOKIE_PATH,
        "max_age": TRACKING_COOKIE_MAX_AGE_SECONDS,
    }
    if settings.COOKIE_DOMAIN:
        kwargs["domain"] = settings.COOKIE_DOMAIN
    response.set_cookie(TRACKING_COOKIE_NAME, token, **kwargs)  # type: ignore[arg-type]


def clear_tracking_cookie(response: Response, settings: Settings) -> None:
    """Borra el contexto. Los atributos deben coincidir con los de escritura."""
    response.delete_cookie(
        TRACKING_COOKIE_NAME,
        path=TRACKING_COOKIE_PATH,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite=settings.COOKIE_SAMESITE,
    )


def read_tracking_token(request: Request) -> str | None:
    """Token del contexto publico, o None si no hay uno utilizable.

    Comprueba la forma antes de devolverlo. Un token con la longitud
    equivocada no puede corresponder a ninguna orden —el esquema exige entre
    32 y 64 caracteres— asi que se descarta aqui y no llega a la base: sin
    esto, cualquiera podria disparar consultas con cadenas de un megabyte
    contra un endpoint que no pide sesion.
    """
    return sanitize_qr_token(request.cookies.get(TRACKING_COOKIE_NAME))


def sanitize_qr_token(raw: str | None) -> str | None:
    """Descarta lo que no puede ser un token del QR, sin tocar la base."""
    if not raw:
        return None
    token = raw.strip()
    if not (QR_TOKEN_MIN_LENGTH <= len(token) <= QR_TOKEN_LENGTH):
        return None
    return token
