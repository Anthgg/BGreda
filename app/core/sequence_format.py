"""Renderizado del patron de una secuencia documental.

Funciones puras, sin acceso a base de datos: se usan tanto para emitir un
correlativo real como para calcular una vista previa que no consume contador.

El patron es configurable a proposito. El Plan v1.2 aprueba
``{PREFIX}-{YYYY}-{NUMBER}`` y el Documento Funcional describe una variante con
mes y dia; ambas se alcanzan cambiando configuracion, nunca codigo.
"""

from __future__ import annotations

import re
from datetime import date

#: Marcadores admitidos dentro de un patron.
ALLOWED_TOKENS = frozenset({"PREFIX", "YYYY", "YY", "MM", "DD", "NUMBER"})

#: Marcador obligatorio: sin numero no hay correlativo.
REQUIRED_TOKEN = "NUMBER"  # noqa: S105 - es un marcador de patron, no una credencial

_TOKEN_PATTERN = re.compile(r"\{([A-Z]+)\}")

#: Longitud maxima del texto renderizado; coincide con la columna que lo guarda.
MAX_RENDERED_LENGTH = 160


class SequencePatternError(ValueError):
    """El patron configurado no es utilizable."""


def extract_tokens(pattern: str) -> list[str]:
    """Devuelve los marcadores presentes en el patron, en orden."""
    return _TOKEN_PATTERN.findall(pattern)


def validate_pattern(pattern: str) -> str:
    """Comprueba que el patron sea renderizable. Devuelve el patron validado."""
    if not pattern.strip():
        raise SequencePatternError("El patron no puede estar vacio")

    tokens = extract_tokens(pattern)
    unknown = sorted({token for token in tokens if token not in ALLOWED_TOKENS})
    if unknown:
        admitidos = ", ".join(f"{{{token}}}" for token in sorted(ALLOWED_TOKENS))
        raise SequencePatternError(
            f"Marcadores no reconocidos: {', '.join(unknown)}. Admitidos: {admitidos}"
        )
    if REQUIRED_TOKEN not in tokens:
        raise SequencePatternError("El patron debe incluir {NUMBER}")
    if tokens.count(REQUIRED_TOKEN) > 1:
        raise SequencePatternError("El patron solo admite un {NUMBER}")
    return pattern


def render(pattern: str, *, prefix: str, number: int, padding: int, moment: date) -> str:
    """Construye el valor final de un correlativo.

    ``moment`` es la fecha de emision: el numero renderizado congela el formato
    vigente en ese instante, de modo que cambiar el prefijo mas adelante no
    reescribe los documentos ya emitidos.
    """
    validate_pattern(pattern)

    replacements = {
        "PREFIX": prefix,
        "YYYY": f"{moment.year:04d}",
        "YY": f"{moment.year % 100:02d}",
        "MM": f"{moment.month:02d}",
        "DD": f"{moment.day:02d}",
        "NUMBER": f"{number:0{padding}d}",
    }

    rendered = _TOKEN_PATTERN.sub(lambda match: replacements[match.group(1)], pattern)
    if len(rendered) > MAX_RENDERED_LENGTH:
        raise SequencePatternError(f"El valor generado supera {MAX_RENDERED_LENGTH} caracteres")
    return rendered
