"""Dobles de prueba para las dependencias externas."""

from __future__ import annotations

import itertools
import uuid

from app.core.errors import AuthInvalidCredentialsError, AuthSessionExpiredError
from app.models.profile import Profile
from app.services.profiles import ProfileRepository
from app.services.supabase_auth import (
    SupabaseAuthClient,
    SupabaseSession,
    SupabaseUser,
    SupabaseUserAlreadyExistsError,
)


class FakeSupabaseAuthClient(SupabaseAuthClient):
    """Reproduce el comportamiento observable de Supabase Auth, sin red."""

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._identities: dict[str, tuple[uuid.UUID, str]] = {}
        self._access_tokens: dict[str, str] = {}
        self._refresh_tokens: dict[str, str] = {}
        self._counter = itertools.count(1)
        self.sign_out_calls: list[str] = []
        #: Rastro de las operaciones de administracion, para poder afirmar que
        #: la compensacion de un alta a medias se ejecuto de verdad.
        self.admin_created: list[uuid.UUID] = []
        self.admin_deleted: list[uuid.UUID] = []
        self.fallos: set[str] = set()

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

    # -- administracion (Fase 009K.2) ------------------------------------
    #
    # El doble se comporta como GoTrue en lo que importa: rechaza correos
    # repetidos, no conoce cuentas que no ha creado, y permite provocar fallos
    # de red a voluntad para poder probar la compensacion del alta.
    async def admin_list_users(self) -> list[SupabaseUser]:
        self._fallar_si_toca("admin_list_users")
        return [SupabaseUser(id=uid, email=correo) for uid, correo in self._identities.values()]

    async def admin_get_user(self, user_id: uuid.UUID) -> SupabaseUser | None:
        self._fallar_si_toca("admin_get_user")
        for uid, correo in self._identities.values():
            if uid == user_id:
                return SupabaseUser(id=uid, email=correo)
        return None

    async def admin_create_user(self, email: str, password: str) -> SupabaseUser:
        self._fallar_si_toca("admin_create_user")
        if email in self._identities:
            raise SupabaseUserAlreadyExistsError()
        user_id = uuid.uuid4()
        self.register(email=email, password=password, user_id=user_id)
        self.admin_created.append(user_id)
        return SupabaseUser(id=user_id, email=email)

    async def admin_delete_user(self, user_id: uuid.UUID) -> None:
        self._fallar_si_toca("admin_delete_user")
        self.admin_deleted.append(user_id)
        for correo, (uid, _) in list(self._identities.items()):
            if uid == user_id:
                del self._identities[correo]
                self._credentials.pop(correo, None)

    def fallar_en(self, operacion: str) -> None:
        """Hace que la siguiente llamada a esa operacion reviente."""
        self.fallos.add(operacion)

    def _fallar_si_toca(self, operacion: str) -> None:
        if operacion in self.fallos:
            raise RuntimeError(f"fallo inyectado en {operacion}")

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
