"""Ensayo en seco de la conciliacion historica; abre la base en solo lectura."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.lot_reconciliation import load_lot_reconciliation
from app.db.session import normalize_database_url


def _test_database_url() -> str:
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        raise RuntimeError("Define TEST_DATABASE_URL para ejecutar el informe de lotes")
    url = normalize_database_url(raw_url)
    parts = urlsplit(url)
    database_name = parts.path.rsplit("/", maxsplit=1)[-1].casefold()
    if parts.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in database_name:
        raise RuntimeError("El informe W2 solo admite una PostgreSQL local con 'test' en el nombre")
    return url


async def _run() -> int:
    engine = create_async_engine(_test_database_url(), connect_args={"statement_cache_size": 0})
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            report, _uom_codes = await connection.run_sync(load_lot_reconciliation)
        print(
            "LOT_RECONCILIATION_REPORT: " + json.dumps(asdict(report), default=str, sort_keys=True)
        )
        return 2 if report.blocks_migration else 0
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
