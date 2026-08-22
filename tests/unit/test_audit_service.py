"""Reglas de la auditoria: que se registra y que jamas se registra."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.audit import MAX_VALUE_LENGTH, REDACTED
from app.services.audit import diff_model, is_sensitive, normalize_value


# ---------------------------------------------------------------------------
# Campos sensibles
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "campo",
    [
        "password",
        "user_password",
        "csrf_secret",
        "supabase_secret_key",
        "access_token",
        "refresh_token",
        "session_cookie",
        "DATABASE_URL",
        "api_key",
        "apikey",
        "authorization",
        "private_key",
    ],
)
def test_los_campos_sensibles_se_detectan(campo: str) -> None:
    assert is_sensitive(campo)


@pytest.mark.parametrize(
    "campo",
    ["legal_name", "tax_id", "tax_percent", "bank_name", "account_number", "cci", "prefix"],
)
def test_los_campos_empresariales_no_son_sensibles(campo: str) -> None:
    assert not is_sensitive(campo)


def test_el_valor_de_un_campo_sensible_nunca_se_almacena() -> None:
    assert normalize_value("password", "SuperSecreta123") == REDACTED
    assert normalize_value("supabase_secret_key", "sb_secret_abc") == REDACTED


def test_un_campo_sensible_nulo_tambien_se_redacta() -> None:
    """Distinguir nulo de no nulo ya filtra informacion."""
    assert normalize_value("access_token", None) == REDACTED


# ---------------------------------------------------------------------------
# Normalizacion de valores
# ---------------------------------------------------------------------------
def test_el_binario_del_logo_no_se_copia() -> None:
    assert normalize_value("logo", b"\x89PNG...") == "(archivo)"
    assert normalize_value("logo_object_path", "company/logo-abc.png") == "(archivo)"


def test_los_decimales_conservan_su_valor() -> None:
    assert normalize_value("tax_percent", Decimal("18.000000")) == "18"
    assert normalize_value("tax_percent", Decimal("18.5")) == "18.5"


def test_los_booleanos_se_escriben_legibles() -> None:
    assert normalize_value("active", True) == "true"
    assert normalize_value("active", False) == "false"


def test_un_valor_nulo_se_conserva_como_nulo() -> None:
    assert normalize_value("legal_name", None) is None


def test_los_textos_muy_largos_se_truncan() -> None:
    resultado = normalize_value("general_conditions", "x" * (MAX_VALUE_LENGTH + 500))

    assert resultado is not None
    assert len(resultado) == MAX_VALUE_LENGTH + 3
    assert resultado.endswith("...")


# ---------------------------------------------------------------------------
# Deteccion de cambios
# ---------------------------------------------------------------------------
def test_solo_se_registran_los_campos_que_cambian() -> None:
    actual = SimpleNamespace(legal_name="Greda", tax_id="20123456789", phone=None)

    cambios = diff_model(
        actual,
        {"legal_name": "Greda SAC", "tax_id": "20123456789", "phone": None},
        ["legal_name", "tax_id", "phone"],
    )

    assert cambios == {"legal_name": ("Greda", "Greda SAC")}


def test_guardar_sin_editar_no_genera_historial() -> None:
    actual = SimpleNamespace(legal_name="Greda", tax_id=None)

    cambios = diff_model(actual, {"legal_name": "Greda", "tax_id": None}, ["legal_name", "tax_id"])

    assert cambios == {}


def test_pasar_de_nulo_a_valor_cuenta_como_cambio() -> None:
    actual = SimpleNamespace(phone=None)

    cambios = diff_model(actual, {"phone": "987654321"}, ["phone"])

    assert cambios == {"phone": (None, "987654321")}
