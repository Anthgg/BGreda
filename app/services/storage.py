"""Validacion y almacenamiento de archivos de la aplicacion.

Reglas de seguridad aplicadas al logo:

- La extension y el ``Content-Type`` declarados por el cliente **no se creen**.
  El tipo real se deduce de los bytes iniciales del archivo (numeros magicos) y
  debe coincidir con lo declarado.
- SVG queda excluido: admite scripts embebidos y no existe todavia una
  sanitizacion segura en el proyecto.
- El nombre original **jamas** determina la ruta interna. La ruta la genera el
  backend con un identificador aleatorio, de modo que el path traversal es
  imposible por construccion, no por filtrado.
- El bucket es privado. El frontend nunca habla con Storage: pide el logo al
  backend, que lo sirve.
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod

import httpx

from app.core.config import Settings
from app.core.errors import APIError

#: Tipos admitidos y su extension canonica.
ALLOWED_IMAGE_TYPES: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

#: Extensiones que el cliente puede declarar para cada tipo real.
ACCEPTED_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/png": frozenset({"png"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/webp": frozenset({"webp"}),
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class LogoValidationError(APIError):
    status_code = 422
    code = "LOGO_INVALID"
    message = "El archivo no es una imagen valida"


class StorageUnavailableError(APIError):
    status_code = 503
    code = "STORAGE_NOT_CONFIGURED"
    message = "El almacenamiento de archivos no esta configurado"


class StorageOperationError(APIError):
    status_code = 502
    code = "STORAGE_ERROR"
    message = "No se pudo completar la operacion de almacenamiento"


# ---------------------------------------------------------------------------
# Deteccion de tipo real
# ---------------------------------------------------------------------------
def sniff_content_type(data: bytes) -> str | None:
    """Deduce el tipo real a partir de los primeros bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_extension(filename: str | None) -> str:
    """Extension declarada, normalizada y sin componentes de ruta."""
    if not filename:
        return ""
    # Se descarta cualquier separador antes de mirar la extension: un nombre
    # como "../../etc/passwd.png" queda reducido a "passwd.png".
    base = _SAFE_NAME.sub("_", filename.replace("\\", "/").split("/")[-1])
    _, separator, extension = base.rpartition(".")
    if not separator:
        # Sin punto no hay extension: "logo" no declara ninguna.
        return ""
    return extension.lower()


def validate_logo(
    *,
    data: bytes,
    filename: str | None,
    declared_content_type: str | None,
    max_bytes: int,
) -> str:
    """Valida el archivo y devuelve su tipo real. Lanza ``APIError`` si falla."""
    if not data:
        raise LogoValidationError("El archivo esta vacio", code="LOGO_EMPTY")

    if len(data) > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise LogoValidationError(
            f"El archivo supera el maximo permitido de {limit_mb:.1f} MB",
            code="LOGO_TOO_LARGE",
        )

    real_type = sniff_content_type(data)
    if real_type is None or real_type not in ALLOWED_IMAGE_TYPES:
        permitidos = ", ".join(sorted(ALLOWED_IMAGE_TYPES))
        raise LogoValidationError(
            f"Formato no admitido. Formatos permitidos: {permitidos}",
            code="LOGO_TYPE_NOT_ALLOWED",
        )

    # El tipo declarado debe coincidir con el real: no se acepta un PNG que
    # dice ser otra cosa ni al reves.
    if declared_content_type:
        declared = declared_content_type.split(";")[0].strip().lower()
        if declared and declared != real_type:
            raise LogoValidationError(
                "El tipo declarado no coincide con el contenido del archivo",
                code="LOGO_TYPE_MISMATCH",
            )

    extension = safe_extension(filename)
    if extension and extension not in ACCEPTED_EXTENSIONS[real_type]:
        raise LogoValidationError(
            "La extension del archivo no corresponde a su contenido",
            code="LOGO_EXTENSION_MISMATCH",
        )

    return real_type


def build_logo_path(content_type: str) -> str:
    """Ruta interna del logo. La genera el backend, nunca el cliente."""
    return f"company/logo-{uuid.uuid4().hex}.{ALLOWED_IMAGE_TYPES[content_type]}"


# ---------------------------------------------------------------------------
# Almacenamiento
# ---------------------------------------------------------------------------
class ObjectStorage(ABC):
    """Contrato de almacenamiento de objetos.

    Abstraerlo permite sustituirlo por un doble en las pruebas, de modo que la
    suite no necesita red ni credenciales.
    """

    @abstractmethod
    async def upload(self, path: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def download(self, path: str) -> bytes: ...

    @abstractmethod
    async def delete(self, path: str) -> None: ...


class SupabaseObjectStorage(ObjectStorage):
    """Implementacion sobre la API REST de Supabase Storage."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.storage_configured:
            raise StorageUnavailableError()
        self._base_url = f"{settings.SUPABASE_URL}/storage/v1/object"
        self._bucket = settings.SUPABASE_STORAGE_BUCKET
        self._key = settings.SUPABASE_SECRET_KEY.get_secret_value()
        self._client = client or httpx.AsyncClient(timeout=settings.SUPABASE_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._key}", "apikey": self._key}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{self._bucket}/{path}"

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        try:
            response = await self._client.post(
                self._url(path),
                content=data,
                headers={**self._headers(content_type), "x-upsert": "true"},
            )
        except httpx.HTTPError as exc:
            raise StorageOperationError() from exc
        if response.status_code >= 400:
            raise StorageOperationError()

    async def download(self, path: str) -> bytes:
        try:
            response = await self._client.get(self._url(path), headers=self._headers())
        except httpx.HTTPError as exc:
            raise StorageOperationError() from exc
        if response.status_code >= 400:
            raise StorageOperationError()
        return response.content

    async def delete(self, path: str) -> None:
        try:
            response = await self._client.delete(self._url(path), headers=self._headers())
        except httpx.HTTPError as exc:
            raise StorageOperationError() from exc
        # Un 404 significa que ya no esta: el resultado buscado se cumple.
        if response.status_code >= 400 and response.status_code != 404:
            raise StorageOperationError()
