"""Fase 010P W1 — un solo orden de bloqueo para todo lo que recalcula una cotizacion V2.

## El deadlock que esto cierra

`GET /pricing`, `/firing`, `/reductions` y `/confirmation-preview` recalculan
el borrador y escriben sus lineas dentro de la transaccion de la lectura (que
luego se descarta). Las escrituras (`PUT` de una linea, de una tarea, de la
quema) bloquean PRIMERO la cabecera con `SELECT ... FOR UPDATE` y despues las
lineas. Las lecturas, en cambio, no bloqueaban la cabecera: iban directo a las
lineas. Dos transacciones que toman las mismas filas en orden distinto se
esperan en cruz, y PostgreSQL mata a una con `deadlock detected` (500). La E2E
de 010O lo encontro; `tests/db/test_quoter_v2_deadlock_010p.py` lo reproducia
desde la primera ronda.

## El arreglo

Toda operacion que recalcula una cotizacion, sea lectura o escritura, bloquea
PRIMERO la cabecera. Con un unico orden de adquisicion —cabecera, luego
lineas— el ciclo es imposible: la segunda espera a la primera en la cabecera,
antes de tocar ninguna linea.

No es un reintento, ni una espera, ni un candado del proceso: es la base de
datos serializando el trabajo sobre UNA cotizacion. Dos cotizaciones distintas
siguen yendo en paralelo. La lectura sigue sin confirmar su transaccion, asi
que el bloqueo se suelta al terminar la peticion.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quoter_v2 import V2Quotation


async def lock_quotation_for_recalculation(
    session: AsyncSession, quotation_id: int
) -> V2Quotation | None:
    """La cotizacion con su cabecera BLOQUEADA, o None si no existe.

    - Si la fila NO estaba en la sesion, se lee ya bloqueada: lo que se
      recalcule parte de lo que la otra transaccion dejo confirmado.
    - Si YA estaba (una escritura la cambio en memoria y ahora recalcula), se
      bloquea sin releerla: `populate_existing` descartaria esos cambios.
    - Nunca con autoflush: volcar a la base una cabecera a medio cambiar (la
      quema apagada con su hornada aun sin recalcular) romperia sus CHECK antes
      de que el recalculo la deje coherente.
    """
    sesion = session.sync_session
    ya_cargada = sesion.identity_map.get(sesion.identity_key(V2Quotation, quotation_id))
    consulta = select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
    if ya_cargada is None:
        consulta = consulta.execution_options(populate_existing=True)
    with session.no_autoflush:
        return (await session.scalars(consulta)).one_or_none()


__all__ = ["lock_quotation_for_recalculation"]
