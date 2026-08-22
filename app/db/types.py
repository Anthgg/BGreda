"""Tipos de columna compartidos."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class StrEnumType(TypeDecorator[Any]):
    """Guarda un ``StrEnum`` como texto y lo devuelve como miembro del enum.

    Sin esto, PostgreSQL devuelve ``str`` y una comparacion con ``is`` o una
    pertenencia a un ``set`` de miembros del enum falla en silencio: ``"CREATE"``
    y ``ImportAction.CREATE`` no comparten hash. Se prefiere ``VARCHAR`` con
    ``CHECK`` a un tipo ENUM nativo porque anadir un valor nuevo no exige
    ``ALTER TYPE``.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[StrEnum], length: int) -> None:
        self._enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return str(self._enum_class(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        return self._enum_class(value)
