"""Reglas estructurales de la generacion de correlativos.

Estas pruebas inspeccionan el propio codigo. No sustituyen a la prueba de
concurrencia real, pero detectan al instante una regresion que la reintroduzca:
alguien podria "arreglar" un fallo cambiando el UPDATE atomico por un SELECT
previo y la suite funcional seguiria pasando en local, donde nunca hay dos
peticiones a la vez.
"""

from __future__ import annotations

import ast
import inspect
import re

from app.services import sequences


def _source() -> str:
    return inspect.getsource(sequences)


def test_no_se_calcula_el_siguiente_numero_con_max() -> None:
    """SELECT MAX(numero) + 1 esta prohibido por el plan del proyecto.

    Entre el SELECT y el INSERT otra transaccion puede leer el mismo maximo y
    ambas obtendrian el mismo correlativo.

    Se analiza el arbol sintactico y no el texto: el propio docstring del
    modulo menciona la formula prohibida para explicarla.
    """
    arbol = ast.parse(_source())

    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            if isinstance(nodo.func, ast.Name):
                nombres.add(nodo.func.id)
            elif isinstance(nodo.func, ast.Attribute):
                nombres.add(nodo.func.attr)

    assert "max" not in nombres, "el contador no puede derivarse de un maximo"


def test_el_contador_se_actualiza_con_update_returning() -> None:
    """El punto de serializacion debe ser el propio UPDATE."""
    codigo = _source()

    assert "update(DocumentSequence)" in codigo
    assert ".returning(" in codigo


def test_no_se_lee_el_contador_para_luego_escribirlo() -> None:
    """Un patron leer-y-despues-escribir no seria atomico."""
    codigo = _source()

    # No debe existir una asignacion del tipo current_value = <algo leido> + 1
    assert not re.search(r"current_value\s*=\s*\w+\.current_value\s*\+\s*1", codigo)


def test_el_registro_de_emision_se_escribe_en_la_misma_transaccion() -> None:
    """El numero y su registro deben confirmarse juntos o no confirmarse."""
    codigo = _source()

    assert "DocumentSequenceIssue(" in codigo
    # El servicio no confirma por su cuenta: lo hace quien crea el documento.
    assert "commit()" not in codigo


def test_la_vista_previa_no_toca_la_base_de_datos() -> None:
    """preview_for debe ser una funcion pura."""
    arbol = ast.parse(inspect.getsource(sequences.preview_for))
    llamadas = {
        nodo.func.attr
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
    }

    assert "execute" not in llamadas
    assert "add" not in llamadas
    assert "commit" not in llamadas
