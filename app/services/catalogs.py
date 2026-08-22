"""Lectura y alta de catalogos controlados de configuracion."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.catalog import CurrencyCatalog, SequencePatternPreset, UbigeoDistrict
from app.schemas.auth import AuthenticatedUser
from app.schemas.catalog import SequencePatternPresetCreate
from app.services.audit import AuditRecorder


class CatalogConflictError(APIError):
    status_code = 409
    code = "CATALOG_VALUE_EXISTS"
    message = "Ya existe un formato con ese nombre o patron"


class CatalogService:
    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    async def list_currencies(self) -> list[CurrencyCatalog]:
        result = await self._session.execute(select(CurrencyCatalog).order_by(CurrencyCatalog.code))
        return list(result.scalars().all())

    async def list_districts(self) -> list[UbigeoDistrict]:
        result = await self._session.execute(
            select(UbigeoDistrict).order_by(
                UbigeoDistrict.department_name,
                UbigeoDistrict.province_name,
                UbigeoDistrict.district_name,
            )
        )
        return list(result.scalars().all())

    async def list_sequence_patterns(self) -> list[SequencePatternPreset]:
        result = await self._session.execute(
            select(SequencePatternPreset).order_by(
                SequencePatternPreset.is_system.desc(),
                SequencePatternPreset.name,
            )
        )
        return list(result.scalars().all())

    async def create_sequence_pattern(
        self,
        payload: SequencePatternPresetCreate,
        user: AuthenticatedUser,
    ) -> SequencePatternPreset:
        existing = await self._session.execute(
            select(SequencePatternPreset.id).where(
                or_(
                    SequencePatternPreset.name == payload.name,
                    SequencePatternPreset.pattern == payload.pattern,
                )
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise CatalogConflictError()

        preset = SequencePatternPreset(
            name=payload.name,
            pattern=payload.pattern,
            is_system=False,
        )
        self._session.add(preset)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise CatalogConflictError() from exc

        self._audit.record_action(
            entity_type="sequence_pattern_preset",
            entity_id=str(preset.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"name": preset.name, "pattern": preset.pattern},
        )
        return preset
