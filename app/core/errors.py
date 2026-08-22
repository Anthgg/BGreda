"""Errores de dominio y formato uniforme de respuesta de la API.

Toda respuesta de error de BGreda tiene exactamente esta forma::

    {"error": {"code": "AUTH_INVALID_CREDENTIALS", "message": "Credenciales invalidas"}}

Nunca se devuelven trazas de pila, mensajes de excepciones internas ni valores
enviados por el cliente (que podrian incluir contrasenas).
"""

from __future__ import annotations

from typing import Any


class APIError(Exception):
    """Error de aplicacion traducible a una respuesta HTTP uniforme."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "Ocurrio un error inesperado"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        self.headers = headers
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        """Serializa el error al contrato publico de la API."""
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


# ---------------------------------------------------------------------------
# Autenticacion / sesion
# ---------------------------------------------------------------------------
class AuthInvalidCredentialsError(APIError):
    status_code = 401
    code = "AUTH_INVALID_CREDENTIALS"
    message = "Credenciales invalidas"


class AuthNotAuthenticatedError(APIError):
    status_code = 401
    code = "AUTH_NOT_AUTHENTICATED"
    message = "No hay una sesion activa"


class AuthSessionExpiredError(APIError):
    status_code = 401
    code = "AUTH_SESSION_EXPIRED"
    message = "La sesion expiro o ya no es valida"


class AuthProfileNotProvisionedError(APIError):
    """El usuario existe en Supabase pero no tiene perfil en la aplicacion.

    Es la barrera que sustituye al registro publico: sin perfil aprovisionado
    no hay acceso, aunque las credenciales de Supabase sean correctas.
    """

    status_code = 403
    code = "AUTH_PROFILE_NOT_PROVISIONED"
    message = "El usuario no tiene un perfil habilitado en la aplicacion"


class AuthAccountInactiveError(APIError):
    status_code = 403
    code = "AUTH_ACCOUNT_INACTIVE"
    message = "La cuenta esta desactivada"


class AuthInsufficientRoleError(APIError):
    status_code = 403
    code = "AUTH_INSUFFICIENT_ROLE"
    message = "El rol actual no permite realizar esta operacion"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
class CsrfTokenMissingError(APIError):
    status_code = 403
    code = "CSRF_TOKEN_MISSING"
    message = "Falta el token CSRF"


class CsrfTokenInvalidError(APIError):
    status_code = 403
    code = "CSRF_TOKEN_INVALID"
    message = "El token CSRF es invalido o expiro"


# ---------------------------------------------------------------------------
# Validacion y dependencias externas
# ---------------------------------------------------------------------------
class ValidationFailedError(APIError):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "Los datos enviados no son validos"


class ServiceUnavailableError(APIError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"
    message = "Una dependencia requerida no esta disponible"


class UpstreamAuthError(APIError):
    """Supabase respondio de forma inesperada o es inalcanzable."""

    status_code = 502
    code = "UPSTREAM_AUTH_ERROR"
    message = "El proveedor de identidad no esta disponible"
