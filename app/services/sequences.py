"""Servicio de secuencias documentales.

## Como se garantiza la atomicidad

El numero se obtiene con **una sola sentencia** ``UPDATE ... RETURNING``:

```sql
UPDATE document_sequences
   SET current_value = CASE WHEN period_key = :periodo THEN current_value + 1 ELSE 1 END,
       period_key    = :periodo
 WHERE sequence_type = :tipo
RETURNING current_value, prefix, pattern, padding
```

PostgreSQL toma un bloqueo de fila al ejecutar el UPDATE, de modo que dos
transacciones simultaneas se serializan: la segunda espera y lee el valor ya
incrementado. No hace falta ``SELECT ... FOR UPDATE`` previo ni *advisory
locking*, porque el propio UPDATE es el punto de sincronizacion.

``SELECT MAX(numero) + 1`` queda **prohibido**: entre el SELECT y el INSERT otra
transaccion puede leer el mismo maximo y ambas obtendrian el mismo correlativo.

Como red de seguridad, ``document_sequence_issues`` lleva restricciones UNIQUE
sobre ``(sequence_type, period_key, number)`` y sobre el texto renderizado: si
algun dia el contador fallara, la base de datos aborta la operacion en vez de
emitir un duplicado silencioso.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import Case, case, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.sequence_format import render
from app.models.sequence import (
    DocumentSequence,
    DocumentSequenceIssue,
    ResetPolicy,
    SequenceType,
)


class SequenceNotConfiguredError(APIError):
    status_code = 409
    code = "SEQUENCE_NOT_CONFIGURED"
    message = "La secuencia documental no esta configurada"


class SequenceInactiveError(APIError):
    status_code = 409
    code = "SEQUENCE_INACTIVE"
    message = "La secuencia documental esta desactivada"


def period_key_for(policy: ResetPolicy, moment: date) -> str:
    """Clave del periodo vigente segun la politica de reinicio."""
    if policy is ResetPolicy.NEVER:
        return ""
    if policy is ResetPolicy.YEARLY:
        return f"{moment.year:04d}"
    if policy is ResetPolicy.MONTHLY:
        return f"{moment.year:04d}-{moment.month:02d}"
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"


def preview_for(sequence: DocumentSequence, moment: date | None = None) -> str:
    """Como se veria el proximo correlativo, **sin consumirlo**.

    Es una funcion pura: no toca la base de datos ni incrementa nada. Existe
    para que la interfaz muestre un ejemplo del formato configurado.
    """
    today = moment or datetime.now(UTC).date()
    period = period_key_for(ResetPolicy(sequence.reset_policy), today)
    # Si el periodo cambio, el proximo numero sera 1.
    next_number = sequence.current_value + 1 if sequence.period_key == period else 1
    return render(
        sequence.pattern,
        prefix=sequence.prefix,
        number=next_number,
        padding=sequence.padding,
        moment=today,
    )


def _next_value_case(period: str) -> Case[int]:
    """CASE que incrementa dentro del periodo y reinicia al cambiar de periodo."""
    return case(
        (DocumentSequence.period_key == literal(period), DocumentSequence.current_value + 1),
        else_=literal(1),
    )


class SequenceService:
    """Lectura, configuracion y emision de correlativos."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_sequences(self) -> list[DocumentSequence]:
        result = await self._session.execute(
            select(DocumentSequence).order_by(DocumentSequence.sequence_type)
        )
        return list(result.scalars().all())

    async def get(self, sequence_type: SequenceType) -> DocumentSequence:
        result = await self._session.execute(
            select(DocumentSequence).where(DocumentSequence.sequence_type == sequence_type)
        )
        sequence = result.scalar_one_or_none()
        if sequence is None:
            raise SequenceNotConfiguredError()
        return sequence

    async def issue(
        self,
        sequence_type: SequenceType,
        *,
        user_id: uuid.UUID | None = None,
        moment: date | None = None,
    ) -> str:
        """Entrega el siguiente correlativo de forma atomica.

        Uso interno exclusivo: lo invocaran los modulos de quema (Fase 4) y de
        cotizacion (Fase 5) al persistir un documento nuevo. **No existe ningun
        endpoint publico que consuma numeros**: nadie debe poder gastarlos a
        voluntad.

        El llamador es responsable del ``commit``: el correlativo y el documento
        se confirman juntos o no se confirma ninguno.
        """
        today = moment or datetime.now(UTC).date()

        current = await self.get(sequence_type)
        if not current.active:
            raise SequenceInactiveError()

        period = period_key_for(ResetPolicy(current.reset_policy), today)

        # Punto de serializacion: el UPDATE bloquea la fila, de modo que dos
        # peticiones concurrentes obtienen valores distintos por construccion.
        result = await self._session.execute(
            update(DocumentSequence)
            .where(DocumentSequence.sequence_type == sequence_type)
            .values(current_value=_next_value_case(period), period_key=period)
            .returning(
                DocumentSequence.current_value,
                DocumentSequence.prefix,
                DocumentSequence.pattern,
                DocumentSequence.padding,
            )
            .execution_options(synchronize_session=False)
        )
        number, prefix, pattern, padding = result.one()

        formatted = render(
            pattern,
            prefix=prefix,
            number=number,
            padding=padding,
            moment=today,
        )

        # Registro inmutable: prueba de unicidad y de no reutilizacion. El texto
        # se guarda con el formato vigente hoy, asi cambiar el prefijo manana no
        # reescribe lo ya emitido.
        self._session.add(
            DocumentSequenceIssue(
                sequence_type=sequence_type,
                period_key=period,
                number=number,
                formatted_value=formatted,
                issued_by=user_id,
            )
        )
        return formatted

    async def synchronize(self, sequence_type: SequenceType, max_number: int) -> None:
        """Ajusta el contador de forma atomica si el valor maximo supera el actual.

        Se usa al confirmar importaciones masivas para sincronizar los contadores
        en una sola operacion transaccional sin incrementar fila por fila.
        """
        if max_number <= 0:
            return
        await self._session.execute(
            update(DocumentSequence)
            .where(
                DocumentSequence.sequence_type == sequence_type,
                DocumentSequence.current_value < max_number,
            )
            .values(current_value=max_number)
            .execution_options(synchronize_session=False)
        )
