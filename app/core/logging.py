"""Configuracion de logging.

Regla del proyecto: nunca se registran secretos, cuerpos de peticion ni
cabeceras de autenticacion. Los valores sensibles de la configuracion son
``SecretStr``, de modo que su representacion en un log siempre esta
enmascarada.
"""

from __future__ import annotations

import logging
from logging.config import dictConfig

from app.core.config import Settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Aplica la configuracion de logging del proceso."""
    level = settings.LOG_LEVEL.upper()
    if level not in logging.getLevelNamesMapping():
        level = "INFO"

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"default": {"format": _LOG_FORMAT}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                # El log de acceso de uvicorn incluye la ruta completa; se
                # mantiene en WARNING para no registrar trafico autenticado.
                "uvicorn.access": {"handlers": ["console"], "level": "WARNING", "propagate": False},
                "uvicorn.error": {"handlers": ["console"], "level": level, "propagate": False},
            },
        }
    )
