"""Validacion, enmascarado y estados de la consulta de identidad (DNI/RUC).

Funciones puras: nada de red ni de base de datos. La logica de proveedores y
cache vive en ``app.services.identity``; aqui solo esta lo que cualquier capa
—proveedor, servicio, API— necesita para hablar el mismo idioma sin duplicar
reglas.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from enum import StrEnum

from app.core.errors import APIError

#: DNI peruano: exactamente ocho digitos. Ni espacios intermedios, ni letras,
#: ni notacion cientifica: la validacion es sobre el texto tal cual, nunca
#: sobre un ``int`` que ya habria perdido un cero inicial. ``re.ASCII`` evita
#: que un digito Unicode no latino (arabigo-indico, ancho completo...) pase
#: la validacion: ``\d`` sin esa bandera acepta esas variantes y la consulta
#: gastaria cuota externa con un documento que nunca fue valido.
_DNI_PATTERN = re.compile(r"^\d{8}$", re.ASCII)

#: RUC peruano: exactamente once digitos.
_RUC_PATTERN = re.compile(r"^\d{11}$", re.ASCII)


class IdentityDocumentType(StrEnum):
    """Los dos unicos documentos que este modulo sabe consultar.

    Partner admite tambien CE y PASSPORT (ver ``app.models.masters``), pero
    ningun proveedor de los integrados aqui los resuelve.
    """

    DNI = "DNI"
    RUC = "RUC"


class LookupStatus(StrEnum):
    """Resultado normalizado de una consulta, uniforme entre proveedores.

    Ningun consumidor de este modulo ve un codigo HTTP ni un mensaje crudo de
    Peru API o Decolecta: todo se traduce a uno de estos seis valores antes de
    salir del adaptador del proveedor.
    """

    SUCCESS = "SUCCESS"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    INVALID_DOCUMENT = "INVALID_DOCUMENT"


class ProviderName(StrEnum):
    """Los dos proveedores integrados. Perú API es el primario."""

    PERU_API = "PERU_API"
    DECOLECTA = "DECOLECTA"


class InvalidDniError(APIError):
    status_code = 422
    code = "INVALID_DNI"
    message = "El DNI debe tener exactamente 8 digitos"


class InvalidRucError(APIError):
    status_code = 422
    code = "INVALID_RUC"
    message = "El RUC debe tener exactamente 11 digitos"


class IdentityLookupUnavailableError(APIError):
    """Ambos proveedores fallaron. Nunca se propaga el motivo crudo."""

    status_code = 503
    code = "IDENTITY_LOOKUP_UNAVAILABLE"
    message = "El servicio de consulta no esta disponible en este momento"


def normalize_dni(raw: str) -> str:
    """Recorta espacios y valida el formato. Lanza ``InvalidDniError`` si falla.

    No se acepta ``1e8`` ni decimales porque nunca se convierte a numero: el
    valor recortado se compara caracter a caracter contra ocho digitos.
    """
    value = raw.strip()
    if not _DNI_PATTERN.fullmatch(value):
        raise InvalidDniError()
    return value


def normalize_ruc(raw: str) -> str:
    """Igual que :func:`normalize_dni` pero para los once digitos del RUC."""
    value = raw.strip()
    if not _RUC_PATTERN.fullmatch(value):
        raise InvalidRucError()
    return value


def mask_document(document_type: IdentityDocumentType, document: str) -> str:
    """Enmascara un documento para logs y metadatos de auditoria.

    Se conservan los dos ultimos digitos porque son suficientes para que un
    operador reconozca "el mismo documento de antes" sin poder reconstruir el
    original a partir de un log. DNI: ``******78``. RUC: ``*******6789``.
    """
    visible = 2 if document_type is IdentityDocumentType.DNI else 4
    oculto = len(document) - visible
    return ("*" * max(oculto, 0)) + document[-visible:]


def document_hash(document_type: IdentityDocumentType, document: str, *, secret: str) -> str:
    """Clave de cache y de deduplicacion, no reversible.

    La cache guarda este hash y no el documento en claro: no hace falta
    conservar una copia legible del numero para poder invalidarla o volver a
    consultarla, porque quien pide un refresco ya conoce el documento.

    Se firma con HMAC y un secreto que solo conoce el backend, no un
    ``sha256`` a secas: un DNI solo tiene cien millones de valores posibles,
    asi que quien lea la tabla de cache (o un respaldo de ella) podria
    reconstruir el hash de cada uno de esos valores y recuperar el documento
    original en minutos. El secreto hace esa enumeracion inviable.
    """
    payload = f"{document_type.value}:{document}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
