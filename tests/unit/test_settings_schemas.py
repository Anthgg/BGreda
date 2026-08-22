"""Validacion de los contratos de configuracion."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.sequence import SequenceConfigUpdate
from app.schemas.settings import (
    BankAccountBase,
    CommercialSettingsUpdate,
    CompanySettingsUpdate,
)


def _company(**campos: object) -> CompanySettingsUpdate:
    return CompanySettingsUpdate(version=1, **campos)  # type: ignore[arg-type]


def _commercial(**campos: object) -> CommercialSettingsUpdate:
    return CommercialSettingsUpdate(version=1, **campos)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mass assignment
# ---------------------------------------------------------------------------
def test_no_se_admiten_campos_desconocidos() -> None:
    """Un campo no expuesto debe fallar, no ignorarse en silencio."""
    with pytest.raises(ValidationError, match="extra"):
        _company(logo_object_path="company/otro.png")


def test_no_se_puede_escribir_la_version_interna_por_otro_nombre() -> None:
    with pytest.raises(ValidationError):
        _commercial(id=99)


# ---------------------------------------------------------------------------
# Empresa
# ---------------------------------------------------------------------------
def test_el_ruc_debe_tener_once_digitos() -> None:
    with pytest.raises(ValidationError, match="11 digitos"):
        _company(tax_id="123")


def test_el_ruc_no_admite_letras() -> None:
    with pytest.raises(ValidationError, match="digitos"):
        _company(tax_id="2012345678A")


def test_un_ruc_valido_se_acepta() -> None:
    assert _company(tax_id="20123456789").tax_id == "20123456789"


def test_el_correo_debe_tener_formato_valido() -> None:
    with pytest.raises(ValidationError):
        _company(email="no-es-un-correo")


def test_el_sitio_web_exige_esquema() -> None:
    with pytest.raises(ValidationError, match="http"):
        _company(website="greda.pe")


def test_los_campos_vacios_se_normalizan_a_nulo() -> None:
    assert _company(legal_name="   ").legal_name is None


# ---------------------------------------------------------------------------
# Texto plano, nunca HTML
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "texto",
    [
        "<script>alert(1)</script>",
        "Condiciones <b>importantes</b>",
        "</div>",
        "<img src=x onerror=alert(1)>",
    ],
)
def test_no_se_admite_marcado_html_en_los_textos(texto: str) -> None:
    with pytest.raises(ValidationError, match="HTML"):
        _commercial(general_conditions=texto)


def test_los_signos_de_comparacion_normales_si_se_admiten() -> None:
    """Prohibir el caracter "<" impediria escribir condiciones legitimas."""
    valor = _commercial(general_conditions="Pedidos < 10 unidades: 3 dias").general_conditions

    assert valor == "Pedidos < 10 unidades: 3 dias"


def test_se_rechazan_los_caracteres_de_control() -> None:
    with pytest.raises(ValidationError, match="control"):
        _commercial(payment_notes="texto\x00malicioso")


# ---------------------------------------------------------------------------
# Comercial
# ---------------------------------------------------------------------------
def test_el_igv_se_persiste_como_decimal() -> None:
    valor = _commercial(tax_percent=18).tax_percent

    assert isinstance(valor, Decimal)


def test_un_igv_con_decimales_no_pierde_precision() -> None:
    """Aunque el cliente envie un numero JSON, el valor no se degrada."""
    assert _commercial(tax_percent=18.5).tax_percent == Decimal("18.5")


def test_el_igv_admite_cero() -> None:
    assert _commercial(tax_percent=0).tax_percent == Decimal("0")


def test_el_igv_no_admite_negativos() -> None:
    with pytest.raises(ValidationError):
        _commercial(tax_percent=-1)


def test_el_igv_no_admite_valores_absurdos() -> None:
    """18 significa 18 %; enviar 1800 seria confundir fraccion con porcentaje."""
    with pytest.raises(ValidationError):
        _commercial(tax_percent=1800)


def test_la_moneda_se_normaliza_a_mayusculas() -> None:
    assert _commercial(currency_code="pen").currency_code == "PEN"


def test_la_moneda_debe_ser_iso_4217() -> None:
    with pytest.raises(ValidationError):
        _commercial(currency_code="SOL3")


def test_la_vigencia_debe_ser_positiva() -> None:
    with pytest.raises(ValidationError):
        _commercial(quote_validity_days=0)


def test_la_vigencia_tiene_un_limite_razonable() -> None:
    with pytest.raises(ValidationError):
        _commercial(quote_validity_days=99999)


# ---------------------------------------------------------------------------
# Cuenta bancaria
# ---------------------------------------------------------------------------
def test_el_cci_debe_tener_veinte_digitos() -> None:
    with pytest.raises(ValidationError, match="20 digitos"):
        BankAccountBase(cci="123")


def test_el_cci_admite_espacios_y_guiones_al_escribirlo() -> None:
    assert BankAccountBase(cci="002-193-001234567890-15").cci == "00219300123456789015"


def test_un_cci_valido_se_conserva() -> None:
    assert BankAccountBase(cci="00219300123456789015").cci == "00219300123456789015"


# ---------------------------------------------------------------------------
# Secuencias
# ---------------------------------------------------------------------------
def _sequence(**campos: object) -> SequenceConfigUpdate:
    base: dict[str, object] = {
        "prefix": "CTZ",
        "pattern": "{PREFIX}-{YYYY}-{NUMBER}",
        "padding": 6,
        "reset_policy": "YEARLY",
        "active": True,
        "version": 1,
    }
    base.update(campos)
    return SequenceConfigUpdate(**base)  # type: ignore[arg-type]


def test_el_prefijo_se_normaliza_a_mayusculas() -> None:
    assert _sequence(prefix="ctz").prefix == "CTZ"


def test_el_prefijo_no_admite_separadores() -> None:
    with pytest.raises(ValidationError, match="letras y digitos"):
        _sequence(prefix="CT-Z")


def test_el_patron_invalido_se_rechaza() -> None:
    with pytest.raises(ValidationError, match="NUMBER"):
        _sequence(pattern="{PREFIX}-{YYYY}")


def test_el_padding_tiene_limites() -> None:
    with pytest.raises(ValidationError):
        _sequence(padding=0)
    with pytest.raises(ValidationError):
        _sequence(padding=99)
