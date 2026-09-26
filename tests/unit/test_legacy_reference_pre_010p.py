"""Fase 010P W0 — el Excel anterior queda como LEGACY_REFERENCE_PRE_010P.

Decision del owner (P3): el caso canonico de S/ 11 864,90 pertenece a las
reglas anteriores. Con 010P deja de ser oraculo para lo que cambia (espacio,
administracion, mano de obra y todo lo que depende de ellos), pero NO se borra:
queda documentado en `tests/fixtures/LEGACY_REFERENCE_PRE_010P.json`.

Esta prueba esta en VERDE desde W0 y protege la integridad de esa referencia:
cada valor esta clasificado como vivo o invalidado, y los que siguen vivos
coinciden con lo que afirma la prueba del Excel en la base de datos.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.legacy_reference_pre_010p

TESTS = Path(__file__).resolve().parents[1]
FIXTURE = TESTS / "fixtures" / "LEGACY_REFERENCE_PRE_010P.json"
EXCEL_SMOKE = TESTS / "db" / "test_legacy_reference_pre_010p.py"


def _referencia() -> dict:  # type: ignore[type-arg]
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_la_referencia_se_llama_asi_y_dice_que_no_es_oraculo() -> None:
    datos = _referencia()
    assert datos["label"] == "LEGACY_REFERENCE_PRE_010P"
    assert "NO es oraculo" in datos["status"]


def test_el_caso_final_conserva_su_total_historico() -> None:
    valores = _referencia()["cases"]["excel_final_010j"]["values"]
    assert (valores["subtotal"], valores["tax"], valores["total"]) == (
        "10055",
        "1809.90",
        "11864.90",
    )


def test_cada_valor_esta_clasificado_una_sola_vez() -> None:
    caso = _referencia()["cases"]["excel_final_010j"]
    vivos = set(caso["still_live_under_010p"])
    invalidados = set(caso["invalidated_by_010p"])
    assert vivos.isdisjoint(invalidados)
    assert vivos | invalidados == set(caso["values"])


def test_los_valores_vivos_son_los_que_afirma_la_prueba_del_excel() -> None:
    caso = _referencia()["cases"]["excel_final_010j"]
    fuente = EXCEL_SMOKE.read_text(encoding="utf-8")
    for nombre in caso["still_live_under_010p"]:
        assert f'"{caso["values"][nombre]}"' in fuente, nombre
