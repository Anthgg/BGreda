"""Entorno de Alembic.

La URL de conexion se toma de ``DATABASE_URL`` (nunca del fichero .ini) para
que ninguna credencial quede versionada.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.db.session import create_engine_from_settings, normalize_database_url

# Importar los modelos registra las tablas en Base.metadata para autogenerate.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    settings = get_settings()
    if not settings.database_configured:
        raise RuntimeError(
            "DATABASE_URL no esta definida. Alembic necesita la credencial "
            "PostgreSQL del proyecto: la publishable key de Supabase no la sustituye."
        )
    return normalize_database_url(settings.DATABASE_URL.get_secret_value())


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse a la base de datos."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Aplica las migraciones usando el motor asincrono de la aplicacion."""
    engine = create_engine_from_settings(get_settings())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
