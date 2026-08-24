"""Rutas REST de la consulta de identidad (DNI/RUC).

Cualquier usuario autenticado puede consultar: es una operacion de lectura
con sus propias protecciones internas (cuota por documento, cortocircuito por
proveedor). El refresco explicito —que gasta cuota externa a proposito— queda
reservado a ADMIN, siguiendo la misma linea que el resto de acciones que
cuestan dinero real en este proyecto.

El rol se comprueba dentro del handler y no delegando a otra funcion con su
propia dependencia de ADMIN: llamar a esa funcion directamente desde Python
no pasa por la resolucion de dependencias de FastAPI, asi que la comprobacion
de rol nunca se ejecutaria y cualquier usuario podria forzar un refresco con
``?refresh=true``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserDep, IdentityLookupServiceDep
from app.core.errors import AuthInsufficientRoleError
from app.models.profile import UserRole
from app.schemas.auth import AuthenticatedUser
from app.schemas.identity import DniLookupOut, RucLookupOut

router = APIRouter(prefix="/identity", tags=["identidad"])


def _require_admin_for_refresh(user: AuthenticatedUser, refresh: bool) -> None:
    if refresh and user.role is not UserRole.ADMIN:
        raise AuthInsufficientRoleError("Solo un administrador puede forzar un refresco")


@router.get("/dni/{dni}", response_model=DniLookupOut)
async def lookup_dni(
    dni: str,
    service: IdentityLookupServiceDep,
    user: CurrentUserDep,
    refresh: Annotated[bool, Query(description="Ignora la cache y vuelve a consultar")] = False,
) -> DniLookupOut:
    _require_admin_for_refresh(user, refresh)
    return await service.lookup_dni(dni, user=user, refresh=refresh)


@router.get("/ruc/{ruc}", response_model=RucLookupOut)
async def lookup_ruc(
    ruc: str,
    service: IdentityLookupServiceDep,
    user: CurrentUserDep,
    refresh: Annotated[bool, Query(description="Ignora la cache y vuelve a consultar")] = False,
) -> RucLookupOut:
    _require_admin_for_refresh(user, refresh)
    return await service.lookup_ruc(ruc, user=user, refresh=refresh)
