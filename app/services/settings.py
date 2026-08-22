"""Lectura y escritura de la configuracion de empresa y comercial.

Dos garantias transversales:

- **Concurrencia optimista.** Cada escritura declara la version que leyo. Si
  otro administrador guardo entretanto, la operacion se rechaza con 409 en vez
  de pisar en silencio el trabajo ajeno.
- **Auditoria.** Todo cambio queda registrado campo a campo en la misma
  transaccion, de modo que historial y dato nunca divergen.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.models.settings import SINGLETON_ID, BankAccount, CommercialSettings, CompanySettings
from app.schemas.auth import AuthenticatedUser
from app.schemas.settings import (
    BankAccountBase,
    CommercialSettingsUpdate,
    CompanySettingsUpdate,
)
from app.services.audit import AuditRecorder, diff_model

ENTITY_COMPANY = "company_settings"
ENTITY_COMMERCIAL = "commercial_settings"
ENTITY_BANK_ACCOUNT = "bank_account"


class SettingsNotInitializedError(APIError):
    status_code = 500
    code = "SETTINGS_NOT_INITIALIZED"
    message = "La configuracion no fue inicializada por la migracion"


class SettingsVersionConflictError(APIError):
    status_code = 409
    code = "SETTINGS_VERSION_CONFLICT"
    message = (
        "La configuracion fue modificada por otra persona mientras editaba. "
        "Vuelva a cargarla para no perder ese cambio."
    )


def _editable_fields(model: type[Any]) -> list[str]:
    """Campos de negocio de un esquema, excluidos los de control."""
    return [name for name in model.model_fields if name not in {"version", "bank_account"}]


class SettingsService:
    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------
    # Empresa
    # ------------------------------------------------------------------
    async def get_company(self) -> CompanySettings:
        result = await self._session.execute(
            select(CompanySettings).where(CompanySettings.id == SINGLETON_ID)
        )
        settings = result.scalar_one_or_none()
        if settings is None:
            raise SettingsNotInitializedError()
        return settings

    async def update_company(
        self,
        payload: CompanySettingsUpdate,
        user: AuthenticatedUser,
    ) -> CompanySettings:
        settings = await self.get_company()
        self._check_version(settings.version, payload.version)

        incoming = payload.model_dump(exclude={"version"})
        fields = _editable_fields(CompanySettingsUpdate)
        changes = diff_model(settings, incoming, fields)

        for field, (_, new) in changes.items():
            setattr(settings, field, new)

        if changes:
            settings.version += 1
            self._audit.record_changes(
                entity_type=ENTITY_COMPANY,
                entity_id=str(settings.id),
                changes=changes,
                user_id=user.id,
                user_display_name=user.display_name,
            )
        return settings

    # ------------------------------------------------------------------
    # Comercial
    # ------------------------------------------------------------------
    async def get_commercial(self) -> CommercialSettings:
        result = await self._session.execute(
            select(CommercialSettings)
            .where(CommercialSettings.id == SINGLETON_ID)
            .options(selectinload(CommercialSettings.bank_accounts))
        )
        settings = result.scalar_one_or_none()
        if settings is None:
            raise SettingsNotInitializedError()
        return settings

    async def update_commercial(
        self,
        payload: CommercialSettingsUpdate,
        user: AuthenticatedUser,
    ) -> CommercialSettings:
        settings = await self.get_commercial()
        self._check_version(settings.version, payload.version)

        incoming = payload.model_dump(exclude={"version", "bank_account"})
        fields = _editable_fields(CommercialSettingsUpdate)
        changes = diff_model(settings, incoming, fields)

        for field, (_, new) in changes.items():
            setattr(settings, field, new)

        bank_changed = await self._apply_primary_bank_account(settings, payload.bank_account, user)

        if changes or bank_changed:
            settings.version += 1
        if changes:
            self._audit.record_changes(
                entity_type=ENTITY_COMMERCIAL,
                entity_id=str(settings.id),
                changes=changes,
                user_id=user.id,
                user_display_name=user.display_name,
            )
        return settings

    async def _apply_primary_bank_account(
        self,
        settings: CommercialSettings,
        payload: BankAccountBase | None,
        user: AuthenticatedUser,
    ) -> bool:
        """Sincroniza la cuenta bancaria principal.

        La Fase 2 gestiona una unica cuenta. El modelo admite varias, de modo
        que anadir la segunda no exigira migrar el esquema.
        """
        if payload is None:
            return False

        primary = next((a for a in settings.bank_accounts if a.is_primary), None)
        fields = list(BankAccountBase.model_fields)
        incoming = payload.model_dump()

        if primary is None:
            primary = BankAccount(settings_id=settings.id, is_primary=True, **incoming)
            self._session.add(primary)
            settings.bank_accounts.append(primary)
            self._audit.record_changes(
                entity_type=ENTITY_BANK_ACCOUNT,
                entity_id="primary",
                changes={field: (None, incoming[field]) for field in fields if incoming[field]},
                user_id=user.id,
                user_display_name=user.display_name,
            )
            return True

        changes = diff_model(primary, incoming, fields)
        for field, (_, new) in changes.items():
            setattr(primary, field, new)
        if changes:
            self._audit.record_changes(
                entity_type=ENTITY_BANK_ACCOUNT,
                entity_id=str(primary.id),
                changes=changes,
                user_id=user.id,
                user_display_name=user.display_name,
            )
        return bool(changes)

    # ------------------------------------------------------------------
    @staticmethod
    def _check_version(stored: int, provided: int) -> None:
        if stored != provided:
            raise SettingsVersionConflictError()
