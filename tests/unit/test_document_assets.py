"""El logo configurado pertenece al sistema documental, no a Cotización."""

from __future__ import annotations

import base64

import pytest

from app.models.settings import CompanySettings
from app.services.document_assets import resolve_company_logo_data_uri
from app.services.storage import ObjectStorage


class MemoryStorage(ObjectStorage):
    def __init__(self, content: bytes | None = None, *, fails: bool = False) -> None:
        self.content = content
        self.fails = fails

    async def upload(self, path: str, content: bytes, content_type: str) -> None:
        raise NotImplementedError

    async def download(self, path: str) -> bytes:
        if self.fails:
            raise RuntimeError("storage unavailable")
        return self.content or b""

    async def delete(self, path: str) -> None:
        raise NotImplementedError


def _company() -> CompanySettings:
    return CompanySettings(
        id=1,
        logo_object_path="company/logo.png",
        logo_content_type="image/png",
    )


@pytest.mark.asyncio
async def test_el_logo_configurado_se_convierte_en_data_uri_compartido() -> None:
    content = b"\x89PNG\r\n\x1a\nconfigured-logo"

    uri = await resolve_company_logo_data_uri(_company(), MemoryStorage(content))

    assert uri == f"data:image/png;base64,{base64.b64encode(content).decode('ascii')}"


@pytest.mark.asyncio
async def test_un_fallo_del_logo_no_impide_emitir_documentos() -> None:
    assert await resolve_company_logo_data_uri(_company(), MemoryStorage(fails=True)) is None
