"""Contratos de la consulta de identidad (DNI/RUC).

Solo se exponen los campos que un proveedor real puede entregar. Ningun campo
se inventa cuando el proveedor no lo trae: llega como ``None``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DniLookupOut(_Out):
    document_type: Literal["DNI"] = "DNI"
    document_number: str
    full_name: str
    first_names: str | None = None
    paternal_surname: str | None = None
    maternal_surname: str | None = None
    provider: str
    cache_hit: bool
    #: Momento en que el proveedor entrego el dato, no en que se sirvio desde
    #: cache. Asi el frontend puede mostrar "consultado hace 12 dias".
    freshness: datetime


class RucLookupOut(_Out):
    document_type: Literal["RUC"] = "RUC"
    document_number: str
    business_name: str
    status: str | None = None
    condition: str | None = None
    address: str | None = None
    ubigeo: str | None = None
    #: Resueltos contra el catalogo territorial existente cuando el ubigeo
    #: del proveedor coincide con un distrito conocido. Si no coincide, el
    #: ubigeo crudo del proveedor se conserva arriba y estos quedan en None:
    #: no se inserta un distrito nuevo en el catalogo por una consulta externa.
    department: str | None = None
    province: str | None = None
    district: str | None = None
    provider: str
    cache_hit: bool
    freshness: datetime
