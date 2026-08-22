"""Proteccion CSRF.

Se combinan dos mecanismos independientes:

1. **Token firmado**: cada token lleva un nonce, una expiracion y un HMAC-SHA256
   calculado con ``CSRF_SECRET``. El servidor puede validarlo sin estado.
2. **Double submit**: el mismo token viaja en una cookie ``HttpOnly`` y en la
   cabecera ``X-CSRF-Token``. Un sitio atacante puede provocar que el navegador
   envie la cookie, pero no puede leerla ni fijar la cabecera, de modo que la
   comparacion falla.

El frontend obtiene el token desde ``GET /api/v1/auth/csrf`` y lo mantiene en
memoria. No necesita —ni puede— leer la cookie.
"""

from __future__ import annotations

import hmac
import secrets
import time
from hashlib import sha256

from app.core.config import Settings
from app.core.errors import CsrfTokenInvalidError, CsrfTokenMissingError

#: Cabecera que transporta el token en operaciones mutadoras.
CSRF_HEADER_NAME = "X-CSRF-Token"

#: Metodos HTTP que exigen token CSRF.
PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_SEPARATOR = "."
_NONCE_BYTES = 32


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()


def issue_token(settings: Settings) -> str:
    """Genera un token CSRF firmado y con expiracion."""
    nonce = secrets.token_urlsafe(_NONCE_BYTES)
    expires_at = int(time.time()) + settings.CSRF_TOKEN_TTL_SECONDS
    payload = f"{nonce}{_SEPARATOR}{expires_at}"
    signature = _sign(settings.CSRF_SECRET.get_secret_value(), payload)
    return f"{payload}{_SEPARATOR}{signature}"


def is_token_valid(settings: Settings, token: str, *, now: int | None = None) -> bool:
    """Comprueba firma y vigencia de un token, sin estado en servidor."""
    parts = token.split(_SEPARATOR)
    if len(parts) != 3:
        return False
    nonce, raw_expiry, signature = parts
    if not nonce:
        return False
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False

    current = int(time.time()) if now is None else now
    if expires_at <= current:
        return False

    expected = _sign(settings.CSRF_SECRET.get_secret_value(), f"{nonce}{_SEPARATOR}{raw_expiry}")
    return hmac.compare_digest(expected, signature)


def validate(settings: Settings, *, header_token: str | None, cookie_token: str | None) -> None:
    """Valida una peticion mutadora. Lanza ``APIError`` si no supera el control."""
    if not header_token or not cookie_token:
        raise CsrfTokenMissingError()
    # compare_digest evita filtrar informacion por tiempo de comparacion.
    if not hmac.compare_digest(header_token, cookie_token):
        raise CsrfTokenInvalidError()
    if not is_token_valid(settings, header_token):
        raise CsrfTokenInvalidError()
