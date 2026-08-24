"""Validacion, enmascarado y hash de la consulta de identidad."""

from __future__ import annotations

import pytest

from app.core.identity import (
    IdentityDocumentType,
    InvalidDniError,
    InvalidRucError,
    document_hash,
    mask_document,
    normalize_dni,
    normalize_ruc,
)


# ---------------------------------------------------------------------------
# DNI
# ---------------------------------------------------------------------------
def test_dni_valido_se_acepta_tal_cual() -> None:
    assert normalize_dni("12345678") == "12345678"


def test_dni_recorta_espacios_al_borde() -> None:
    assert normalize_dni("  12345678  ") == "12345678"


@pytest.mark.parametrize(
    "invalido",
    [
        "1234567",  # siete digitos
        "123456789",  # nueve digitos
        "1234 5678",  # espacio intermedio
        "1234567a",  # letra
        "1e8",  # notacion cientifica
        "12345.78",  # decimal
        "",
        "        ",
    ],
)
def test_dni_invalido_se_rechaza(invalido: str) -> None:
    with pytest.raises(InvalidDniError):
        normalize_dni(invalido)


def test_dni_conserva_ceros_iniciales() -> None:
    """Un DNI que empieza en cero deja de ser el mismo DNI si se lee como numero."""
    assert normalize_dni("00012345") == "00012345"


# ---------------------------------------------------------------------------
# RUC
# ---------------------------------------------------------------------------
def test_ruc_valido_se_acepta_tal_cual() -> None:
    assert normalize_ruc("20123456789") == "20123456789"


@pytest.mark.parametrize(
    "invalido",
    [
        "2012345678",  # diez digitos
        "201234567890",  # doce digitos
        "2012345 6789",
        "2012345678a",
        "1e11",
        "",
    ],
)
def test_ruc_invalido_se_rechaza(invalido: str) -> None:
    with pytest.raises(InvalidRucError):
        normalize_ruc(invalido)


# ---------------------------------------------------------------------------
# Enmascarado
# ---------------------------------------------------------------------------
def test_mascara_de_dni_conserva_los_dos_ultimos_digitos() -> None:
    assert mask_document(IdentityDocumentType.DNI, "12345678") == "******78"


def test_mascara_de_ruc_conserva_los_cuatro_ultimos_digitos() -> None:
    assert mask_document(IdentityDocumentType.RUC, "20123456789") == "*******6789"


def test_la_mascara_nunca_revela_el_documento_completo() -> None:
    dni = "87654321"
    mascara = mask_document(IdentityDocumentType.DNI, dni)
    assert dni not in mascara
    assert len(mascara) == len(dni)


# ---------------------------------------------------------------------------
# Hash de cache
# ---------------------------------------------------------------------------
def test_el_hash_es_deterministico() -> None:
    a = document_hash(IdentityDocumentType.DNI, "12345678")
    b = document_hash(IdentityDocumentType.DNI, "12345678")
    assert a == b


def test_el_hash_distingue_dni_de_ruc_aunque_el_texto_coincida() -> None:
    """El mismo texto no debe colisionar entre DNI y RUC en la cache."""
    dni_hash = document_hash(IdentityDocumentType.DNI, "12345678")
    ruc_hash = document_hash(IdentityDocumentType.RUC, "12345678")
    assert dni_hash != ruc_hash


def test_el_hash_no_es_reversible_a_simple_vista() -> None:
    """No es una prueba criptografica: solo que no se guarda el texto plano."""
    documento = "12345678"
    resultado = document_hash(IdentityDocumentType.DNI, documento)
    assert documento not in resultado
    assert len(resultado) == 64  # sha256 hex
