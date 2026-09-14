"""Correccion 010H — habilitar una tecnica a un trabajador desde las pruebas.

Desde que el trabajador tiene sus tecnicas configuradas, el backend rechaza una
tarea con una tecnica que la persona no tiene. Las pruebas anteriores armaban
tareas con cualquier combinacion: antes de crear una, la habilitan por la MISMA
API que usa la pantalla del maestro, con su version.
"""

from __future__ import annotations

import httpx

WORKERS = "/api/v1/quoter-v2/workers"


async def habilitar(api: httpx.AsyncClient, csrf: str, worker_id: int, *technique_ids: int) -> None:
    """Suma esas tecnicas a las que el trabajador ya tiene habilitadas."""
    fichas = (await api.get(WORKERS)).json()["items"]
    ficha = next(item for item in fichas if item["id"] == worker_id)
    deseadas = sorted(set(ficha["technique_ids"]) | set(technique_ids))
    if deseadas == sorted(ficha["technique_ids"]):
        return
    respuesta = await api.put(
        f"{WORKERS}/{worker_id}",
        json={"expected_version": ficha["version"], "technique_ids": deseadas},
        headers={"X-CSRF-Token": csrf},
    )
    assert respuesta.status_code == 200, respuesta.text
