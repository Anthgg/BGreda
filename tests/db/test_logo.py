"""Subida, lectura y borrado del logo."""

from __future__ import annotations

import httpx

from tests.db.fakes import FakeObjectStorage

LOGO = "/api/v1/settings/company/logo"
COMPANY = "/api/v1/settings/company"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 128
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _file(data: bytes, name: str, tipo: str) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, tipo)}


# ---------------------------------------------------------------------------
# Subida
# ---------------------------------------------------------------------------
async def test_admin_sube_el_logo(
    api: httpx.AsyncClient, admin_csrf: str, storage: FakeObjectStorage
) -> None:
    response = await api.post(
        LOGO, files=_file(PNG, "logo.png", "image/png"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 200, response.text
    logo = response.json()["logo"]
    assert logo["content_type"] == "image/png"
    assert logo["size_bytes"] == len(PNG)
    # El frontend recibe una ruta del backend, nunca una URL de Storage.
    assert logo["url"] == LOGO
    assert len(storage.uploads) == 1


async def test_la_ruta_interna_no_deriva_del_nombre_enviado(
    api: httpx.AsyncClient, admin_csrf: str, storage: FakeObjectStorage
) -> None:
    await api.post(
        LOGO,
        files=_file(PNG, "../../../etc/passwd.png", "image/png"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    ruta = storage.uploads[0]
    assert ".." not in ruta
    assert "passwd" not in ruta
    assert ruta.startswith("company/logo-")


async def test_operator_no_puede_subir_logo(
    api: httpx.AsyncClient, operator_csrf: str, storage: FakeObjectStorage
) -> None:
    response = await api.post(
        LOGO, files=_file(PNG, "logo.png", "image/png"), headers={"X-CSRF-Token": operator_csrf}
    )

    assert response.status_code == 403
    assert storage.uploads == []


async def test_sin_csrf_no_se_puede_subir(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.post(LOGO, files=_file(PNG, "logo.png", "image/png"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_MISSING"


# ---------------------------------------------------------------------------
# Formatos rechazados
# ---------------------------------------------------------------------------
async def test_se_rechaza_svg(
    api: httpx.AsyncClient, admin_csrf: str, storage: FakeObjectStorage
) -> None:
    response = await api.post(
        LOGO, files=_file(SVG, "logo.svg", "image/svg+xml"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LOGO_TYPE_NOT_ALLOWED"
    assert storage.uploads == []


async def test_se_rechaza_un_archivo_vacio(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.post(
        LOGO, files=_file(b"", "logo.png", "image/png"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LOGO_EMPTY"


async def test_se_rechaza_un_ejecutable_renombrado(
    api: httpx.AsyncClient, admin_csrf: str, storage: FakeObjectStorage
) -> None:
    response = await api.post(
        LOGO,
        files=_file(b"MZ\x90\x00" + b"\x00" * 64, "logo.png", "image/png"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422
    assert storage.uploads == []


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------
async def test_el_logo_se_sirve_desde_el_backend(api: httpx.AsyncClient, admin_csrf: str) -> None:
    await api.post(
        LOGO, files=_file(PNG, "logo.png", "image/png"), headers={"X-CSRF-Token": admin_csrf}
    )

    response = await api.get(LOGO)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == PNG


async def test_sin_logo_se_responde_404(api: httpx.AsyncClient, admin_csrf: str) -> None:
    assert (await api.get(LOGO)).status_code == 404


async def test_sin_sesion_no_se_puede_leer_el_logo(api: httpx.AsyncClient) -> None:
    assert (await api.get(LOGO)).status_code == 401


async def test_operator_puede_ver_el_logo(
    api: httpx.AsyncClient, admin_csrf: str, api_app: object
) -> None:
    await api.post(
        LOGO, files=_file(PNG, "logo.png", "image/png"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert (await api.get(LOGO)).status_code == 200


# ---------------------------------------------------------------------------
# Reemplazo y borrado
# ---------------------------------------------------------------------------
async def test_reemplazar_el_logo_borra_el_anterior(
    api: httpx.AsyncClient, admin_csrf: str, storage: FakeObjectStorage
) -> None:
    await api.post(
        LOGO, files=_file(PNG, "logo.png", "image/png"), headers={"X-CSRF-Token": admin_csrf}
    )
    primera_ruta = storage.uploads[0]

    await api.post(
        LOGO, files=_file(WEBP, "logo.webp", "image/webp"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert len(storage.uploads) == 2
    assert primera_ruta in storage.deletes
    assert primera_ruta not in storage.objects


async def test_eliminar_el_logo(
    api: httpx.AsyncClient, admin_csrf: str, storage: FakeObjectStorage
) -> None:
    await api.post(
        LOGO, files=_file(PNG, "logo.png", "image/png"), headers={"X-CSRF-Token": admin_csrf}
    )

    response = await api.delete(LOGO, headers={"X-CSRF-Token": admin_csrf})

    assert response.status_code == 200
    assert response.json()["logo"] is None
    assert storage.deletes
    assert (await api.get(LOGO)).status_code == 404


async def test_operator_no_puede_eliminar_el_logo(
    api: httpx.AsyncClient, operator_csrf: str
) -> None:
    response = await api.delete(LOGO, headers={"X-CSRF-Token": operator_csrf})

    assert response.status_code == 403


async def test_eliminar_sin_logo_no_falla(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.delete(LOGO, headers={"X-CSRF-Token": admin_csrf})

    assert response.status_code == 200
    assert response.json()["logo"] is None
