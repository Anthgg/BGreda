from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.users import UserUpdateIn


def test_admin_user_update_accepts_only_quick_create_capability() -> None:
    payload = UserUpdateIn(capabilities=["MASTERS_QUICK_CREATE"])
    assert payload.capabilities == ["MASTERS_QUICK_CREATE"]


def test_admin_user_update_can_revoke_quick_create_capability() -> None:
    payload = UserUpdateIn(capabilities=[])
    assert payload.capabilities == []


def test_admin_user_update_rejects_unknown_capability() -> None:
    with pytest.raises(ValidationError):
        UserUpdateIn(capabilities=["ADMIN"])
