"""Seguridad de la migracion 0007 y fidelidad de su seed.

Dos cosas se comprueban aqui sin necesidad de base de datos: que las tablas
nuevas queden con RLS habilitado y sin politicas publicas, y que el catalogo
sembrado sea el del documento funcional y no una aproximacion.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

MIGRATION = (
    Path(__file__).resolve().parent.parent.parent
    / "alembic"
    / "versions"
    / "0007_kilns_rates_and_firings.py"
)

TABLAS = (
    "kilns",
    "kiln_rates",
    "kiln_occupancy_factors",
    "firings",
    "firing_kiln_sessions",
    "firing_lines",
)


@pytest.fixture(scope="module")
def contenido() -> str:
    return MIGRATION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def migracion() -> ModuleType:
    """Carga la migracion por ruta: ``alembic/versions`` no es un paquete."""
    spec = importlib.util.spec_from_file_location("migracion_0007", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rls_habilitado_sin_forzar(contenido: str) -> None:
    """FORCE RLS bloquearia al propio backend: se habilita RLS, no se fuerza."""
    assert "ENABLE ROW LEVEL SECURITY" in contenido
    assert "FORCE ROW LEVEL SECURITY" not in contenido


def test_revoca_los_roles_publicos_de_postgrest(contenido: str) -> None:
    assert "REVOKE ALL" in contenido
    assert "anon" in contenido
    assert "authenticated" in contenido


def test_no_crea_ninguna_politica_publica(contenido: str) -> None:
    assert "CREATE POLICY" not in contenido


def test_cubre_las_seis_tablas_nuevas(contenido: str) -> None:
    for tabla in TABLAS:
        assert tabla in contenido, tabla


def test_el_seed_transcribe_las_tarifas_del_documento(migracion: ModuleType) -> None:
    assert migracion.SEED_KILNS == (
        ("KILN-001", "Horno pequeno", 17000, "90.00", "180.00"),
        ("KILN-002", "Horno grande", 200000, "1000.00", "2000.00"),
    )


def test_el_seed_transcribe_los_diez_tramos_de_factores(migracion: ModuleType) -> None:
    assert len(migracion.SEED_FACTORS) == 10
    # Los tramos son contiguos y cubren de 1 a 100 sin huecos.
    assert [(minimo, maximo) for minimo, maximo, _c, _g in migracion.SEED_FACTORS] == [
        (1, 10),
        (11, 20),
        (21, 30),
        (31, 40),
        (41, 50),
        (51, 60),
        (61, 70),
        (71, 80),
        (81, 90),
        (91, 100),
    ]
    # El tramo que usa el caso de referencia: horno pequeno al 71-80 % -> 1.2.
    assert migracion.SEED_FACTORS[7] == (71, 80, "1.2", "1.4")
    # Al llenar el horno el multiplicador desaparece en ambos hornos.
    assert migracion.SEED_FACTORS[9] == (91, 100, "1.0", "1.0")


def test_ninguna_cantidad_de_negocio_usa_coma_flotante(contenido: str) -> None:
    """Los importes del seed viajan como texto, nunca como ``float``."""
    assert "sa.Float" not in contenido
    assert "REAL" not in contenido
    assert "DOUBLE PRECISION" not in contenido
