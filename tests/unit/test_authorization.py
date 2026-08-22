"""Autorizacion por rol y resolucion de perfiles."""

from __future__ import annotations

import uuid

import pytest

from app.api.deps import require_roles, resolve_profile
from app.core.errors import (
    AuthAccountInactiveError,
    AuthInsufficientRoleError,
    AuthProfileNotProvisionedError,
)
from app.models.profile import Profile, UserRole
from app.schemas.auth import AuthenticatedUser
from tests.fakes import FakeProfileRepository

USER_ID = uuid.UUID("99999999-8888-7777-6666-555555555555")


def _profile(*, role: UserRole = UserRole.OPERATOR, active: bool = True) -> Profile:
    profile = Profile()
    profile.id = USER_ID
    profile.display_name = "Operario"
    profile.role = role
    profile.active = active
    return profile


def _user(role: UserRole) -> AuthenticatedUser:
    return AuthenticatedUser(id=USER_ID, email="u@empresa.com", display_name="Operario", role=role)


# ---------------------------------------------------------------------------
# resolve_profile
# ---------------------------------------------------------------------------
async def test_sin_perfil_no_hay_acceso() -> None:
    with pytest.raises(AuthProfileNotProvisionedError):
        await resolve_profile(USER_ID, "u@empresa.com", FakeProfileRepository())


async def test_perfil_inactivo_no_tiene_acceso() -> None:
    repo = FakeProfileRepository({USER_ID: _profile(active=False)})

    with pytest.raises(AuthAccountInactiveError):
        await resolve_profile(USER_ID, "u@empresa.com", repo)


async def test_perfil_activo_resuelve_el_usuario() -> None:
    repo = FakeProfileRepository({USER_ID: _profile(role=UserRole.ADMIN)})

    usuario = await resolve_profile(USER_ID, "u@empresa.com", repo)

    assert usuario.id == USER_ID
    assert usuario.role is UserRole.ADMIN
    assert usuario.display_name == "Operario"


# ---------------------------------------------------------------------------
# require_roles
# ---------------------------------------------------------------------------
async def test_require_roles_admite_el_rol_esperado() -> None:
    dependencia = require_roles(UserRole.ADMIN)

    usuario = await dependencia(_user(UserRole.ADMIN))

    assert usuario.role is UserRole.ADMIN


async def test_require_roles_rechaza_un_rol_no_autorizado() -> None:
    dependencia = require_roles(UserRole.ADMIN)

    with pytest.raises(AuthInsufficientRoleError):
        await dependencia(_user(UserRole.OPERATOR))


async def test_require_roles_admite_varios_roles() -> None:
    dependencia = require_roles(UserRole.ADMIN, UserRole.OPERATOR)

    assert await dependencia(_user(UserRole.OPERATOR))
