"""Superficie HTTP del Cotizador V2.

Ruta propia —`/api/v1/quotations-v2`— y no una variante de `/quotations`. Un
segmento distinto significa que ningun path de V2 puede caer por accidente en
un handler de Legacy ni al reves: no hay un solo patron que case con los dos.

Rutas delgadas, como el resto de la API: el router traduce HTTP y delega. Lo
que decidira dinero vivira en el servicio, donde se puede probar sin levantar
una peticion.

Quien cotiza es administracion, igual que en Legacy: poner un precio no es
ejecutar el trabajo. Desde 010H tambien lo es emitir, cancelar, duplicar y
pasar a produccion: son decisiones comerciales.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from sqlalchemy import select

from app.api.deps import (
    AdminUserDep,
    DbSessionDep,
    V2LifecycleServiceDep,
    V2QuotationPdfServiceDep,
    V2QuotationServiceDep,
)
from app.core.quoter_v2_lifecycle import V2EffectiveStatus, effective_status
from app.models.audit import AuditAction, AuditEvent
from app.models.quoter_v2 import V2ProductionHandoff, V2QuotationStatus
from app.schemas.quoter_v2 import (
    V2BlockerOut,
    V2CancelIn,
    V2ConfirmationPreviewOut,
    V2ConfirmIn,
    V2DuplicateOut,
    V2DuplicateWarningOut,
    V2HistoryEventOut,
    V2PreviewLineOut,
    V2ProductionHandoffOut,
    V2QuotationCreateIn,
    V2QuotationListItemOut,
    V2QuotationOut,
    V2QuotationPage,
    V2QuotationUpdateIn,
    V2SendToProductionOut,
)
from app.services.quoter_v2 import V2_QUOTATION_ENTITY
from app.services.quoter_v2_firing import refresh_firing
from app.services.quoter_v2_lifecycle import LifecycleView
from app.services.quoter_v2_pricing import refresh_pricing

router = APIRouter(prefix="/quotations-v2", tags=["cotizador-v2"])


def _handoff_out(fila: V2ProductionHandoff) -> V2ProductionHandoffOut:
    return V2ProductionHandoffOut(
        id=fila.id,
        v2_quotation_id=fila.v2_quotation_id,
        status=fila.status,
        created_at=fila.created_at,
        created_by_name=fila.created_by_name,
    )


def _present(vista: LifecycleView) -> V2QuotationOut:
    fila = vista.quotation
    return V2QuotationOut(
        id=fila.id,
        code=fila.code,
        pricing_engine_version=fila.pricing_engine_version,
        status=fila.status,
        production_type=fila.production_type,
        customer_id=fila.customer_id,
        customer_name=fila.customer_name_snapshot,
        name=fila.name,
        notes=fila.notes,
        customer_kind=fila.customer_kind,
        tax_percent=fila.tax_percent_snapshot,
        currency_code=fila.currency_code_snapshot,
        currency_symbol=fila.currency_symbol_snapshot,
        exchange_rate=fila.exchange_rate_snapshot,
        validity_days=fila.validity_days_snapshot,
        workday_hours=fila.workday_hours_snapshot,
        space_service_cost_per_day=fila.space_service_cost_per_day_snapshot,
        administrative_cost=fila.administrative_cost_snapshot,
        commercial_factor=fila.commercial_factor,
        commercial_factor_min=fila.commercial_factor_min_snapshot,
        commercial_factor_max=fila.commercial_factor_max_snapshot,
        low_fire_enabled=fila.low_fire_enabled,
        high_fire_enabled=fila.high_fire_enabled,
        settings_version=fila.settings_version_snapshot,
        created_at=fila.created_at,
        updated_at=fila.updated_at,
        client_notes=fila.client_notes,
        effective_status=vista.effective_status,
        issued_at=fila.issued_at,
        valid_until=fila.valid_until,
        expires_at=fila.expires_at,
        issued_by_name=fila.issued_by_name,
        cancelled_at=fila.cancelled_at,
        cancelled_by_name=fila.cancelled_by_name,
        cancel_reason=fila.cancel_reason,
        duplicated_from_id=fila.duplicated_from_id,
        open_duplicate_id=vista.open_duplicate_id,
        production_handoff=_handoff_out(vista.handoff) if vista.handoff else None,
    )


@router.get("", response_model=V2QuotationPage)
async def list_v2_quotations(
    service: V2QuotationServiceDep,
    lifecycle: V2LifecycleServiceDep,
    session: DbSessionDep,
    _: AdminUserDep,
    status_filter: Annotated[V2QuotationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> V2QuotationPage:
    filas, total = await service.list(status=status_filter, limit=limit, offset=offset)
    ids = [fila.id for fila in filas]
    # Una sola consulta para saber cuales pasaron a produccion, no una por fila.
    con_puente = (
        set(
            (
                await session.scalars(
                    select(V2ProductionHandoff.v2_quotation_id).where(
                        V2ProductionHandoff.v2_quotation_id.in_(ids)
                    )
                )
            ).all()
        )
        if ids
        else set()
    )
    ahora = await lifecycle.db_now()
    return V2QuotationPage(
        items=[
            V2QuotationListItemOut(
                id=fila.id,
                code=fila.code,
                pricing_engine_version=fila.pricing_engine_version,
                status=fila.status,
                production_type=fila.production_type,
                customer_name=fila.customer_name_snapshot,
                name=fila.name,
                created_at=fila.created_at,
                effective_status=effective_status(
                    status=fila.status.value,
                    expires_at=fila.expires_at,
                    has_production_handoff=fila.id in con_puente,
                    now=ahora,
                ),
                valid_until=fila.valid_until,
            )
            for fila in filas
        ],
        total=total,
    )


@router.post("", response_model=V2QuotationOut, status_code=status.HTTP_201_CREATED)
async def create_v2_quotation(
    payload: V2QuotationCreateIn,
    service: V2QuotationServiceDep,
    lifecycle: V2LifecycleServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2QuotationOut:
    fila = await service.create_draft(payload.model_dump(), user=admin)
    resultado = _present(await lifecycle.view(fila))
    await session.commit()
    return resultado


@router.put("/{quotation_id}", response_model=V2QuotationOut)
async def update_v2_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2QuotationUpdateIn,
    service: V2QuotationServiceDep,
    lifecycle: V2LifecycleServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2QuotationOut:
    """Cambia la cabecera de un borrador: cliente, nombre, moneda, tipo.

    `exclude_unset` mantiene la semantica parcial de toda la familia. Aqui
    importa mas que en ningun otro sitio: el flujo de 010G deja volver atras, y
    abrir el primer paso para mirar no puede reescribir lo que ya se decidio.
    """
    fila = await service.update_draft(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    # La moneda y el tipo de produccion mueven el horno y, con el, todo el
    # precio: recalcular aqui evita que el resumen ensene cifras de antes.
    await refresh_firing(session, fila)
    await refresh_pricing(session, fila)
    # `updated_at` se calcula con `onupdate=func.now()`, de modo que el UPDATE
    # la deja expirada y leerla exige otra consulta. Se pide explicitamente:
    # dejar que el atributo se cargue solo revienta con `MissingGreenlet`,
    # porque una carga perezosa no puede esperar a nadie desde codigo sincrono.
    #
    # Primero el flush, para que los recalculos de arriba esten escritos: un
    # refresco sobre cambios sin consolidar los sustituye por lo que haya en la
    # base. Y SOLO `updated_at`: un refresco a ciegas expira tambien el resto
    # de la fila y obliga a releerla entera para nada.
    await session.flush()
    await session.refresh(fila, attribute_names=["updated_at"])
    resultado = _present(await lifecycle.view(fila))
    await session.commit()
    return resultado


@router.get("/{quotation_id}", response_model=V2QuotationOut)
async def read_v2_quotation(
    # `ge=1`: un id no positivo se rechaza en la capa HTTP, sin llegar a
    # consultar la base para acabar en el mismo 404.
    quotation_id: Annotated[int, Path(ge=1)],
    lifecycle: V2LifecycleServiceDep,
    _: AdminUserDep,
) -> V2QuotationOut:
    return _present(await lifecycle.get_view(quotation_id))


# ---------------------------------------------------------------------------
# Fase 010H: ciclo de vida
# ---------------------------------------------------------------------------
@router.get("/{quotation_id}/confirmation-preview", response_model=V2ConfirmationPreviewOut)
async def preview_v2_confirmation(
    quotation_id: Annotated[int, Path(ge=1)],
    lifecycle: V2LifecycleServiceDep,
    _: AdminUserDep,
) -> V2ConfirmationPreviewOut:
    """El resumen que se revisa antes de emitir. Lectura: no confirma nada."""
    resumen = await lifecycle.preview(quotation_id)
    vista = await lifecycle.view(resumen.quotation)
    q = resumen.quotation
    return V2ConfirmationPreviewOut(
        quotation_id=q.id,
        code=q.code,
        status=q.status,
        effective_status=vista.effective_status,
        can_confirm=q.status is V2QuotationStatus.DRAFT and not resumen.blockers,
        blockers=[V2BlockerOut(**b.as_dict()) for b in resumen.blockers],
        warnings=resumen.warnings,
        fingerprint=resumen.fingerprint,
        customer_name=q.customer_name_snapshot,
        name=q.name,
        client_notes=q.client_notes,
        currency_code=q.currency_code_snapshot,
        currency_symbol=q.currency_symbol_snapshot,
        exchange_rate=q.exchange_rate_snapshot,
        tax_percent=q.tax_percent_snapshot,
        commercial_factor=q.commercial_factor,
        validity_days=resumen.validity_days,
        valid_until=resumen.projected_valid_until,
        subtotal_amount=q.subtotal_amount,
        tax_amount=q.tax_amount,
        total_amount=q.total_amount,
        lines=[
            V2PreviewLineOut(
                id=linea.id,
                product_name=linea.product_name_snapshot,
                quantity=linea.quantity,
                length_cm=linea.length_cm,
                width_cm=linea.width_cm,
                height_cm=linea.height_cm,
                client_observation=linea.client_observation,
                unit_price=linea.unit_price,
                line_subtotal=linea.line_subtotal,
                line_tax=linea.line_tax,
                line_total=linea.line_total,
            )
            for linea in resumen.lines
        ],
    )


@router.post("/{quotation_id}/confirm", response_model=V2QuotationOut)
async def confirm_v2_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2ConfirmIn,
    lifecycle: V2LifecycleServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
    response: Response,
) -> V2QuotationOut:
    """Emite y congela. El doble clic devuelve la misma emision con 200."""
    fila, _emitida_ahora = await lifecycle.confirm(
        quotation_id, payload.expected_fingerprint, user=admin
    )
    await session.flush()
    await session.refresh(fila, attribute_names=["updated_at"])
    resultado = _present(await lifecycle.view(fila))
    await session.commit()
    return resultado


@router.post("/{quotation_id}/cancel", response_model=V2QuotationOut)
async def cancel_v2_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2CancelIn,
    lifecycle: V2LifecycleServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2QuotationOut:
    fila, _ = await lifecycle.cancel(quotation_id, payload.reason, user=admin)
    await session.flush()
    await session.refresh(fila, attribute_names=["updated_at"])
    resultado = _present(await lifecycle.view(fila))
    await session.commit()
    return resultado


@router.post("/{quotation_id}/duplicate", response_model=V2DuplicateOut)
async def duplicate_v2_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    lifecycle: V2LifecycleServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
    response: Response,
) -> V2DuplicateOut:
    """Nueva cotizacion recotizada con lo de hoy. 201 si nace, 200 si ya estaba abierta."""
    resultado = await lifecycle.duplicate(quotation_id, user=admin)
    await session.flush()
    await session.refresh(resultado.quotation, attribute_names=["updated_at"])
    salida = V2DuplicateOut(
        quotation=_present(await lifecycle.view(resultado.quotation)),
        created=resultado.created,
        warnings=[V2DuplicateWarningOut(**aviso) for aviso in resultado.warnings],
    )
    await session.commit()
    response.status_code = status.HTTP_201_CREATED if resultado.created else status.HTTP_200_OK
    return salida


@router.post("/{quotation_id}/send-to-production", response_model=V2SendToProductionOut)
async def send_v2_quotation_to_production(
    quotation_id: Annotated[int, Path(ge=1)],
    lifecycle: V2LifecycleServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
    response: Response,
) -> V2SendToProductionOut:
    """Deja la cotizacion lista para produccion. No consume inventario."""
    puente, creado = await lifecycle.send_to_production(quotation_id, user=admin)
    salida = V2SendToProductionOut(handoff=_handoff_out(puente), created=creado)
    await session.commit()
    response.status_code = status.HTTP_201_CREATED if creado else status.HTTP_200_OK
    return salida


@router.get(
    "/{quotation_id}/pdf",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "PDF del cliente."},
        409: {"description": "La cotizacion todavia es un borrador."},
    },
)
async def download_v2_quotation_pdf(
    quotation_id: Annotated[int, Path(ge=1)],
    lifecycle: V2LifecycleServiceDep,
    pdf: V2QuotationPdfServiceDep,
    _: AdminUserDep,
) -> Response:
    """El documento del cliente, dibujado SOLO con lo que se congelo al emitir."""
    vista = await lifecycle.get_view(quotation_id)
    contenido, nombre = await pdf.render(
        vista.quotation, expired=vista.effective_status is V2EffectiveStatus.EXPIRED
    )
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
            # Un documento comercial con datos del cliente no se guarda en
            # caches intermedias.
            "Cache-Control": "no-store",
        },
    )


@router.get("/{quotation_id}/history", response_model=list[V2HistoryEventOut])
async def v2_quotation_history(
    quotation_id: Annotated[int, Path(ge=1)],
    lifecycle: V2LifecycleServiceDep,
    session: DbSessionDep,
    _: AdminUserDep,
) -> list[V2HistoryEventOut]:
    """Los hechos del ciclo de vida: alta, emision, cancelacion, duplicacion, produccion.

    Solo los hechos, no cada edicion de un borrador: esas ya tienen su propia
    auditoria y mezclarlas aqui enterraria la emision entre cien cambios de
    cantidad.
    """
    await lifecycle.get_view(quotation_id)
    eventos = (
        await session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == V2_QUOTATION_ENTITY,
                AuditEvent.entity_id == str(quotation_id),
            )
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        )
    ).all()
    salida: list[V2HistoryEventOut] = []
    for evento in eventos:
        datos = evento.event_metadata or {}
        if evento.action == AuditAction.CREATE:
            nombre = "CREATED"
        elif isinstance(datos.get("event"), str):
            nombre = str(datos["event"])
        else:
            continue
        salida.append(
            V2HistoryEventOut(
                event=nombre,
                at=evento.created_at,
                user_name=evento.user_display_name,
                details={
                    clave: (None if valor is None else str(valor))
                    for clave, valor in datos.items()
                    if clave
                    in {
                        "code",
                        "valid_until",
                        "validity_days",
                        "new_id",
                        "new_code",
                        "source_id",
                        "source_code",
                        "handoff_id",
                        "was_issued",
                        "warnings",
                    }
                },
            )
        )
    return salida
