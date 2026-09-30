"""Fase 010P W1 — la sugerencia de pasar a «Por mayor»: aceptarla o rechazarla.

Superar el umbral de unidades del pedido (congelado al crear) solo SUGIERE: el
pedido nunca cambia solo. La persona decide:

- **Aceptar** configura todo lo de por mayor de una vez: el tipo de pedido, el
  horno por mayor de la configuracion con sus tarifas congeladas de nuevo, la
  administracion vigente y el personal por defecto (decision 4). Del personal,
  solo cambia lo que puso el SISTEMA (origen DEFAULT) o lo que nadie asigno, y
  solo si el externo por defecto sabe la tecnica. Lo elegido a mano se conserva.
  Sin externo por defecto el tipo cambia igual y se avisa para que alguien elija.
- **Rechazar** solo lo anota. No toca trabajadores, horno, administracion ni
  tipo de pedido, y la sugerencia no vuelve a aparecer sola.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.quoter_v2_rules_010p import (
    DefaultWorker,
    ProcessAssignment,
    plan_wholesale_labor,
)
from app.models.audit import AuditAction
from app.models.quoter_v2 import V2ProductionType, V2Quotation, V2QuotationStatus
from app.models.quoter_v2_labor import V2LaborAssignmentOrigin, V2QuotationLabor
from app.models.quoter_v2_processes import V2QuotationProcess
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2_firing import freeze_kiln_rates, refresh_firing
from app.services.quoter_v2_labor import V2LaborService
from app.services.quoter_v2_pricing import refresh_pricing
from app.services.quoter_v2_settings import V2SettingsService

V2_WHOLESALE_ENTITY = "v2_quotation_wholesale"

_TARIFAS_DEL_HORNO = (
    "gas_cost_low_snapshot",
    "gas_cost_high_snapshot",
    "commercial_rate_low_snapshot",
    "commercial_rate_high_snapshot",
)
_ACUERDOS_DEL_HORNO = (
    "gas_low_is_override",
    "gas_high_is_override",
    "commercial_low_is_override",
    "commercial_high_is_override",
)


class V2WholesaleQuotationNotFoundError(APIError):
    status_code = 404
    code = "V2_QUOTATION_NOT_FOUND"
    message = "La cotizacion V2 no existe"


class V2WholesaleNotEditableError(APIError):
    status_code = 409
    code = "V2_QUOTATION_NOT_EDITABLE"
    message = "Solo un borrador puede cambiar a por mayor"


class V2WholesaleService:
    """Aceptar o rechazar la sugerencia de pasar un borrador a por mayor."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit
        self._settings = V2SettingsService(session, audit)
        self._labor = V2LaborService(session, audit)

    async def apply_defaults(
        self, quotation_id: int, *, user: AuthenticatedUser
    ) -> tuple[V2Quotation, list[str]]:
        quotation = await self._draft(quotation_id)
        ajustes = await self._settings.get()
        avisos: list[str] = []

        # 1. Tipo de pedido y administracion vigente de por mayor.
        quotation.production_type = V2ProductionType.WHOLESALE
        quotation.administrative_cost_snapshot = ajustes.administrative_cost_per_quote

        # 2. Horno por mayor de la configuracion, con sus tarifas congeladas de
        # nuevo: las anteriores (y sus acuerdos) eran de otro horno.
        horno = await self._settings.suggested_kiln_for(V2ProductionType.WHOLESALE)
        if horno is not None and quotation.kiln_id != horno.id:
            quotation.kiln_id = horno.id
            quotation.kiln_name_snapshot = horno.name
            quotation.kiln_capacity_snapshot = horno.capacity_volume_cm3
            for campo in _TARIFAS_DEL_HORNO:
                setattr(quotation, campo, None)
            for marca in _ACUERDOS_DEL_HORNO:
                setattr(quotation, marca, False)
            await freeze_kiln_rates(self._session, quotation)

        # 3. Personal por defecto de por mayor (decision 4).
        avisos += await self._apply_default_labor(quotation, user=user)

        await self._session.flush()
        avisos += await refresh_firing(self._session, quotation)
        avisos += await refresh_pricing(self._session, quotation)
        self._audit.record_action(
            entity_type=V2_WHOLESALE_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "event": "WHOLESALE_DEFAULTS_APPLIED",
                "kiln_id": str(quotation.kiln_id),
                "warnings": ",".join(sorted(set(avisos))),
            },
        )
        return quotation, sorted(set(avisos))

    async def decline_suggestion(
        self, quotation_id: int, *, user: AuthenticatedUser
    ) -> V2Quotation:
        quotation = await self._draft(quotation_id)
        if quotation.wholesale_suggestion_declined_at is None:
            quotation.wholesale_suggestion_declined_at = datetime.now(UTC)
            self._audit.record_action(
                entity_type=V2_WHOLESALE_ENTITY,
                entity_id=str(quotation.id),
                action=AuditAction.UPDATE,
                user_id=user.id,
                user_display_name=user.display_name,
                metadata={"event": "WHOLESALE_SUGGESTION_DECLINED"},
            )
        await self._session.flush()
        return quotation

    async def _apply_default_labor(
        self, quotation: V2Quotation, *, user: AuthenticatedUser
    ) -> list[str]:
        procesos = list(
            (
                await self._session.scalars(
                    select(V2QuotationProcess)
                    .where(
                        V2QuotationProcess.v2_quotation_id == quotation.id,
                        V2QuotationProcess.removed_at.is_(None),
                    )
                    .order_by(V2QuotationProcess.id)
                )
            ).all()
        )
        tareas = {
            tarea.v2_quotation_process_id: tarea
            for tarea in (
                await self._session.scalars(
                    select(V2QuotationLabor).where(
                        V2QuotationLabor.v2_quotation_id == quotation.id,
                        V2QuotationLabor.v2_quotation_process_id.is_not(None),
                    )
                )
            ).all()
        }
        asignaciones = [
            ProcessAssignment(
                process_id=proceso.id,
                technique_id=proceso.technique_id,
                worker_id=tareas[proceso.id].worker_id if proceso.id in tareas else None,
                origin=tareas[proceso.id].assignment_origin if proceso.id in tareas else None,
            )
            for proceso in procesos
        ]

        trabajador = await self._settings.default_worker_for(V2ProductionType.WHOLESALE)
        externo: DefaultWorker | None = None
        if trabajador is not None:
            capacidades = await self._labor.capacities_of([trabajador.id])
            externo = DefaultWorker(
                worker_id=trabajador.id,
                technique_ids=frozenset(capacidades.get(trabajador.id, [])),
            )

        plan = plan_wholesale_labor(asignaciones, externo)
        por_id = {proceso.id: proceso for proceso in procesos}
        for cambio in plan.reassignments:
            proceso = por_id[cambio.process_id]
            tarea = tareas.get(cambio.process_id)
            if tarea is not None:
                await self._labor.update_labor(
                    quotation.id,
                    tarea.id,
                    {
                        "worker_id": cambio.worker_id,
                        "assignment_origin": V2LaborAssignmentOrigin.DEFAULT,
                    },
                    user=user,
                    recalcular=False,
                )
            else:
                await self._labor.add_labor(
                    quotation.id,
                    {
                        "v2_quotation_process_id": proceso.id,
                        "v2_quotation_product_id": proceso.v2_quotation_product_id,
                        "worker_id": cambio.worker_id,
                        "technique_id": proceso.technique_id,
                        "quantity": proceso.quantity,
                        "assignment_origin": V2LaborAssignmentOrigin.DEFAULT,
                    },
                    user=user,
                )
        return list(plan.warnings)

    async def _draft(self, quotation_id: int) -> V2Quotation:
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
        if quotation is None:
            raise V2WholesaleQuotationNotFoundError()
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2WholesaleNotEditableError()
        return quotation


__all__ = ["V2WholesaleService"]
