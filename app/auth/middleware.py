"""Middleware de proteccion CSRF.

Se aplica a todas las operaciones mutadoras bajo ``/api/``, incluido el propio
login: el *login CSRF* (forzar a una victima a iniciar sesion con la cuenta del
atacante) tambien es un ataque real y se bloquea aqui.

Las excepciones lanzadas dentro de un middleware no llegan a los manejadores de
FastAPI, por lo que la respuesta de error se construye directamente respetando
el mismo contrato uniforme.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.auth import csrf
from app.auth.cookies import CSRF_COOKIE_NAME
from app.core.config import Settings
from app.core.errors import APIError


class CsrfMiddleware(BaseHTTPMiddleware):
    """Exige un token CSRF valido en POST, PUT, PATCH y DELETE."""

    def __init__(self, app: ASGIApp, settings: Settings, protected_prefix: str = "/api/") -> None:
        super().__init__(app)
        self._settings = settings
        self._protected_prefix = protected_prefix

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._requires_csrf(request):
            try:
                csrf.validate(
                    self._settings,
                    header_token=request.headers.get(csrf.CSRF_HEADER_NAME),
                    cookie_token=request.cookies.get(CSRF_COOKIE_NAME),
                )
            except APIError as exc:
                return JSONResponse(status_code=exc.status_code, content=exc.to_payload())
        return await call_next(request)

    def _requires_csrf(self, request: Request) -> bool:
        return request.method.upper() in csrf.PROTECTED_METHODS and request.url.path.startswith(
            self._protected_prefix
        )
