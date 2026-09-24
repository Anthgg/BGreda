"""API gates for retired Legacy creation and the staged compatibility flag."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import get_settings


def _disable_legacy_creation(app: FastAPI) -> None:
    current = app.dependency_overrides[get_settings]()
    app.dependency_overrides[get_settings] = lambda: current.model_copy(
        update={"LEGACY_CREATION_ENABLED": False}
    )


@pytest.mark.parametrize(
    ("path", "payload", "code"),
    [
        (
            "/api/v1/quotations",
            {"product_id": 1, "quantity": 1},
            "LEGACY_QUOTATION_CREATION_DISABLED",
        ),
        ("/api/v1/quotation-builder", {}, "LEGACY_QUOTATION_CREATION_DISABLED"),
        ("/api/v1/quotations/1/duplicate", None, "LEGACY_QUOTATION_CREATION_DISABLED"),
        ("/api/v1/quotation-builder/1/duplicate", None, "LEGACY_QUOTATION_CREATION_DISABLED"),
        ("/api/v1/firings", {"sessions": [], "lines": []}, "LEGACY_FIRING_CREATION_DISABLED"),
    ],
)
async def test_la_creacion_legacy_responde_409_con_codigo_estable(
    api: httpx.AsyncClient,
    api_app: FastAPI,
    admin_csrf: str,
    path: str,
    payload: Mapping[str, Any] | None,
    code: str,
) -> None:
    _disable_legacy_creation(api_app)
    response = await api.post(path, json=payload, headers={"X-CSRF-Token": admin_csrf})

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == code
