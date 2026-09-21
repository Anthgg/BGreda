"""Superficie HTTP de Solo Quema V2 (fase 010K).

Un recurso propio, `/firing-quotations-v2`, que no toca ninguna ruta de la
quema Legacy ni del Cotizador V2: son documentos distintos con talonario
distinto, y montar uno encima del otro habria acoplado dos motores.

## Por que es de administracion

Todo lo que devuelve la lectura interna es margen: gas, costo real, ganancia y
la comparacion de tarifas entre hornos. Como el resto del Cotizador V2, es
ADMIN. El operador recibe 403.

La vista del CLIENTE —lo que dira el documento— vive en `/preview` y en el PDF,
y no lleva ni ocupacion, ni gas, ni factor, ni ganancia.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import (
    AdminUserDep,
    DbSessionDep,
    V2FiringQuotationPdfServiceDep,
    V2FiringQuotationServiceDep,
)
from app.core.quoter_v2_lifecycle import V2EffectiveStatus
from app.models.firing_quotation_v2 import (
    FIRING_QUOTATION_FACTOR_MAX,
    FIRING_QUOTATION_FACTOR_MIN,
    V2FiringProductionHandoff,
)
from app.schemas.firing_quotation_v2 import (
    V2FiringModeQuoteOut,
    V2FiringProductionHandoffOut,
    V2FiringQuotationBlockerOut,
    V2FiringQuotationCancelIn,
    V2FiringQuotationConfirmIn,
    V2FiringQuotationCreateIn,
    V2FiringQuotationDuplicateOut,
    V2FiringQuotationKilnOut,
    V2FiringQuotationLineIn,
    V2FiringQuotationLineOut,
    V2FiringQuotationListItemOut,
    V2FiringQuotationOut,
    V2FiringQuotationPage,
    V2FiringQuotationPreviewLineOut,
    V2FiringQuotationPreviewOut,
    V2FiringQuotationSuggestionOut,
    V2FiringQuotationUpdateIn,
    V2FiringSendToProductionOut,
)
from app.services.firing_quotation_v2 import (
    FiringQuotationPreview,
    FiringQuotationState,
    service_label,
)

router = APIRouter(prefix="/firing-quotations-v2", tags=["solo-quema-v2"])


def _handoff_out(fila: V2FiringProductionHandoff) -> V2FiringProductionHandoffOut:
    return V2FiringProductionHandoffOut(
        id=fila.id,
        v2_firing_quotation_id=fila.v2_firing_quotation_id,
        created_at=fila.created_at,
        created_by_name=fila.created_by_name,
    )


def _modo(quote: object | None) -> V2FiringModeQuoteOut | None:
    if quote is None:
        return None
    return V2FiringModeQuoteOut(
        billed_load=quote.billed_load,  # type: ignore[attr-defined]
        commercial=quote.commercial,  # type: ignore[attr-defined]
        gas=quote.gas,  # type: ignore[attr-defined]
    )


def _out(estado: FiringQuotationState) -> V2FiringQuotationOut:
    fila = estado.quotation
    return V2FiringQuotationOut(
        id=fila.id,
        code=fila.code,
        status=fila.status,
        effective_status=estado.effective_status,
        name=fila.name,
        notes=fila.notes,
        client_notes=fila.client_notes,
        customer_id=fila.customer_id,
        customer_name=fila.customer_name_snapshot,
        customer_kind=fila.customer_kind,
        currency_code=fila.currency_code_snapshot,
        currency_symbol=fila.currency_symbol_snapshot,
        exchange_rate=fila.exchange_rate_snapshot,
        tax_percent=fila.tax_percent_snapshot,
        rounding_step=fila.rounding_step_snapshot,
        validity_days=fila.validity_days_snapshot,
        kiln_id=fila.kiln_id,
        kiln_name=fila.kiln_name_snapshot,
        kiln_capacity_cm3=fila.kiln_capacity_snapshot,
        firing_mode=fila.firing_mode,
        low_fire_enabled=fila.low_fire_enabled,
        high_fire_enabled=fila.high_fire_enabled,
        piece_separation_cm=fila.piece_separation_cm,
        total_volume_cm3=fila.total_volume_cm3,
        occupancy_percent=fila.occupancy_percent,
        firing_count=fila.firing_count,
        billed_load=fila.billed_load,
        batch_loads=list(estado.batch_loads),
        commercial_rate_low=fila.commercial_rate_low_snapshot,
        commercial_rate_high=fila.commercial_rate_high_snapshot,
        gas_cost_low=fila.gas_cost_low_snapshot,
        gas_cost_high=fila.gas_cost_high_snapshot,
        firing_commercial_total=fila.firing_commercial_total,
        firing_gas_total=fila.firing_gas_total,
        glaze_enabled=fila.glaze_enabled,
        glaze_grams=fila.glaze_grams,
        glaze_cost_source=fila.glaze_cost_source,
        glaze_material_id=fila.glaze_material_id,
        glaze_material_name=fila.glaze_material_name_snapshot,
        glaze_manual_cost_per_gram=fila.glaze_manual_cost_per_gram,
        glaze_cost_per_gram=fila.glaze_cost_per_gram_snapshot,
        glaze_material_cost=fila.glaze_material_cost,
        glaze_labor_enabled=fila.glaze_labor_enabled,
        glaze_labor_worker_id=fila.glaze_labor_worker_id,
        glaze_labor_worker_name=fila.glaze_labor_worker_name_snapshot,
        glaze_labor_worker_type=fila.glaze_labor_worker_type_snapshot,
        glaze_labor_technique_id=fila.glaze_labor_technique_id,
        glaze_labor_technique_name=fila.glaze_labor_technique_name_snapshot,
        glaze_labor_quantity=fila.glaze_labor_quantity,
        glaze_labor_hours=fila.glaze_labor_hours,
        glaze_labor_cost=fila.glaze_labor_cost,
        factor=fila.factor,
        factor_min=FIRING_QUOTATION_FACTOR_MIN,
        factor_max=FIRING_QUOTATION_FACTOR_MAX,
        base_amount=fila.base_amount,
        commercial_price=fila.commercial_price,
        subtotal_amount=fila.subtotal_amount,
        tax_amount=fila.tax_amount,
        total_amount=fila.total_amount,
        real_cost_total=fila.real_cost_total,
        estimated_profit=fila.estimated_profit,
        effective_margin_percent=fila.effective_margin_percent,
        kilns=[
            V2FiringQuotationKilnOut(
                kiln_id=horno.kiln_id,
                name=horno.name,
                capacity_cm3=horno.capacity_cm3,
                active=horno.active,
                selected=horno.selected,
                occupancy_percent=horno.quote.occupancy_percent,
                firing_count=horno.quote.firing_count,
                batch_loads=list(horno.quote.batch_loads),
                shared=_modo(horno.quote.shared),
                exclusive=_modo(horno.quote.exclusive),
            )
            for horno in estado.kilns
        ],
        suggestion=(
            V2FiringQuotationSuggestionOut(
                kiln_id=estado.suggestion.kiln_id,
                name=estado.suggestion.name,
                commercial=estado.suggestion.commercial,
                savings=estado.suggestion.savings,
            )
            if estado.suggestion is not None
            else None
        ),
        lines=[
            V2FiringQuotationLineOut(
                id=linea.id,
                sort_order=linea.sort_order,
                product_id=linea.product_id,
                product_name=linea.product_name_snapshot,
                quantity=linea.quantity,
                length_cm=linea.length_cm,
                width_cm=linea.width_cm,
                height_cm=linea.height_cm,
                separation_cm=fila.piece_separation_cm,
                unit_volume_cm3=linea.unit_volume_cm3,
                total_volume_cm3=linea.total_volume_cm3,
                volume_share_percent=linea.volume_share_percent,
            )
            for linea in estado.lines
        ],
        warnings=estado.warnings,
        issued_at=fila.issued_at,
        valid_until=fila.valid_until,
        cancelled_at=fila.cancelled_at,
        cancel_reason=fila.cancel_reason,
        duplicated_from_id=fila.duplicated_from_id,
        created_at=fila.created_at,
        created_by_name=fila.created_by_name,
    )


def _preview_out(vista: FiringQuotationPreview) -> V2FiringQuotationPreviewOut:
    fila = vista.state.quotation
    return V2FiringQuotationPreviewOut(
        id=fila.id,
        code=fila.code,
        status=fila.status,
        effective_status=vista.state.effective_status,
        can_confirm=vista.can_confirm,
        blockers=[
            V2FiringQuotationBlockerOut(code=b.code, line_id=b.line_id) for b in vista.blockers
        ],
        warnings=vista.state.warnings,
        fingerprint=vista.fingerprint,
        customer_name=vista.customer_name,
        name=fila.name,
        client_notes=fila.client_notes,
        kiln_name=fila.kiln_name_snapshot,
        service_label=vista.service_label,
        firing_mode=fila.firing_mode,
        glaze_enabled=fila.glaze_enabled,
        currency_code=fila.currency_code_snapshot,
        currency_symbol=fila.currency_symbol_snapshot,
        exchange_rate=fila.exchange_rate_snapshot,
        tax_percent=fila.tax_percent_snapshot,
        validity_days=fila.validity_days_snapshot,
        valid_until=vista.valid_until,
        subtotal_amount=fila.subtotal_amount,
        tax_amount=fila.tax_amount,
        total_amount=fila.total_amount,
        lines=[
            V2FiringQuotationPreviewLineOut(
                id=linea.id,
                product_name=linea.product_name_snapshot,
                quantity=linea.quantity,
                length_cm=linea.length_cm,
                width_cm=linea.width_cm,
                height_cm=linea.height_cm,
            )
            for linea in vista.state.lines
        ],
    )


@router.post("", response_model=V2FiringQuotationOut, status_code=status.HTTP_201_CREATED)
async def create_firing_quotation(
    payload: V2FiringQuotationCreateIn,
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationOut:
    """Abre un borrador con la configuracion de HOY y su correlativo Q-V2."""
    estado = await service.create_draft(payload.model_dump(exclude_unset=True), user=admin)
    resultado = _out(estado)
    await session.commit()
    return resultado


@router.get("", response_model=V2FiringQuotationPage)
async def list_firing_quotations(
    service: V2FiringQuotationServiceDep,
    _: AdminUserDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> V2FiringQuotationPage:
    filas, total = await service.list_quotations(limit=limit, offset=offset)
    return V2FiringQuotationPage(
        items=[
            V2FiringQuotationListItemOut(
                id=fila.id,
                code=fila.code,
                status=fila.status,
                effective_status=estado,
                name=fila.name,
                customer_name=fila.customer_name_snapshot,
                currency_code=fila.currency_code_snapshot,
                total_amount=fila.total_amount,
                created_at=fila.created_at,
                valid_until=fila.valid_until,
            )
            for fila, estado in filas
        ],
        total=total,
    )


@router.get("/{quotation_id}", response_model=V2FiringQuotationOut)
async def read_firing_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2FiringQuotationServiceDep,
    _: AdminUserDep,
) -> V2FiringQuotationOut:
    """La lectura interna. Recalcula un borrador y no confirma la transaccion."""
    return _out(await service.get_state(quotation_id))


@router.put("/{quotation_id}", response_model=V2FiringQuotationOut)
async def update_firing_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2FiringQuotationUpdateIn,
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationOut:
    """`exclude_unset` mantiene la semantica de PATCH: lo que no se manda, se conserva."""
    estado = await service.update(quotation_id, payload.model_dump(exclude_unset=True), user=admin)
    resultado = _out(estado)
    await session.commit()
    return resultado


@router.post(
    "/{quotation_id}/lines",
    response_model=V2FiringQuotationOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_firing_quotation_line(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2FiringQuotationLineIn,
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationOut:
    estado = await service.add_line(
        quotation_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _out(estado)
    await session.commit()
    return resultado


@router.put("/{quotation_id}/lines/{line_id}", response_model=V2FiringQuotationOut)
async def update_firing_quotation_line(
    quotation_id: Annotated[int, Path(ge=1)],
    line_id: Annotated[int, Path(ge=1)],
    payload: V2FiringQuotationLineIn,
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationOut:
    estado = await service.update_line(
        quotation_id, line_id, payload.model_dump(exclude_unset=True), user=admin
    )
    resultado = _out(estado)
    await session.commit()
    return resultado


@router.delete("/{quotation_id}/lines/{line_id}", response_model=V2FiringQuotationOut)
async def delete_firing_quotation_line(
    quotation_id: Annotated[int, Path(ge=1)],
    line_id: Annotated[int, Path(ge=1)],
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationOut:
    estado = await service.delete_line(quotation_id, line_id, user=admin)
    resultado = _out(estado)
    await session.commit()
    return resultado


@router.get("/{quotation_id}/preview", response_model=V2FiringQuotationPreviewOut)
async def preview_firing_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2FiringQuotationServiceDep,
    _: AdminUserDep,
) -> V2FiringQuotationPreviewOut:
    """El resumen que se revisa antes de emitir, con la huella que lo identifica."""
    return _preview_out(await service.preview(quotation_id))


@router.post("/{quotation_id}/confirm", response_model=V2FiringQuotationOut)
async def confirm_firing_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2FiringQuotationConfirmIn,
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationOut:
    """Emite y congela. El doble clic devuelve lo mismo sin reemitir."""
    fila, _creada = await service.confirm(quotation_id, payload.expected_fingerprint, user=admin)
    resultado = _out(await service.get_state(fila.id))
    await session.commit()
    return resultado


@router.post("/{quotation_id}/cancel", response_model=V2FiringQuotationOut)
async def cancel_firing_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    payload: V2FiringQuotationCancelIn,
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationOut:
    fila, _anulada = await service.cancel(quotation_id, payload.reason, user=admin)
    resultado = _out(await service.get_state(fila.id))
    await session.commit()
    return resultado


@router.post("/{quotation_id}/duplicate", response_model=V2FiringQuotationDuplicateOut)
async def duplicate_firing_quotation(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
) -> V2FiringQuotationDuplicateOut:
    """Un borrador nuevo con las tarifas, el IGV y el TC de hoy. La original no se toca."""
    estado, creada, avisos = await service.duplicate(quotation_id, user=admin)
    resultado = V2FiringQuotationDuplicateOut(
        quotation=_out(estado), created=creada, warnings=avisos
    )
    await session.commit()
    return resultado


@router.post("/{quotation_id}/send-to-production", response_model=V2FiringSendToProductionOut)
async def send_firing_quotation_to_production(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2FiringQuotationServiceDep,
    admin: AdminUserDep,
    session: DbSessionDep,
    response: Response,
) -> V2FiringSendToProductionOut:
    """Deja una Solo Quema emitida lista para produccion. No consume inventario."""
    puente, creado = await service.send_to_production(quotation_id, user=admin)
    salida = V2FiringSendToProductionOut(handoff=_handoff_out(puente), created=creado)
    await session.commit()
    response.status_code = status.HTTP_201_CREATED if creado else status.HTTP_200_OK
    return salida


@router.get(
    "/{quotation_id}/pdf",
    responses={
        200: {"content": {"application/pdf": {}}, "description": "Documento del cliente."},
    },
    response_class=Response,
)
async def download_firing_quotation_pdf(
    quotation_id: Annotated[int, Path(ge=1)],
    service: V2FiringQuotationServiceDep,
    pdf: V2FiringQuotationPdfServiceDep,
    _: AdminUserDep,
) -> Response:
    """El documento, dibujado SOLO con lo que se congelo al emitir."""
    estado = await service.get_state(quotation_id)
    contenido, nombre = await pdf.render(
        estado.quotation, expired=estado.effective_status is V2EffectiveStatus.EXPIRED
    )
    return Response(
        content=contenido,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router", "service_label"]
