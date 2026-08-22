"""Acceso a los perfiles de aplicacion.

Se define un repositorio abstracto para que las pruebas unitarias puedan
sustituir el almacenamiento sin levantar PostgreSQL, y para aislar el resto de
la aplicacion de los detalles de SQLAlchemy.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile


class ProfileRepository(ABC):
    """Contrato de lectura de perfiles."""

    @abstractmethod
    async def get_by_id(self, user_id: uuid.UUID) -> Profile | None:
        """Devuelve el perfil del usuario indicado, o ``None`` si no existe."""


class SqlAlchemyProfileRepository(ProfileRepository):
    """Implementacion sobre PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> Profile | None:
        result = await self._session.execute(select(Profile).where(Profile.id == user_id))
        return result.scalar_one_or_none()
