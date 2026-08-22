"""Health checks para Cloud Run y monitorizacion.

Ninguna respuesta revela URLs, credenciales ni detalles internos: solo el
nombre logico de cada dependencia y su estado.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.deps import SettingsDep
from app.core.config import Settings
from app.db.session import get_engine
from app.schemas.common import HealthComponent, LivenessResponse, ReadinessResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_STATUS_OK = "ok"
_STATUS_ERROR = "error"
_STATUS_NOT_CONFIGURED = "not_configured"


@router.get("/live", response_model=LivenessResponse, summary="Liveness probe")
async def live(settings: SettingsDep) -> LivenessResponse:
    """Confirma que el proceso esta vivo. No consulta dependencias."""
    return LivenessResponse(app=settings.APP_NAME, version=settings.APP_VERSION)


async def _check_database(settings: Settings) -> HealthComponent:
    if not settings.database_configured:
        return HealthComponent(name="database", status=_STATUS_NOT_CONFIGURED, required=True)
    try:
        engine = get_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("El health check de base de datos fallo")
        return HealthComponent(name="database", status=_STATUS_ERROR, required=True)
    return HealthComponent(name="database", status=_STATUS_OK, required=True)


def _check_supabase(settings: Settings) -> HealthComponent:
    # Comprobacion de configuracion, no de red: /ready debe ser barato y no
    # debe depender de la latencia de un tercero.
    configured = settings.supabase_configured
    return HealthComponent(
        name="supabase_auth",
        status=_STATUS_OK if configured else _STATUS_NOT_CONFIGURED,
        required=True,
    )


def _check_security_config(settings: Settings) -> HealthComponent:
    try:
        origins_ok = bool(settings.frontend_origins)
    except Exception:
        origins_ok = False
    ok = origins_ok and settings.csrf_configured
    return HealthComponent(
        name="security_config",
        status=_STATUS_OK if ok else _STATUS_ERROR,
        required=True,
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def ready(settings: SettingsDep, response: Response) -> ReadinessResponse:
    """Confirma que las dependencias esenciales estan disponibles."""
    components = [
        _check_security_config(settings),
        _check_supabase(settings),
        await _check_database(settings),
    ]
    healthy = all(component.status == _STATUS_OK for component in components if component.required)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if healthy else "degraded", components=components)
