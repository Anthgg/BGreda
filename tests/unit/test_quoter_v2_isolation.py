"""Fase 010A — la frontera entre Legacy y V2, comprobada sin base de datos.

Estas pruebas no miran resultados: miran la FORMA del codigo. Existen porque el
aislamiento entre motores es la clase de propiedad que se rompe sin que nadie
lo note —un import «solo para reutilizar esta funcioncita», un enum compartido
«que total es el mismo»— y cuando se rompe, lo hace sobre un precio.

Lo que aqui se fija:

1. el motor es un dato explicito y persistido, con dos valores y no mas;
2. el dominio V2 no importa una sola linea del dominio Legacy;
3. el motor Legacy tampoco importa el V2 —la dependencia no puede existir en
   ninguna de las dos direcciones, o retirar Legacy en 010J arrastraria V2—;
4. las tablas no se solapan y cada una declara su propio motor;
5. el talonario de V2 es otro, y su prefijo no puede confundirse con el de
   Legacy.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core.pricing_engine import PricingEngineVersion
from app.models.quotations import Quotation
from app.models.quoter_v2 import (
    DEFAULT_V2_PRODUCTION_TYPE,
    V2ProductionType,
    V2Quotation,
    V2QuotationStatus,
)
from app.models.sequence import SequenceType

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Los ficheros que forman el dominio V2. Ninguno puede tocar Legacy.
V2_MODULES = (
    "app/models/quoter_v2.py",
    "app/schemas/quoter_v2.py",
    "app/services/quoter_v2.py",
    "app/api/v1/quoter_v2.py",
)

#: Modulos del motor historico. Que V2 importe cualquiera de estos significa
#: que una formula acumulada durante cuatro fases puede volver a decidir un
#: precio del motor nuevo.
LEGACY_MODULES = frozenset(
    {
        "app.models.quotations",
        "app.schemas.quotations",
        "app.schemas.quotation_builder",
        "app.services.quotations",
        "app.services.quotation_builder",
        "app.services.quotation_pdf",
        "app.core.quotations",
        "app.core.pricing",
        "app.core.prototype_pricing",
    }
)


def _imported_modules(relative_path: str) -> set[str]:
    """Modulos que importa un fichero, resueltos a su ruta con puntos."""
    arbol = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    modulos: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            modulos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            modulos.add(nodo.module)
    return modulos


# ---------------------------------------------------------------------------
# 1. El motor es explicito
# ---------------------------------------------------------------------------
def test_solo_existen_dos_motores() -> None:
    assert [miembro.value for miembro in PricingEngineVersion] == ["LEGACY", "V2"]


def test_la_cabecera_legacy_nace_sellada_como_legacy() -> None:
    assert Quotation.__table__.c.pricing_engine_version.nullable is False
    assert Quotation.__table__.c.pricing_engine_version.server_default is not None


def test_la_cabecera_v2_nace_sellada_como_v2() -> None:
    columna = V2Quotation.__table__.c.pricing_engine_version
    assert columna.nullable is False
    assert columna.server_default is not None


@pytest.mark.parametrize(
    ("tabla", "esperado"),
    [(Quotation.__table__, "LEGACY"), (V2Quotation.__table__, "V2")],
)
def test_cada_tabla_declara_su_motor_en_un_check(tabla: object, esperado: str) -> None:
    """El aislamiento lo sostiene la base de datos, no la buena fe del codigo."""
    textos = [
        str(restriccion.sqltext)
        for restriccion in tabla.constraints  # type: ignore[attr-defined]
        if hasattr(restriccion, "sqltext")
    ]
    assert any(
        "pricing_engine_version" in texto and f"'{esperado}'" in texto for texto in textos
    ), f"falta el CHECK que fija el motor {esperado}"


# ---------------------------------------------------------------------------
# 2 y 3. Nadie importa a nadie
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("modulo", V2_MODULES)
def test_el_dominio_v2_no_importa_legacy(modulo: str) -> None:
    prohibidos = _imported_modules(modulo) & LEGACY_MODULES
    assert not prohibidos, (
        f"{modulo} importa modulos del motor Legacy: {sorted(prohibidos)}. "
        "V2 nace sin las decisiones acumuladas de Legacy —el factor por "
        "ocupacion de horno entre ellas— y compartir codigo es la via mas "
        "rapida para que vuelvan."
    )


@pytest.mark.parametrize(
    "modulo",
    [
        "app/services/quotations.py",
        "app/services/quotation_builder.py",
        "app/api/v1/quotations.py",
        "app/api/v1/quotation_builder.py",
    ],
)
def test_legacy_no_importa_el_dominio_v2(modulo: str) -> None:
    """La dependencia tampoco puede existir al reves.

    Si Legacy llamara a V2, retirar Legacy en 010J dejaria de ser una
    operacion local: habria que desmontar medio V2 con el.
    """
    importados = _imported_modules(modulo)
    v2 = {nombre for nombre in importados if nombre.endswith("quoter_v2")}
    assert not v2, f"{modulo} importa el dominio V2: {sorted(v2)}"


# ---------------------------------------------------------------------------
# 4. Las tablas no se solapan
# ---------------------------------------------------------------------------
def test_v2_vive_en_su_propia_tabla() -> None:
    assert V2Quotation.__tablename__ == "v2_quotations"
    assert Quotation.__tablename__ == "quotations"


def test_v2_no_reutiliza_los_enums_de_legacy() -> None:
    """Mismos valores hoy, historias distintas manana.

    V2 anadira `EXPIRED` cuando llegue la vigencia (010H). Si el enum fuera
    compartido, ese valor aparecerian de rebote en documentos historicos que
    nunca supieron vencer.
    """
    from app.models.quotations import QuotationStatus

    assert V2QuotationStatus is not QuotationStatus


def test_el_tipo_de_produccion_por_defecto_es_por_menor() -> None:
    assert DEFAULT_V2_PRODUCTION_TYPE is V2ProductionType.RETAIL


# ---------------------------------------------------------------------------
# 5. Talonarios separados
# ---------------------------------------------------------------------------
def test_v2_tiene_su_propio_tipo_de_secuencia() -> None:
    assert SequenceType.QUOTE_V2 != SequenceType.QUOTE
    assert SequenceType.QUOTE_V2.value == "QUOTE_V2"


def test_el_check_de_secuencias_admite_el_tipo_nuevo() -> None:
    from app.models.sequence import DocumentSequence

    textos = [
        str(restriccion.sqltext)
        for restriccion in DocumentSequence.__table__.constraints
        if hasattr(restriccion, "sqltext")
    ]
    assert any("QUOTE_V2" in texto for texto in textos)
