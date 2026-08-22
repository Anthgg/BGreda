"""Validacion de seguridad del logo."""

from __future__ import annotations

import pytest

from app.services.storage import (
    ALLOWED_IMAGE_TYPES,
    LogoValidationError,
    build_logo_path,
    safe_extension,
    sniff_content_type,
    validate_logo,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
GIF = b"GIF89a" + b"\x00" * 64

MAX = 1024 * 1024


def _validate(data: bytes, filename: str | None = None, declared: str | None = None) -> str:
    return validate_logo(
        data=data, filename=filename, declared_content_type=declared, max_bytes=MAX
    )


# ---------------------------------------------------------------------------
# Formatos admitidos
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "esperado"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")],
)
def test_se_detecta_el_tipo_real_por_los_bytes(data: bytes, esperado: str) -> None:
    assert sniff_content_type(data) == esperado


def test_los_formatos_permitidos_son_los_declarados() -> None:
    assert set(ALLOWED_IMAGE_TYPES) == {"image/png", "image/jpeg", "image/webp"}


@pytest.mark.parametrize(
    ("data", "nombre"), [(PNG, "logo.png"), (JPEG, "logo.jpg"), (WEBP, "logo.webp")]
)
def test_se_aceptan_las_imagenes_validas(data: bytes, nombre: str) -> None:
    assert _validate(data, nombre) in ALLOWED_IMAGE_TYPES


def test_jpeg_admite_las_dos_extensiones() -> None:
    assert _validate(JPEG, "logo.jpeg") == "image/jpeg"


# ---------------------------------------------------------------------------
# Formatos rechazados
# ---------------------------------------------------------------------------
def test_svg_se_rechaza_por_admitir_scripts() -> None:
    with pytest.raises(LogoValidationError) as excinfo:
        _validate(SVG, "logo.svg", "image/svg+xml")

    assert excinfo.value.code == "LOGO_TYPE_NOT_ALLOWED"


def test_se_rechaza_un_formato_de_imagen_no_admitido() -> None:
    with pytest.raises(LogoValidationError):
        _validate(GIF, "logo.gif", "image/gif")


def test_se_rechaza_un_archivo_vacio() -> None:
    with pytest.raises(LogoValidationError) as excinfo:
        _validate(b"", "logo.png")

    assert excinfo.value.code == "LOGO_EMPTY"


def test_se_rechaza_un_archivo_demasiado_grande() -> None:
    with pytest.raises(LogoValidationError) as excinfo:
        validate_logo(
            data=PNG + b"\x00" * MAX,
            filename="logo.png",
            declared_content_type="image/png",
            max_bytes=MAX,
        )

    assert excinfo.value.code == "LOGO_TOO_LARGE"


# ---------------------------------------------------------------------------
# No confiar en lo que declara el cliente
# ---------------------------------------------------------------------------
def test_un_ejecutable_disfrazado_de_png_se_rechaza() -> None:
    """La extension y el Content-Type mienten; los bytes no."""
    with pytest.raises(LogoValidationError) as excinfo:
        _validate(b"MZ\x90\x00" + b"\x00" * 64, "payload.png", "image/png")

    assert excinfo.value.code == "LOGO_TYPE_NOT_ALLOWED"


def test_el_tipo_declarado_debe_coincidir_con_el_contenido() -> None:
    with pytest.raises(LogoValidationError) as excinfo:
        _validate(PNG, "logo.png", "image/webp")

    assert excinfo.value.code == "LOGO_TYPE_MISMATCH"


def test_la_extension_debe_coincidir_con_el_contenido() -> None:
    with pytest.raises(LogoValidationError) as excinfo:
        _validate(PNG, "logo.webp")

    assert excinfo.value.code == "LOGO_EXTENSION_MISMATCH"


def test_sin_extension_basta_con_el_contenido_real() -> None:
    assert _validate(PNG, "logo") == "image/png"


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "nombre",
    [
        "../../../etc/passwd.png",
        "..\\..\\windows\\system32\\evil.png",
        "/absoluto/logo.png",
        "carpeta/logo.png",
    ],
)
def test_el_nombre_no_puede_escapar_a_otra_ruta(nombre: str) -> None:
    """Solo se conserva la extension; el nombre nunca forma la ruta interna."""
    assert "/" not in safe_extension(nombre)
    assert "\\" not in safe_extension(nombre)
    assert safe_extension(nombre) == "png"


def test_la_ruta_interna_la_genera_el_backend() -> None:
    ruta = build_logo_path("image/png")

    assert ruta.startswith("company/logo-")
    assert ruta.endswith(".png")
    assert ".." not in ruta


def test_dos_subidas_no_comparten_ruta() -> None:
    assert build_logo_path("image/png") != build_logo_path("image/png")


def test_un_nombre_con_traversal_no_llega_a_la_ruta() -> None:
    _validate(PNG, "../../evil.png")
    ruta = build_logo_path("image/png")

    assert "evil" not in ruta
    assert ".." not in ruta
