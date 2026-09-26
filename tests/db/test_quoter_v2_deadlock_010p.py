"""Fase 010P W0 — el deadlock de las lecturas que recalculan, reproducido en rojo.

Lo encontro la E2E de la revision en 010O: `GET /pricing`, `/firing`,
`/reductions` y `/confirmation-preview` recalculan y escriben las lineas dentro
de su transaccion (luego la descartan). Pedidas a la vez que otra lectura o que
un `PUT` de la misma cotizacion, PostgreSQL detectaba el ciclo y una de ellas
caia con `deadlock detected` (500). El frontend lo mitiga poniendo esas
peticiones en fila (`enTurno`, 010O); el arreglo real es del backend (W1).

Esta prueba lo reproduce con sesiones independientes —una por peticion, como
en produccion— y queda en ROJO hasta que W1 imponga un solo orden de bloqueo
(la cabecera primero). No se modifica para pasar: pasa cuando el backend deja de
bloquearse en cruz.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tests.db.test_quoter_v2_lifecycle_api import cotizacion_completa, h
from tests.db.v2_escenario_base import V2

#: Rondas de peticiones simultaneas. En 010O bastaron unas pocas decenas de
#: guardados seguidos para verlo en la E2E; aqui cada ronda mezcla las cuatro
#: lecturas que recalculan con dos escrituras sobre lineas distintas.
RONDAS = 25


def _fallo(resultado: Any) -> str | None:
    if isinstance(resultado, BaseException):
        return f"{type(resultado).__name__}: {str(resultado)[:160]}"
    if isinstance(resultado, httpx.Response) and resultado.status_code >= 500:
        return f"{resultado.request.method} {resultado.request.url.path} -> {resultado.status_code}"
    return None


async def test_lecturas_que_recalculan_y_escrituras_simultaneas_no_se_bloquean_en_cruz(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    datos = await cotizacion_completa(
        api,
        admin_csrf,
        productos=(("Plato hondo", 100), ("Taza", 60), ("Fuente", 20)),
    )
    qid = datos["id"]
    lineas: list[int] = datos["lines"]

    fallos: list[str] = []
    ultima_cantidad: dict[int, int] = {}
    for ronda in range(RONDAS):
        a, b = lineas[ronda % len(lineas)], lineas[(ronda + 1) % len(lineas)]
        cantidad_a, cantidad_b = 10 + ronda, 40 + ronda
        ultima_cantidad[a], ultima_cantidad[b] = cantidad_a, cantidad_b
        resultados = await asyncio.gather(
            api.get(f"{V2}/{qid}/pricing"),
            api.get(f"{V2}/{qid}/firing"),
            api.get(f"{V2}/{qid}/confirmation-preview"),
            api.get(f"{V2}/{qid}/reductions"),
            api.put(
                f"{V2}/{qid}/products/{a}", json={"quantity": cantidad_a}, headers=h(admin_csrf)
            ),
            api.put(
                f"{V2}/{qid}/products/{b}", json={"quantity": cantidad_b}, headers=h(admin_csrf)
            ),
            return_exceptions=True,
        )
        fallos += [f"ronda {ronda}: {f}" for f in map(_fallo, resultados) if f is not None]

    assert fallos == [], "peticiones simultaneas que fallaron:\n" + "\n".join(fallos)

    # Y lo ultimo que se guardo es lo que quedo: ninguna escritura se perdio.
    lineas_finales = (await api.get(f"{V2}/{qid}/products")).json()["items"]
    for fila in lineas_finales:
        if fila["id"] in ultima_cantidad:
            assert fila["quantity"] == ultima_cantidad[fila["id"]], fila
