"""Cliente de Supabase Auth (GoTrue).

Supabase se usa **exclusivamente** desde el backend. El frontend nunca recibe
la URL del proyecto, la publishable key ni los tokens emitidos: solo obtiene
cookies HttpOnly y datos de perfil ya resueltos.

Se habla con la API REST de GoTrue mediante ``httpx`` en lugar de instalar el
SDK completo, para mantener la superficie de dependencias pequena y el control
total sobre el manejo de errores.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import (
    AuthInvalidCredentialsError,
    AuthSessionExpiredError,
    ServiceUnavailableError,
    UpstreamAuthError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SupabaseSession:
    """Sesion emitida por Supabase. Nunca se serializa hacia el cliente."""

    access_token: str
    refresh_token: str
    expires_in: int
    user_id: uuid.UUID
    email: str


@dataclass(frozen=True, slots=True)
class SupabaseUser:
    """Identidad verificada de un access token."""

    id: uuid.UUID
    email: str


class SupabaseAuthClient(ABC):
    """Contrato del proveedor de identidad.

    Permite sustituir la implementacion HTTP por un doble en las pruebas
    unitarias, de modo que la suite no dependa de la red.
    """

    @abstractmethod
    async def sign_in_with_password(self, email: str, password: str) -> SupabaseSession: ...

    @abstractmethod
    async def refresh_session(self, refresh_token: str) -> SupabaseSession: ...

    @abstractmethod
    async def get_user(self, access_token: str) -> SupabaseUser: ...

    @abstractmethod
    async def sign_out(self, access_token: str) -> None: ...


class HttpSupabaseAuthClient(SupabaseAuthClient):
    """Implementacion contra la API REST de GoTrue."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.supabase_configured:
            raise ServiceUnavailableError(
                "Supabase no esta configurado",
                code="SUPABASE_NOT_CONFIGURED",
            )
        self._base_url = f"{settings.SUPABASE_URL}/auth/v1"
        self._apikey = settings.SUPABASE_PUBLISHABLE_KEY.get_secret_value()
        self._client = client or httpx.AsyncClient(timeout=settings.SUPABASE_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Operaciones
    # ------------------------------------------------------------------
    async def sign_in_with_password(self, email: str, password: str) -> SupabaseSession:
        response = await self._post(
            "/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        if response.status_code in (400, 401, 403, 422):
            # GoTrue distingue varios motivos (credenciales, email sin
            # confirmar, usuario bloqueado). Se responde siempre igual para no
            # habilitar enumeracion de cuentas.
            raise AuthInvalidCredentialsError()
        return self._parse_session(self._require_ok(response))

    async def refresh_session(self, refresh_token: str) -> SupabaseSession:
        response = await self._post(
            "/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        if response.status_code in (400, 401, 403, 422):
            raise AuthSessionExpiredError()
        return self._parse_session(self._require_ok(response))

    async def get_user(self, access_token: str) -> SupabaseUser:
        try:
            response = await self._client.get(
                f"{self._base_url}/user",
                headers=self._headers(access_token),
            )
        except httpx.HTTPError as exc:
            raise self._upstream_error("get_user", exc) from exc

        if response.status_code in (400, 401, 403):
            raise AuthSessionExpiredError()
        payload = self._json(self._require_ok(response))
        return SupabaseUser(
            id=self._parse_uuid(payload.get("id")),
            email=str(payload.get("email") or ""),
        )

    async def sign_out(self, access_token: str) -> None:
        """Revoca la sesion en Supabase.

        Un fallo aqui no debe impedir el logout local: las cookies se borran de
        todas formas, de modo que el error se registra y se absorbe.
        """
        try:
            response = await self._client.post(
                f"{self._base_url}/logout",
                headers=self._headers(access_token),
            )
        except httpx.HTTPError:
            logger.warning("No se pudo revocar la sesion en Supabase; se continua con el logout")
            return
        if response.status_code >= 400:
            logger.warning(
                "Supabase rechazo la revocacion de sesion (status=%s); se continua con el logout",
                response.status_code,
            )

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------
    def _headers(self, access_token: str | None = None) -> dict[str, str]:
        headers = {"apikey": self._apikey, "Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    async def _post(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return await self._client.post(
                f"{self._base_url}{path}",
                params=params,
                json=json,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise self._upstream_error(path, exc) from exc

    def _upstream_error(self, operation: str, exc: Exception) -> UpstreamAuthError:
        # Se registra el tipo de fallo, nunca la URL completa ni la apikey.
        logger.error("Fallo de comunicacion con Supabase en %s: %s", operation, type(exc).__name__)
        return UpstreamAuthError()

    def _require_ok(self, response: httpx.Response) -> httpx.Response:
        if response.status_code >= 400:
            logger.error("Supabase respondio status=%s inesperado", response.status_code)
            raise UpstreamAuthError()
        return response

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("Supabase devolvio un cuerpo no JSON")
            raise UpstreamAuthError() from exc
        if not isinstance(payload, dict):
            raise UpstreamAuthError()
        return payload

    def _parse_uuid(self, raw: Any) -> uuid.UUID:
        try:
            return uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError) as exc:
            logger.error("Supabase devolvio un identificador de usuario no valido")
            raise UpstreamAuthError() from exc

    def _parse_session(self, response: httpx.Response) -> SupabaseSession:
        payload = self._json(response)
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        user = payload.get("user")
        if not access_token or not refresh_token or not isinstance(user, dict):
            logger.error("Supabase devolvio una sesion incompleta")
            raise UpstreamAuthError()
        return SupabaseSession(
            access_token=str(access_token),
            refresh_token=str(refresh_token),
            expires_in=int(payload.get("expires_in") or 3600),
            user_id=self._parse_uuid(user.get("id")),
            email=str(user.get("email") or ""),
        )
