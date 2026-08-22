"""Motor y sesiones asincronas de SQLAlchemy.

El motor se construye de forma perezosa: la aplicacion arranca y responde
``/live`` aunque ``DATABASE_URL`` todavia no este provista, y solo falla —de
forma explicita— cuando alguna operacion necesita realmente la base de datos.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.errors import ServiceUnavailableError

_ASYNC_DRIVER = "postgresql+asyncpg://"

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def normalize_database_url(url: str) -> str:
    """Fuerza el driver asincrono sobre una URL de PostgreSQL.

    Supabase entrega la cadena como ``postgresql://`` (o ``postgres://``);
    SQLAlchemy necesita el driver explicito para el motor asincrono.
    """
    if url.startswith(_ASYNC_DRIVER):
        return url
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return _ASYNC_DRIVER + url[len(prefix) :]
    return url


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Crea el motor asincrono a partir de la configuracion."""
    if not settings.database_configured:
        raise ServiceUnavailableError(
            "La base de datos no esta configurada",
            code="DB_NOT_CONFIGURED",
        )
    return create_async_engine(
        normalize_database_url(settings.DATABASE_URL.get_secret_value()),
        pool_pre_ping=True,
        # El pooler de Supabase opera en modo transaccion (pgbouncer): la
        # cache de sentencias preparadas de asyncpg debe quedar desactivada.
        connect_args={"statement_cache_size": 0},
    )


def get_engine() -> AsyncEngine:
    """Devuelve el motor del proceso, creandolo la primera vez."""
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_engine_from_settings(get_settings())
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Devuelve la factoria de sesiones del proceso."""
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    """Cierra el pool de conexiones (apagado de la aplicacion)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Dependencia FastAPI que entrega una sesion por peticion."""
    async with get_sessionmaker()() as session:
        yield session
