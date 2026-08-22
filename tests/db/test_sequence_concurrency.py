"""Atomicidad de la generacion de correlativos.

Es la prueba que justifica el diseno: sin ella, un duplicado solo aparecería en
produccion, en el peor momento y sobre un documento ya entregado al cliente.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.sequence import DocumentSequenceIssue, SequenceType
from app.services.sequences import SequenceService

SOLICITUDES = 20


async def _issue_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    sequence_type: SequenceType,
) -> str:
    """Emite un correlativo en su propia transaccion, como haria una peticion."""
    async with sessionmaker() as session:
        value = await SequenceService(session).issue(sequence_type)
        await session.commit()
        return value


async def test_veinte_solicitudes_concurrentes_no_repiten_numero(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    valores = await asyncio.gather(
        *(_issue_once(sessionmaker_for_tests, SequenceType.QUOTE) for _ in range(SOLICITUDES))
    )

    assert len(valores) == SOLICITUDES
    assert len(set(valores)) == SOLICITUDES, f"correlativos duplicados: {valores}"


async def test_la_serie_concurrente_es_contigua_desde_uno(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    """No basta con que sean distintos: tampoco puede haber huecos."""
    valores = await asyncio.gather(
        *(_issue_once(sessionmaker_for_tests, SequenceType.QUOTE) for _ in range(SOLICITUDES))
    )

    numeros = sorted(int(valor.rsplit("-", 1)[1]) for valor in valores)

    assert numeros == list(range(1, SOLICITUDES + 1))


async def test_cada_numero_queda_registrado_una_sola_vez(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    await asyncio.gather(
        *(_issue_once(sessionmaker_for_tests, SequenceType.QUOTE) for _ in range(SOLICITUDES))
    )

    total = (
        await db_session.execute(select(func.count()).select_from(DocumentSequenceIssue))
    ).scalar_one()
    distintos = (
        await db_session.execute(
            select(func.count(func.distinct(DocumentSequenceIssue.formatted_value)))
        )
    ).scalar_one()

    assert total == SOLICITUDES
    assert distintos == SOLICITUDES


async def test_los_contadores_de_cada_tipo_son_independientes(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    """Consumir cotizaciones no debe adelantar el contador de quemas."""
    await asyncio.gather(
        *(_issue_once(sessionmaker_for_tests, SequenceType.QUOTE) for _ in range(5))
    )
    valores_quema = await asyncio.gather(
        *(_issue_once(sessionmaker_for_tests, SequenceType.FIRING) for _ in range(3))
    )

    numeros = sorted(int(valor.rsplit("-", 1)[1]) for valor in valores_quema)

    assert numeros == [1, 2, 3]
    assert all(valor.startswith("HR-") for valor in valores_quema)


async def test_solicitudes_concurrentes_mezclando_tipos(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    tareas = [
        _issue_once(
            sessionmaker_for_tests, SequenceType.QUOTE if indice % 2 else SequenceType.FIRING
        )
        for indice in range(SOLICITUDES)
    ]

    valores = await asyncio.gather(*tareas)

    assert len(set(valores)) == SOLICITUDES
    cotizaciones = [v for v in valores if v.startswith("CTZ-")]
    quemas = [v for v in valores if v.startswith("HR-")]
    assert len(set(cotizaciones)) == len(cotizaciones)
    assert len(set(quemas)) == len(quemas)


async def test_creaciones_concurrentes_de_productos_no_duplican_codigo(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    """Dos o mas creaciones simultaneas de producto jamás colisionan."""
    valores = await asyncio.gather(
        *(_issue_once(sessionmaker_for_tests, SequenceType.PRODUCT_50) for _ in range(SOLICITUDES))
    )

    assert len(valores) == SOLICITUDES
    assert len(set(valores)) == SOLICITUDES
    numeros = sorted(int(v.replace("LAB50", "")) for v in valores)
    assert numeros == list(range(1, SOLICITUDES + 1))


async def test_creaciones_concurrentes_mezclando_familias_50_y_70(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    tareas = [
        _issue_once(
            sessionmaker_for_tests,
            SequenceType.PRODUCT_50 if indice % 2 == 0 else SequenceType.PRODUCT_70,
        )
        for indice in range(SOLICITUDES)
    ]
    valores = await asyncio.gather(*tareas)
    assert len(set(valores)) == SOLICITUDES

    prod_50 = [v for v in valores if v.startswith("LAB50")]
    prod_70 = [v for v in valores if v.startswith("LAB70")]
    assert len(set(prod_50)) == len(prod_50) == SOLICITUDES // 2
    assert len(set(prod_70)) == len(prod_70) == SOLICITUDES // 2
