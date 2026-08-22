"""Manejadores de excepciones que garantizan el formato uniforme de error."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import APIError, ValidationFailedError

logger = logging.getLogger(__name__)


def _envelope(
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


def _code_for_status(status_code: int) -> str:
    """Deriva un codigo estable a partir del status HTTP (404 -> NOT_FOUND)."""
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP_ERROR"
    return phrase.upper().replace(" ", "_").replace("-", "_")


async def _api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, APIError)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(),
        headers=exc.headers,
    )


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    message = exc.detail if isinstance(exc.detail, str) else _code_for_status(exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(_code_for_status(exc.status_code), message),
        headers=getattr(exc, "headers", None),
    )


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Se exponen unicamente la ubicacion y el motivo. El campo "input" de
    # pydantic se descarta a proposito: puede contener la contrasena enviada.
    details = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
            "reason": error.get("msg", ""),
            "type": error.get("type", ""),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_envelope(ValidationFailedError.code, ValidationFailedError.message, details),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # La traza queda solo en el log del servidor; el cliente recibe un mensaje
    # generico sin detalles internos.
    logger.exception(
        "Excepcion no controlada en %s %s", request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=500,
        content=_envelope("INTERNAL_ERROR", "Ocurrio un error inesperado"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Registra los manejadores en la aplicacion FastAPI."""
    app.add_exception_handler(APIError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
