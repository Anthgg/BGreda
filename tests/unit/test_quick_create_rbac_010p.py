from __future__ import annotations

import uuid

import pytest

from app.api.deps import require_masters_quick_create
from app.core.errors import AuthInsufficientRoleError
from app.models.profile import UserRole
from app.schemas.auth import AuthenticatedUser


def _user(role: UserRole, capabilities: list[str] | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=uuid.uuid4(),
        email="operator@greda.pe",
        display_name="Operator",
        role=role,
        capabilities=capabilities or [],
    )


@pytest.mark.asyncio
async def test_admin_always_has_quick_create_access() -> None:
    user = _user(UserRole.ADMIN)
    assert await require_masters_quick_create()(user) is user


@pytest.mark.asyncio
async def test_operator_without_quick_create_capability_is_forbidden() -> None:
    with pytest.raises(AuthInsufficientRoleError):
        await require_masters_quick_create()(_user(UserRole.OPERATOR))


@pytest.mark.asyncio
async def test_operator_with_quick_create_capability_is_allowed() -> None:
    user = _user(UserRole.OPERATOR, ["MASTERS_QUICK_CREATE"])
    assert await require_masters_quick_create()(user) is user
