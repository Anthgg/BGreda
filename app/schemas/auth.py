"""Esquemas de autenticacion.

Ningun esquema de salida incluye access token, refresh token ni ningun otro
material de sesion: los tokens de Supabase viajan exclusivamente en cookies
HttpOnly y son inaccesibles para JavaScript.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr

from app.models.profile import UserRole


class LoginRequest(BaseModel):
    """Credenciales enviadas por el frontend."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    # SecretStr evita que la contrasena aparezca en un repr, un log o una traza.
    # El limite superior acota el costo de hashing del proveedor de identidad.
    password: SecretStr = Field(min_length=1, max_length=256)


class AuthenticatedUser(BaseModel):
    """Datos de usuario que el frontend puede conocer."""

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: UserRole


class SessionResponse(BaseModel):
    """Respuesta de ``/auth/login``, ``/auth/refresh`` y ``/auth/me``."""

    authenticated: Literal[True] = True
    user: AuthenticatedUser


class LogoutResponse(BaseModel):
    authenticated: Literal[False] = False


class CsrfTokenResponse(BaseModel):
    """Token CSRF que el frontend mantiene solo en memoria."""

    csrf_token: str
    expires_in: int = Field(description="Vigencia del token en segundos")
