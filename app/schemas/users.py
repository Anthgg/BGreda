"""Contrato publico de la administracion de usuarios (Fase 009K.2).

Un usuario de Greda vive en dos sitios y no en uno: la CUENTA esta en Supabase
Auth —correo y contrasena, que este backend nunca guarda— y el PERFIL esta en
`profiles`, que es la lista de habilitacion: nombre visible, rol y si sigue
activo. Existir en Supabase no da acceso; hace falta perfil, y activo.

Estos esquemas juntan las dos mitades para la pantalla y no exponen nada mas.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.profile import UserRole

#: El limite de la columna. Se repite aqui para rechazar en el borde y no en la
#: base: un 422 explica que pasa, un error de integridad no.
NOMBRE_MAXIMO = 120


def _nombre_limpio(valor: str) -> str:
    """Sin espacios de sobra, y no vacio.

    Un nombre visible de un solo espacio pasaria el `min_length` y luego
    violaria el CHECK `display_name_not_blank`. Se recorta antes.
    """
    limpio = valor.strip()
    if not limpio:
        raise ValueError("El nombre visible no puede estar vacio")
    return limpio


class UserOut(BaseModel):
    """Lo que la pantalla de administracion necesita saber, y nada mas."""

    model_config = ConfigDict(from_attributes=True)

    #: Hace falta para saber a quien se edita. No viaja en ningun documento
    #: comercial: alli van nombres, no identificadores.
    id: uuid.UUID
    display_name: str
    #: Viene de Supabase Auth, que es su unica autoridad. `None` cuando el
    #: perfil existe pero la cuenta no aparece: eso se ensena, no se esconde.
    email: str | None = None
    role: UserRole
    active: bool


class UserPage(BaseModel):
    items: list[UserOut]
    total: int


class UserCreateIn(BaseModel):
    """Alta: crea la cuenta en Supabase Y el perfil local."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    display_name: Annotated[str, Field(min_length=1, max_length=NOMBRE_MAXIMO)]
    role: UserRole = UserRole.OPERATOR
    #: Contrasena inicial. Viaja de ida y nunca de vuelta: no se guarda en este
    #: backend, no se registra en los logs y no aparece en ninguna respuesta.
    #: La custodia es de Supabase.
    password: Annotated[str, Field(min_length=8, max_length=72)]

    _limpiar = field_validator("display_name")(_nombre_limpio)


class UserUpdateIn(BaseModel):
    """Edicion de identidad y rol. El correo no se toca en esta fase."""

    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[str | None, Field(default=None, max_length=NOMBRE_MAXIMO)] = None
    role: UserRole | None = None

    @field_validator("display_name")
    @classmethod
    def _limpiar(cls, valor: str | None) -> str | None:
        return None if valor is None else _nombre_limpio(valor)
