"""Los CHECK que enumeran valores no pueden quedarse atras del enum.

Esta prueba nace de un fallo real de 009K: la migracion 0021 amplio
`document_sequences.type_allowed` para aceptar `PROTOTYPE`, pero el CHECK del
modelo se quedo con la lista anterior. En produccion no se habria notado
—manda la migracion—, pero la base de pruebas se crea desde los modelos, asi
que 29 pruebas fallaron con un `SEQUENCE_NOT_CONFIGURED` que no tenia nada que
ver con lo que estaban probando.

El error es barato de cometer: anadir un miembro al enum es una linea y el
CHECK vive en otro archivo. Comparar las dos listas cuesta milisegundos y no
necesita base de datos.
"""

from __future__ import annotations

import re

from sqlalchemy import CheckConstraint

from app.models.inventory import MovementType, StockMovement
from app.models.sequence import DocumentSequence, SequenceType


def _valores_del_check(tabla: object, nombre: str) -> set[str]:
    """Los literales que un CHECK `columna IN (...)` acepta.

    Se busca por sufijo: la convencion de nombres del proyecto antepone
    `ck_<tabla>_`, y fijar el nombre completo aqui ataria la prueba a esa
    convencion en vez de a lo que quiere comprobar.
    """
    for restriccion in tabla.__table__.constraints:  # type: ignore[attr-defined]
        nombre_real = restriccion.name or ""
        if isinstance(restriccion, CheckConstraint) and nombre_real.endswith(nombre):
            return set(re.findall(r"'([A-Z_0-9]+)'", str(restriccion.sqltext)))
    raise AssertionError(f"No existe el CHECK «{nombre}»")


def test_el_check_de_secuencias_acepta_todos_los_tipos() -> None:
    aceptados = _valores_del_check(DocumentSequence, "type_allowed")
    assert aceptados == {tipo.value for tipo in SequenceType}


def test_el_check_de_movimientos_acepta_todos_los_tipos() -> None:
    aceptados = _valores_del_check(StockMovement, "movement_type_allowed")
    assert aceptados == {tipo.value for tipo in MovementType}
