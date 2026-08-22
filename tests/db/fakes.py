"""Dobles para las pruebas con base de datos."""

from __future__ import annotations

from app.services.storage import ObjectStorage, StorageOperationError


class FakeObjectStorage(ObjectStorage):
    """Almacen en memoria. Registra las operaciones para poder afirmarlas."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.uploads: list[str] = []
        self.deletes: list[str] = []
        #: Si se activa, toda operacion falla: simula Storage caido.
        self.fail = False

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        if self.fail:
            raise StorageOperationError()
        self.objects[path] = (data, content_type)
        self.uploads.append(path)

    async def download(self, path: str) -> bytes:
        if self.fail or path not in self.objects:
            raise StorageOperationError()
        return self.objects[path][0]

    async def delete(self, path: str) -> None:
        if self.fail:
            raise StorageOperationError()
        self.objects.pop(path, None)
        self.deletes.append(path)
