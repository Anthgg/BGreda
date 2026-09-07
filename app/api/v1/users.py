"""Administracion de usuarios. Solo ADMIN (Fase 009K.2).

La proteccion vive aqui, en `AdminUserDep`, y no en que el frontend esconda un
boton: esconder es maquillaje, y una peticion directa lo atraviesa. Es la misma
autoridad de rol que ya usan Configuracion y los maestros.

No hay borrado. Un usuario que ya firmo documentos no se puede quitar sin
romper el historial, asi que la baja es `active = false`: deja de entrar y
sigue apareciendo en lo que hizo.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import AdminUserDep, DbSessionDep, UserServiceDep
from app.schemas.users import UserCreateIn, UserOut, UserPage, UserUpdateIn

router = APIRouter(prefix="/users", tags=["usuarios"])


@router.get("", response_model=UserPage)
async def list_users(service: UserServiceDep, actor: AdminUserDep) -> UserPage:
    return await service.list_users()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateIn,
    service: UserServiceDep,
    actor: AdminUserDep,
    session: DbSessionDep,
) -> UserOut:
    resultado = await service.create(payload, user=actor)
    await session.commit()
    return resultado


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateIn,
    service: UserServiceDep,
    actor: AdminUserDep,
    session: DbSessionDep,
) -> UserOut:
    resultado = await service.update(user_id, payload, user=actor)
    await session.commit()
    return resultado


@router.post("/{user_id}/disable", response_model=UserOut)
async def disable_user(
    user_id: uuid.UUID,
    service: UserServiceDep,
    actor: AdminUserDep,
    session: DbSessionDep,
) -> UserOut:
    resultado = await service.set_active(user_id, active=False, user=actor)
    await session.commit()
    return resultado


@router.post("/{user_id}/enable", response_model=UserOut)
async def enable_user(
    user_id: uuid.UUID,
    service: UserServiceDep,
    actor: AdminUserDep,
    session: DbSessionDep,
) -> UserOut:
    resultado = await service.set_active(user_id, active=True, user=actor)
    await session.commit()
    return resultado
