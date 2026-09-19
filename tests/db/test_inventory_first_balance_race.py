"""Fase 010J, DEFERRED_01 — la carrera del PRIMER saldo positivo, contra PostgreSQL.

`apply_movement` bloquea el saldo con `SELECT ... FOR UPDATE`. Cuando el saldo
todavia NO existe no hay fila que bloquear: dos entradas simultaneas no ven
ninguna, las dos crean la suya y la segunda choca con el UNIQUE
`(product_id, location_id)`. Esa `IntegrityError` salia como un 500 y la entrada
se perdia.

Lo que se fija: con el saldo inexistente, N entradas positivas a la vez se
aplican TODAS, el saldo final es la suma exacta, hay un movimiento auditable por
entrada y ninguna respuesta es un 500. Y lo que no cambia: sin existencia, una
salida sigue rechazandose sin dejar ni saldo ni movimiento.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from tests.db.test_inventory_api import _adjust, _setup


async def _saldo(db: AsyncSession, product_id: int, location_id: int) -> Decimal | None:
    db.expire_all()
    return await db.scalar(
        select(StockBalance.quantity).where(
            StockBalance.product_id == product_id, StockBalance.location_id == location_id
        )
    )


async def _movimientos(db: AsyncSession, product_id: int, location_id: int) -> int:
    db.expire_all()
    return int(
        await db.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.product_id == product_id, StockMovement.location_id == location_id)
        )
        or 0
    )


class TestPrimerSaldoConcurrente:
    async def test_dos_entradas_de_100_sobre_saldo_inexistente_dejan_200(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        product_id, location_id = await _setup(api, admin_csrf)
        assert await _saldo(db_session, product_id, location_id) is None

        respuestas = await asyncio.gather(
            _adjust(api, admin_csrf, product_id, location_id, "100"),
            _adjust(api, admin_csrf, product_id, location_id, "100"),
        )

        assert [r.status_code for r in respuestas] == [201, 201], [r.text for r in respuestas]
        assert await _saldo(db_session, product_id, location_id) == Decimal("200")
        assert await _movimientos(db_session, product_id, location_id) == 2

    async def test_ocho_entradas_a_la_vez_se_aplican_todas(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        product_id, location_id = await _setup(api, admin_csrf)

        respuestas = await asyncio.gather(
            *(_adjust(api, admin_csrf, product_id, location_id, "12.5") for _ in range(8))
        )

        assert sorted(r.status_code for r in respuestas) == [201] * 8, [r.text for r in respuestas]
        assert await _saldo(db_session, product_id, location_id) == Decimal("100")
        assert await _movimientos(db_session, product_id, location_id) == 8
        # Cada movimiento deja el saldo que habia tras el: ninguno se aplico
        # sobre un saldo que otro ya habia cambiado sin verlo.
        db_session.expire_all()
        despues = sorted(
            (
                await db_session.scalars(
                    select(StockMovement.balance_after).where(
                        StockMovement.product_id == product_id,
                        StockMovement.location_id == location_id,
                    )
                )
            ).all()
        )
        assert despues == [Decimal("12.5") * i for i in range(1, 9)]

    async def test_una_salida_sin_existencia_sigue_rechazandose_sin_dejar_nada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        product_id, location_id = await _setup(api, admin_csrf)

        r = await _adjust(api, admin_csrf, product_id, location_id, "-5")

        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "NEGATIVE_STOCK_NOT_ALLOWED"
        assert await _saldo(db_session, product_id, location_id) is None
        assert await _movimientos(db_session, product_id, location_id) == 0
