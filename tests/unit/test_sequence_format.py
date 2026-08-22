"""Formato de las secuencias documentales."""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.core.sequence_format import (
    MAX_RENDERED_LENGTH,
    SequencePatternError,
    render,
    validate_pattern,
)
from app.models.sequence import ResetPolicy
from app.services.sequences import period_key_for

PLAN_PATTERN = "{PREFIX}-{YYYY}-{NUMBER}"
DIA = date(2026, 8, 22)


# ---------------------------------------------------------------------------
# Formato aprobado en el Plan v1.2
# ---------------------------------------------------------------------------
def test_formato_de_cotizacion_del_plan() -> None:
    assert render(PLAN_PATTERN, prefix="CTZ", number=1, padding=6, moment=DIA) == "CTZ-2026-000001"


def test_formato_de_quema_del_plan() -> None:
    assert render(PLAN_PATTERN, prefix="HR", number=25, padding=6, moment=DIA) == "HR-2026-000025"


def test_el_padding_rellena_con_ceros() -> None:
    assert render(PLAN_PATTERN, prefix="CTZ", number=7, padding=3, moment=DIA) == "CTZ-2026-007"


def test_un_numero_mas_largo_que_el_padding_no_se_trunca() -> None:
    """Perder digitos convertiria dos correlativos distintos en el mismo."""
    assert render(PLAN_PATTERN, prefix="CTZ", number=1234567, padding=6, moment=DIA).endswith(
        "1234567"
    )


def test_el_ano_cambia_con_la_fecha() -> None:
    valor = render(PLAN_PATTERN, prefix="CTZ", number=1, padding=6, moment=date(2027, 1, 1))

    assert valor == "CTZ-2027-000001"


# ---------------------------------------------------------------------------
# Variante del Documento Funcional, alcanzable por configuracion
# ---------------------------------------------------------------------------
def test_el_formato_con_mes_y_dia_se_obtiene_sin_tocar_codigo() -> None:
    """El Documento Funcional describe HR-AAAA-MM-DD-NNN.

    Se resuelve configurando el patron, no modificando la aplicacion.
    """
    valor = render(
        "{PREFIX}-{YYYY}-{MM}-{DD}-{NUMBER}", prefix="HR", number=3, padding=3, moment=DIA
    )

    assert valor == "HR-2026-08-22-003"


def test_el_ano_corto_tambien_esta_disponible() -> None:
    assert render("{PREFIX}{YY}{NUMBER}", prefix="C", number=9, padding=4, moment=DIA) == "C260009"


# ---------------------------------------------------------------------------
# Validacion de patrones
# ---------------------------------------------------------------------------
def test_el_patron_debe_incluir_number() -> None:
    with pytest.raises(SequencePatternError, match="NUMBER"):
        validate_pattern("{PREFIX}-{YYYY}")


def test_el_patron_no_admite_dos_numbers() -> None:
    with pytest.raises(SequencePatternError, match=re.escape("un {NUMBER}")):
        validate_pattern("{NUMBER}-{NUMBER}")


def test_se_rechazan_marcadores_desconocidos() -> None:
    with pytest.raises(SequencePatternError, match="SEMANA"):
        validate_pattern("{PREFIX}-{SEMANA}-{NUMBER}")


def test_se_rechaza_un_patron_vacio() -> None:
    with pytest.raises(SequencePatternError):
        validate_pattern("   ")


def test_se_rechaza_un_valor_renderizado_demasiado_largo() -> None:
    with pytest.raises(SequencePatternError, match=str(MAX_RENDERED_LENGTH)):
        render("{PREFIX}-{NUMBER}", prefix="X" * 200, number=1, padding=6, moment=DIA)


# ---------------------------------------------------------------------------
# Periodos de reinicio
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("politica", "esperado"),
    [
        (ResetPolicy.NEVER, ""),
        (ResetPolicy.YEARLY, "2026"),
        (ResetPolicy.MONTHLY, "2026-08"),
        (ResetPolicy.DAILY, "2026-08-22"),
    ],
)
def test_clave_de_periodo_por_politica(politica: ResetPolicy, esperado: str) -> None:
    assert period_key_for(politica, DIA) == esperado


def test_el_periodo_anual_distingue_los_anos() -> None:
    assert period_key_for(ResetPolicy.YEARLY, date(2026, 12, 31)) != period_key_for(
        ResetPolicy.YEARLY, date(2027, 1, 1)
    )
