"""Seguimiento publico de una orden de produccion.

**Esta es una superficie distinta de la API interna, no un permiso relajado de
la misma.** No comparte esquemas con `/production-orders`, no expone
identificadores y no tiene ni una operacion que escriba: solo GET.

Quien escanea el QR de una hoja de taller normalmente no tiene cuenta —es quien
lleva la pieza al horno, quien pregunta por su encargo, quien pasa por el
taller—. Antes de 009I.1 escaneaba y acababa en el login, asi que el QR
impreso no servia para nada.

Lo que se le responde esta acotado por el tipo, no por la buena memoria de
quien escriba la siguiente linea: el servicio devuelve un `PublicTrackingData`
que no tiene campos donde quepan un almacen, unos gramos o un identificador.

Reglas que sostienen esto:

- **Solo lectura.** No hay POST, PUT, PATCH ni DELETE bajo este prefijo, y hay
  una prueba que lo comprueba enumerando las rutas.
- **Respuesta unica ante cualquier fallo.** Token corto, token inexistente,
  cookie ausente o cookie caducada responden lo mismo. Distinguirlos convertiria
  el endpoint en un oraculo para adivinar tokens.
- **El identificador interno solo con sesion.** El unico endpoint que lo
  devuelve exige autenticacion, y sirve para que quien SI trabaja aqui pueda
  saltar del seguimiento a la vista interna sin volver a teclear nada.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.deps import (
    CurrentUserDep,
    ProductionOrderServiceDep,
    ProductionPdfServiceDep,
    SettingsDep,
)
from app.auth.tracking import read_tracking_token, sanitize_qr_token, set_tracking_cookie
from app.core.errors import APIError
from app.models.production import ProductionOrder
from app.schemas.tracking import PublicTrackingOut, TrackingInternalLinkOut
from app.services.production import ProductionOrderNotFoundError

router = APIRouter(prefix="/tracking", tags=["seguimiento"])


class TrackingNotFoundError(APIError):
    """La respuesta unica de esta superficie.

    Un codigo propio y no `PRODUCTION_ORDER_NOT_FOUND`: aqui no se confirma ni
    se niega que exista una orden de produccion detras de nada.
    """

    status_code = 404
    code = "TRACKING_NOT_FOUND"
    message = "No se encontró un seguimiento para este código"


async def _resolve(service: ProductionOrderServiceDep, token: str | None) -> ProductionOrder:
    """Token -> orden, con la misma respuesta para todos los fallos."""
    if token is None:
        raise TrackingNotFoundError()
    try:
        return await service.get_by_qr_token(token)
    except ProductionOrderNotFoundError as exc:
        raise TrackingNotFoundError() from exc


@router.get("/production-orders/scan/{qr_token}", response_model=PublicTrackingOut)
async def scan_tracking(
    qr_token: str,
    response: Response,
    service: ProductionOrderServiceDep,
    pdf: ProductionPdfServiceDep,
    settings: SettingsDep,
) -> PublicTrackingOut:
    """Resuelve el QR y deja el contexto en una cookie.

    Es el unico sitio donde el token viaja en la URL, y solo una vez: a partir
    de aqui la aplicacion sustituye la direccion por una sin token y sigue
    consultando con la cookie. Ver `app/auth/tracking.py`.
    """
    order = await _resolve(service, sanitize_qr_token(qr_token))
    payload = PublicTrackingOut.model_validate(await pdf.build_public_data(order))
    set_tracking_cookie(response, settings, order.qr_token)
    return payload


@router.get("/production-orders/current", response_model=PublicTrackingOut)
async def current_tracking(
    request: Request,
    service: ProductionOrderServiceDep,
    pdf: ProductionPdfServiceDep,
) -> PublicTrackingOut:
    """El seguimiento del contexto vigente en este navegador."""
    order = await _resolve(service, read_tracking_token(request))
    return PublicTrackingOut.model_validate(await pdf.build_public_data(order))


@router.get(
    "/production-orders/current/document",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Constancia de seguimiento, sin datos operativos.",
        },
        404: {"description": "No hay un seguimiento vigente."},
    },
)
async def current_tracking_document(
    request: Request,
    service: ProductionOrderServiceDep,
    pdf: ProductionPdfServiceDep,
) -> Response:
    """La constancia publica.

    **No es la hoja de taller.** Esa lleva el almacen, el material preparado y
    los gramos, y sigue exigiendo sesion en `/production-orders/{id}/document`.
    Esta se compone desde el mismo dato publico que responde la API.
    """
    order = await _resolve(service, read_tracking_token(request))
    content, filename = await pdf.render_public(order)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@router.get("/production-orders/current/internal-link", response_model=TrackingInternalLinkOut)
async def current_tracking_internal_link(
    request: Request,
    service: ProductionOrderServiceDep,
    _: CurrentUserDep,
) -> TrackingInternalLinkOut:
    """Para quien tiene sesion: a que orden interna corresponde este QR.

    Vive bajo el prefijo publico porque la cookie de seguimiento esta acotada a
    el, y exige sesion como cualquier otra lectura interna. Lo que puede hacer
    despues con esa orden lo decide la matriz de 009J, no este endpoint.
    """
    order = await _resolve(service, read_tracking_token(request))
    return TrackingInternalLinkOut(production_order_id=order.id)
