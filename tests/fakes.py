"""Dobles de prueba para las dependencias externas."""

from __future__ import annotations

import itertools
import uuid

from app.core.errors import AuthInvalidCredentialsError, AuthSessionExpiredError
from app.models.profile import Profile
from app.services.profiles import ProfileRepository
from app.services.supabase_auth import SupabaseAuthClient, SupabaseSession, SupabaseUser


class FakeSupabaseAuthClient(SupabaseAuthClient):
    """Reproduce el comportamiento observable de Supabase Auth, sin red."""

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._identities: dict[str, tuple[uuid.UUID, str]] = {}
        self._access_tokens: dict[str, str] = {}
        self._refresh_tokens: dict[str, str] = {}
        self._counter = itertools.count(1)
        self.sign_out_calls: list[str] = []

    # -- utilidades de configuracion del doble ---------------------------
    def register(self, *, email: str, password: str, user_id: uuid.UUID) -> None:
        self._credentials[email] = password
        self._identities[email] = (user_id, email)

    def revoke_access_token(self, token: str) -> None:
        self._access_tokens.pop(token, None)

    def revoke_all(self) -> None:
        self._access_tokens.clear()
        self._refresh_tokens.clear()

    # -- contrato --------------------------------------------------------
    async def sign_in_with_password(self, email: str, password: str) -> SupabaseSession:
        if self._credentials.get(email) != password:
            raise AuthInvalidCredentialsError()
        return self._issue(email)

    async def refresh_session(self, refresh_token: str) -> SupabaseSession:
        email = self._refresh_tokens.pop(refresh_token, None)
        if email is None:
            raise AuthSessionExpiredError()
        return self._issue(email)

    async def get_user(self, access_token: str) -> SupabaseUser:
        email = self._access_tokens.get(access_token)
        if email is None:
            raise AuthSessionExpiredError()
        user_id, resolved_email = self._identities[email]
        return SupabaseUser(id=user_id, email=resolved_email)

    async def sign_out(self, access_token: str) -> None:
        self.sign_out_calls.append(access_token)
        email = self._access_tokens.pop(access_token, None)
        if email is not None:
            for token, owner in list(self._refresh_tokens.items()):
                if owner == email:
                    del self._refresh_tokens[token]

    # -- interno ---------------------------------------------------------
    def _issue(self, email: str) -> SupabaseSession:
        index = next(self._counter)
        access_token = f"fake-access-token-{index}"
        refresh_token = f"fake-refresh-token-{index}"
        self._access_tokens[access_token] = email
        self._refresh_tokens[refresh_token] = email
        user_id, resolved_email = self._identities[email]
        return SupabaseSession(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=3600,
            user_id=user_id,
            email=resolved_email,
        )


class FakeProfileRepository(ProfileRepository):
    """Repositorio de perfiles en memoria."""

    def __init__(self, profiles: dict[uuid.UUID, Profile] | None = None) -> None:
        self.profiles: dict[uuid.UUID, Profile] = dict(profiles or {})

    async def get_by_id(self, user_id: uuid.UUID) -> Profile | None:
        return self.profiles.get(user_id)
