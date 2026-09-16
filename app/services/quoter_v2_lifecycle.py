"""Fase 010H — emitir, cancelar, duplicar y pasar a produccion una cotizacion V2.

Hasta 010G una cotizacion V2 solo podia ser borrador. Aqui gana el resto de su
vida comercial, y cada operacion tiene una regla que la justifica:

- **Emitir congela.** Todas las escrituras de la familia V2 exigen `DRAFT` bajo
  `SELECT ... FOR UPDATE` sobre la cabecera. Emitir toma ese MISMO bloqueo: una
  edicion simultanea espera a que la emision termine y despues encuentra la
  cotizacion emitida, o la emision espera a la edicion y despues ve la huella
  cambiada. No hay una tercera posibilidad.
- **Emitir es idempotente.** El codigo ya se emitio al crear el borrador, asi
  que emitir no gasta correlativo. El segundo clic encuentra la cotizacion
  emitida con la MISMA huella y devuelve lo mismo, sin otra auditoria.
- **Vencer no destruye.** La vencida conserva cada cifra. Solo cambia lo que se
  puede hacer con ella: no pasa a produccion con el precio de entonces.
- **Duplicar recotiza.** Un borrador nuevo, con su propio codigo, que nace de la
  configuracion de HOY y vuelve a pasar por los servicios de siempre. De la
  vieja se toman las DECISIONES —cliente, piezas, medidas, materiales, horno,
  personas— y nunca los importes.
- **Pasar a produccion no mueve inventario.** Crea el puente y nada mas.

## Aislamiento

Como el resto de V2, este modulo no importa nada de Legacy. Usa los maestros
compartidos (`partners`, `products`, configuracion comercial de la casa) y los
servicios V2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.quoter_v2_lifecycle import (
    MAX_VALIDITY_DAYS,
    V2EffectiveStatus,
    ValidityError,
    commercial_fingerprint,
    compute_validity,
    effective_status,
)
from app.models.audit import AuditAction
from app.models.masters import Partner, Product
from app.models.quoter_v2 import (
    V2ProductionHandoff,
    V2ProductionHandoffStatus,
    V2Quotation,
    V2QuotationProduct,
    V2QuotationStatus,
)
from app.models.quoter_v2_labor import V2QuotationLabor, V2Technique
from app.models.quoter_v2_processes import V2Extra, V2QuotationExtra, V2QuotationProcess
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2 import CUSTOMER_ROLES, V2_QUOTATION_ENTITY, V2QuotationService
from app.services.quoter_v2_firing import V2FiringService, refresh_firing
from app.services.quoter_v2_labor import V2LaborService
from app.services.quoter_v2_materials import V2MaterialService
from app.services.quoter_v2_pricing import refresh_pricing
from app.services.quoter_v2_settings import V2SettingsService

ZERO = Decimal(0)

#: Entidad de auditoria del puente. Distinta de la cotizacion: el historial de
#: produccion de 010I colgara de aqui.
V2_HANDOFF_ENTITY = "v2_production_handoff"

#: Lo que se escribe en `metadata.event` de la auditoria de la cotizacion. La
#: accion generica (`UPDATE`) no distingue emitir de cancelar, y el historial
#: tiene que poder contestar «quien la emitio y cuando» sin adivinar.
EVENT_CONFIRMED = "CONFIRMED"
EVENT_CANCELLED = "CANCELLED"
EVENT_DUPLICATED = "DUPLICATED"
EVENT_DUPLICATED_FROM = "DUPLICATED_FROM"
EVENT_SENT_TO_PRODUCTION = "SENT_TO_PRODUCTION"

# ---------------------------------------------------------------------------
# Codigos de lo que impide emitir. Son CODIGOS y los traduce el frontend: una
# frase escrita aqui obligaria a desplegar el backend por una errata.
# ---------------------------------------------------------------------------
BLOCK_CUSTOMER_REQUIRED = "V2_CONFIRM_CUSTOMER_REQUIRED"
BLOCK_CUSTOMER_INACTIVE = "V2_CONFIRM_CUSTOMER_INACTIVE"
BLOCK_NO_LINES = "V2_CONFIRM_NO_LINES"
BLOCK_LINE_PRODUCT = "V2_CONFIRM_LINE_PRODUCT_REQUIRED"
BLOCK_LINE_QUANTITY = "V2_CONFIRM_LINE_QUANTITY_REQUIRED"
BLOCK_LINE_BODY_MATERIAL = "V2_CONFIRM_LINE_BODY_MATERIAL_REQUIRED"
BLOCK_LINE_BODY_WEIGHT = "V2_CONFIRM_LINE_BODY_WEIGHT_REQUIRED"
BLOCK_LINE_PRICE = "V2_CONFIRM_LINE_PRICE_REQUIRED"
BLOCK_FACTOR_REQUIRED = "V2_CONFIRM_FACTOR_REQUIRED"
BLOCK_FACTOR_OUT_OF_RANGE = "V2_CONFIRM_FACTOR_OUT_OF_RANGE"
BLOCK_TAX_REQUIRED = "V2_CONFIRM_TAX_REQUIRED"
BLOCK_ROUNDING_REQUIRED = "V2_CONFIRM_ROUNDING_REQUIRED"
BLOCK_CURRENCY_REQUIRED = "V2_CONFIRM_CURRENCY_REQUIRED"
BLOCK_EXCHANGE_RATE_REQUIRED = "V2_CONFIRM_EXCHANGE_RATE_REQUIRED"
BLOCK_VALIDITY_INVALID = "V2_CONFIRM_VALIDITY_INVALID"
BLOCK_WORK_DAYS_REQUIRED = "V2_CONFIRM_WORK_DAYS_REQUIRED"
BLOCK_KILN_REQUIRED = "V2_CONFIRM_KILN_REQUIRED"
BLOCK_TOTAL_REQUIRED = "V2_CONFIRM_TOTAL_REQUIRED"

# ---------------------------------------------------------------------------
# Avisos de la duplicacion: lo que NO pudo traerse porque el maestro de hoy ya
# no lo admite. La duplicacion no se detiene por ellos —es un borrador y se
# puede completar—, pero tampoco los calla.
# ---------------------------------------------------------------------------
DUP_CUSTOMER_UNAVAILABLE = "V2_DUPLICATE_CUSTOMER_UNAVAILABLE"
DUP_PRODUCT_UNAVAILABLE = "V2_DUPLICATE_PRODUCT_UNAVAILABLE"
DUP_BODY_MATERIAL_UNAVAILABLE = "V2_DUPLICATE_BODY_MATERIAL_UNAVAILABLE"
DUP_GLAZE_MATERIAL_UNAVAILABLE = "V2_DUPLICATE_GLAZE_MATERIAL_UNAVAILABLE"
DUP_KILN_UNAVAILABLE = "V2_DUPLICATE_KILN_UNAVAILABLE"
DUP_LABOR_UNAVAILABLE = "V2_DUPLICATE_LABOR_UNAVAILABLE"
#: El concepto adicional se retiro del maestro: no se recotiza a ciegas.
DUP_EXTRA_UNAVAILABLE = "V2_DUPLICATE_EXTRA_UNAVAILABLE"
DUP_ILLUSTRATION_UNAVAILABLE = "V2_DUPLICATE_ILLUSTRATION_UNAVAILABLE"
DUP_PLANNING_UNAVAILABLE = "V2_DUPLICATE_PLANNING_UNAVAILABLE"


class V2LifecycleNotFoundError(APIError):
    status_code = 404
    code = "V2_QUOTATION_NOT_FOUND"
    message = "La cotizacion V2 no existe"


class V2QuotationChangedError(APIError):
    """Lo que se iba a emitir no es lo que se reviso."""

    status_code = 409
    code = "V2_QUOTATION_CHANGED"
    message = (
        "La cotizacion cambio despues de revisar el resumen. Revise los valores actualizados "
        "antes de confirmar"
    )


class V2QuotationIncompleteError(APIError):
    status_code = 422
    code = "V2_QUOTATION_INCOMPLETE"
    message = "La cotizacion no esta completa y no puede emitirse"


class V2QuotationAlreadyIssuedError(APIError):
    status_code = 409
    code = "V2_QUOTATION_ALREADY_ISSUED"
    message = "La cotizacion ya fue emitida con otros valores"


class V2QuotationNotConfirmableError(APIError):
    status_code = 409
    code = "V2_QUOTATION_NOT_CONFIRMABLE"
    message = "Una cotizacion cancelada no puede emitirse"


class V2QuotationNotCancellableError(APIError):
    status_code = 409
    code = "V2_QUOTATION_NOT_CANCELLABLE"
    message = "La cotizacion ya paso a produccion y no puede cancelarse desde aqui"


class V2QuotationNotDuplicableError(APIError):
    status_code = 409
    code = "V2_QUOTATION_NOT_DUPLICABLE"
    message = "Solo se puede duplicar una cotizacion vencida o cancelada"


class V2QuotationNotSendableError(APIError):
    status_code = 409
    code = "V2_QUOTATION_NOT_SENDABLE"
    message = "Solo una cotizacion emitida y vigente puede pasar a produccion"


@dataclass(frozen=True)
class Blocker:
    code: str
    line_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "line_id": self.line_id}


@dataclass(frozen=True)
class ConfirmationPreview:
    quotation: V2Quotation
    lines: list[V2QuotationProduct]
    blockers: list[Blocker]
    warnings: list[str]
    fingerprint: str
    validity_days: int | None
    projected_valid_until: date | None
    #: Lo que se imprimira del cliente y de las condiciones si se emite ahora.
    issuance: dict[str, str | None]


@dataclass(frozen=True)
class LifecycleView:
    """Lo que la pantalla necesita saber del ciclo de vida de una cotizacion."""

    quotation: V2Quotation
    effective_status: V2EffectiveStatus
    handoff: V2ProductionHandoff | None
    open_duplicate_id: int | None
    now: datetime


@dataclass
class DuplicateResult:
    quotation: V2Quotation
    created: bool
    warnings: list[dict[str, Any]] = field(default_factory=list)


def document_payload(
    quotation: V2Quotation,
    lines: list[V2QuotationProduct],
    validity_days: int | None,
    issuance: dict[str, str | None],
) -> dict[str, Any]:
    """TODO lo que el resumen de emision ensena, y nada mas.

    Es la base de la huella. Cubre lo que el cliente va a leer —cliente,
    moneda, piezas, medidas, unitarios, totales, vigencia, observaciones— y el
    factor, que quien emite ve en el resumen. El nombre de la cotizacion NO
    entra: es interno y no sale en el papel, asi que renombrarla no puede
    invalidar un resumen. Los costos internos tampoco: si
    un material se desactiva sin mover ningun unitario, el documento que se
    reviso sigue siendo exactamente el que se emite, y un 409 ahi no tendria
    explicacion posible en pantalla.

    **Lo que se congela del cliente y de las condiciones tambien entra**
    (`issuance`). Hallazgo BLOCKER del gate final de Codex: la huella no lo
    cubria y la emision lo copiaba del maestro vivo, asi que corregir la
    direccion del cliente o las condiciones de la casa entre el resumen y el
    clic emitia un PDF distinto del revisado sin ningun conflicto.
    """
    return {
        "code": quotation.code,
        "customer_id": quotation.customer_id,
        "issuance": issuance,
        "client_notes": quotation.client_notes,
        "currency_code": quotation.currency_code_snapshot,
        "exchange_rate": quotation.exchange_rate_snapshot,
        "tax_percent": quotation.tax_percent_snapshot,
        "rounding_step": quotation.rounding_step_snapshot,
        "commercial_factor": quotation.commercial_factor,
        "validity_days": validity_days,
        "subtotal": quotation.subtotal_amount,
        "tax": quotation.tax_amount,
        "total": quotation.total_amount,
        "lines": [
            {
                "id": linea.id,
                "product_id": linea.product_id,
                "product_name": linea.product_name_snapshot,
                "quantity": linea.quantity,
                "length_cm": linea.length_cm,
                "width_cm": linea.width_cm,
                "height_cm": linea.height_cm,
                "client_observation": linea.client_observation,
                "unit_price": linea.unit_price,
                "line_subtotal": linea.line_subtotal,
                "line_tax": linea.line_tax,
                "line_total": linea.line_total,
            }
            for linea in lines
        ],
    }


class V2LifecycleService:
    """Emision, cancelacion, duplicacion y puente a produccion."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        settings: V2SettingsService,
        quotations: V2QuotationService,
        materials: V2MaterialService,
        labor: V2LaborService,
        firing: V2FiringService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._settings = settings
        self._quotations = quotations
        self._materials = materials
        self._labor = labor
        self._firing = firing

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    async def db_now(self) -> datetime:
        """El reloj de la base, no el del contenedor.

        `clock_timestamp()` y no `now()`: `now()` es el inicio de la
        transaccion, y una emision que espero un bloqueo quedaria fechada
        antes de haberse producido.
        """
        instante = await self._session.scalar(select(func.clock_timestamp()))
        assert instante is not None
        return instante

    async def view(self, quotation: V2Quotation) -> LifecycleView:
        handoff = await self._handoff_of(quotation.id)
        ahora = await self.db_now()
        abierta = await self._session.scalar(
            select(V2Quotation.id).where(
                V2Quotation.duplicated_from_id == quotation.id,
                V2Quotation.status == V2QuotationStatus.DRAFT,
            )
        )
        return LifecycleView(
            quotation=quotation,
            effective_status=effective_status(
                status=quotation.status.value,
                expires_at=quotation.expires_at,
                has_production_handoff=handoff is not None,
                now=ahora,
            ),
            handoff=handoff,
            open_duplicate_id=abierta,
            now=ahora,
        )

    async def get_view(self, quotation_id: int) -> LifecycleView:
        quotation = await self._session.get(V2Quotation, quotation_id)
        if quotation is None:
            raise V2LifecycleNotFoundError()
        return await self.view(quotation)

    async def preview(self, quotation_id: int) -> ConfirmationPreview:
        """El resumen que se ensena ANTES de emitir, con su huella.

        Recalcula como cualquier lectura de un borrador —las lineas, las
        tareas o el horno pueden haber cambiado por otra via— y no confirma la
        transaccion: la ruta de lectura no persiste nada.
        """
        quotation = await self._session.get(V2Quotation, quotation_id)
        if quotation is None:
            raise V2LifecycleNotFoundError()
        avisos: list[str] = []
        if quotation.status is V2QuotationStatus.DRAFT:
            avisos += await refresh_firing(self._session, quotation)
            avisos += await refresh_pricing(self._session, quotation)
        lineas = await self._lines(quotation.id)
        dias = await self._validity_days(quotation)
        bloqueos = await self._blockers(quotation, lineas, dias)
        proyectada: date | None = None
        if quotation.status is V2QuotationStatus.DRAFT and dias is not None:
            try:
                proyectada, _ = compute_validity(await self.db_now(), dias)
            except ValidityError:
                proyectada = None
        elif quotation.valid_until is not None:
            proyectada = quotation.valid_until
        emision = await self._issuance(quotation)
        return ConfirmationPreview(
            quotation=quotation,
            lines=lineas,
            blockers=bloqueos,
            # Sin duplicados y en orden estable: la misma lista dos veces no
            # dice nada nuevo.
            warnings=sorted(set(avisos)),
            fingerprint=commercial_fingerprint(document_payload(quotation, lineas, dias, emision)),
            validity_days=dias,
            projected_valid_until=proyectada,
            issuance=emision,
        )

    # ------------------------------------------------------------------
    # Emitir
    # ------------------------------------------------------------------
    async def confirm(
        self, quotation_id: int, expected_fingerprint: str, *, user: AuthenticatedUser
    ) -> tuple[V2Quotation, bool]:
        """Emite y congela. Devuelve `(cotizacion, emitida_ahora)`.

        El orden de las comprobaciones es deliberado:

        1. **ya emitida con la misma huella** → devuelve lo mismo. Es el doble
           clic: sin segunda auditoria, sin tocar fechas;
        2. **la huella no coincide** → 409 antes de validar nada. Si otra
           persona cambio el documento, lo primero que hay que decir es eso,
           no que ahora le falta un dato;
        3. **incompleta** → 422 con cada bloqueo.

        Si algo falla, la ruta no confirma la transaccion: el recalculo previo
        tampoco queda escrito.
        """
        quotation = await self._locked(quotation_id)

        if quotation.status is V2QuotationStatus.CONFIRMED:
            if quotation.commercial_fingerprint == expected_fingerprint:
                return quotation, False
            raise V2QuotationAlreadyIssuedError()
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2QuotationNotConfirmableError()

        await refresh_firing(self._session, quotation)
        await refresh_pricing(self._session, quotation)
        lineas = await self._lines(quotation.id)
        dias = await self._validity_days(quotation)

        emision = await self._issuance(quotation)
        huella = commercial_fingerprint(document_payload(quotation, lineas, dias, emision))
        if huella != expected_fingerprint:
            raise V2QuotationChangedError()

        bloqueos = await self._blockers(quotation, lineas, dias)
        if bloqueos or dias is None:
            raise V2QuotationIncompleteError(details=[b.as_dict() for b in bloqueos])

        emitida_en = await self.db_now()
        valid_until, expires_at = compute_validity(emitida_en, dias)

        quotation.validity_days_snapshot = dias
        quotation.issued_at = emitida_en
        quotation.valid_until = valid_until
        quotation.expires_at = expires_at
        quotation.issued_by = user.id
        quotation.issued_by_name = user.display_name
        quotation.commercial_fingerprint = huella
        # Se congela EXACTAMENTE lo que entro en la huella comprobada arriba,
        # leido bajo el mismo bloqueo: ni una segunda lectura del maestro.
        quotation.customer_name_snapshot = emision["customer_name"]
        quotation.customer_document_type_snapshot = emision["customer_document_type"]
        quotation.customer_document_number_snapshot = emision["customer_document_number"]
        quotation.customer_address_snapshot = emision["customer_address"]
        quotation.customer_email_snapshot = emision["customer_email"]
        quotation.customer_phone_snapshot = emision["customer_phone"]
        quotation.conditions_snapshot = emision["conditions"]
        quotation.payment_notes_snapshot = emision["payment_notes"]
        quotation.status = V2QuotationStatus.CONFIRMED
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "event": EVENT_CONFIRMED,
                "code": quotation.code,
                "issued_at": emitida_en.isoformat(),
                "valid_until": valid_until.isoformat(),
                "validity_days": str(dias),
                "currency": str(quotation.currency_code_snapshot),
                "total_amount": str(quotation.total_amount),
                "fingerprint": huella,
            },
        )
        return quotation, True

    # ------------------------------------------------------------------
    # Cancelar
    # ------------------------------------------------------------------
    async def cancel(
        self, quotation_id: int, reason: str | None, *, user: AuthenticatedUser
    ) -> tuple[V2Quotation, bool]:
        """Cancela un borrador o una emitida que no paso a produccion.

        Idempotente: cancelar una cancelada devuelve lo mismo. No borra nada:
        una emitida cancelada conserva sus cifras y su PDF, marcado ANULADA.

        El puente se busca DESPUES de bloquear la cabecera, y el pase a
        produccion toma el mismo bloqueo: cancelar y enviar a la vez no pueden
        terminar los dos.
        """
        quotation = await self._locked(quotation_id)
        if quotation.status is V2QuotationStatus.CANCELLED:
            return quotation, False
        if await self._handoff_of(quotation.id) is not None:
            raise V2QuotationNotCancellableError()

        quotation.status = V2QuotationStatus.CANCELLED
        quotation.cancelled_at = await self.db_now()
        quotation.cancelled_by = user.id
        quotation.cancelled_by_name = user.display_name
        quotation.cancel_reason = reason
        await self._session.flush()
        self._audit.record_action(
            entity_type=V2_QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "event": EVENT_CANCELLED,
                "code": quotation.code,
                "was_issued": str(quotation.issued_at is not None),
            },
        )
        return quotation, True

    # ------------------------------------------------------------------
    # Pasar a produccion
    # ------------------------------------------------------------------
    async def send_to_production(
        self, quotation_id: int, *, user: AuthenticatedUser
    ) -> tuple[V2ProductionHandoff, bool]:
        """Crea el puente, UNA vez. Devuelve `(puente, creado_ahora)`.

        El vencimiento se decide con el reloj de la base y bajo el bloqueo: una
        oferta que vence a medianoche no pasa a produccion a las 00:00:01
        porque la pantalla se cargo a las 23:59.

        No crea orden de produccion Legacy, no toca inventario, no reserva
        material.
        """
        quotation = await self._locked(quotation_id)
        existente = await self._handoff_of(quotation.id)
        if existente is not None:
            return existente, False
        if quotation.status is not V2QuotationStatus.CONFIRMED:
            raise V2QuotationNotSendableError()
        ahora = await self.db_now()
        if quotation.expires_at is None or ahora >= quotation.expires_at:
            raise V2QuotationNotSendableError(
                "La cotizacion esta vencida: duplíquela para actualizar precios",
                code="V2_QUOTATION_EXPIRED",
            )
        assert quotation.commercial_fingerprint is not None

        puente = V2ProductionHandoff(
            v2_quotation_id=quotation.id,
            status=V2ProductionHandoffStatus.READY_FOR_PRODUCTION,
            commercial_fingerprint=quotation.commercial_fingerprint,
            created_by=user.id,
            created_by_name=user.display_name,
        )
        # SAVEPOINT: el bloqueo de la cabecera ya serializa, pero el UNIQUE es
        # la garantia final. Si una via que no tomara el bloqueo llegara
        # primero, aqui se devuelve su puente en vez de un 500.
        try:
            async with self._session.begin_nested():
                self._session.add(puente)
                await self._session.flush()
        except IntegrityError:
            ganador = await self._handoff_of(quotation.id)
            if ganador is None:
                raise
            return ganador, False

        self._audit.record_action(
            entity_type=V2_HANDOFF_ENTITY,
            entity_id=str(puente.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"v2_quotation_id": str(quotation.id), "code": quotation.code},
        )
        self._audit.record_action(
            entity_type=V2_QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"event": EVENT_SENT_TO_PRODUCTION, "handoff_id": str(puente.id)},
        )
        return puente, True

    # ------------------------------------------------------------------
    # Duplicar
    # ------------------------------------------------------------------
    async def duplicate(self, quotation_id: int, *, user: AuthenticatedUser) -> DuplicateResult:
        """Un borrador NUEVO, recotizado con lo de hoy, a partir de una vencida o cancelada.

        La original no se toca: ni estado, ni fechas, ni una cifra.

        Idempotente frente al doble clic: si ya hay un borrador abierto nacido
        de esta cotizacion, se devuelve ese. El bloqueo de la ORIGINAL
        serializa las dos peticiones, y el indice unico parcial es la garantia
        de la base si alguna via no lo tomara.
        """
        original = await self._locked(quotation_id)
        vista = await self.view(original)
        if vista.effective_status not in (
            V2EffectiveStatus.EXPIRED,
            V2EffectiveStatus.CANCELLED,
        ):
            raise V2QuotationNotDuplicableError()
        if vista.open_duplicate_id is not None:
            abierta = await self._session.get(V2Quotation, vista.open_duplicate_id)
            assert abierta is not None
            return DuplicateResult(quotation=abierta, created=False)

        avisos: list[dict[str, Any]] = []
        cliente_id = original.customer_id
        if cliente_id is not None and not await self._customer_usable(cliente_id):
            avisos.append(
                {"code": DUP_CUSTOMER_UNAVAILABLE, "name": original.customer_name_snapshot}
            )
            cliente_id = None

        # Sin factor y sin tipo de cambio: `create_draft` pone los de HOY. Eso
        # es la regla de la fase; pasarlos seria copiar la economia vieja.
        try:
            async with self._session.begin_nested():
                nueva = await self._quotations.create_draft(
                    {
                        "customer_id": cliente_id,
                        "name": original.name,
                        "notes": original.notes,
                        "currency_code": original.currency_code_snapshot,
                        "customer_kind": original.customer_kind,
                        "production_type": original.production_type,
                    },
                    user=user,
                )
                nueva.client_notes = original.client_notes
                nueva.duplicated_from_id = original.id
                await self._session.flush()
        except IntegrityError:
            ganadora = await self._session.scalar(
                select(V2Quotation).where(
                    V2Quotation.duplicated_from_id == original.id,
                    V2Quotation.status == V2QuotationStatus.DRAFT,
                )
            )
            if ganadora is None:
                raise
            return DuplicateResult(quotation=ganadora, created=False)

        await self._copy_firing(original, nueva, avisos, user)
        mapa = await self._copy_lines(original, nueva, avisos, user)
        # Correccion 010H. Las lineas nuevas ya nacieron con los procesos que
        # pide el catalogo de HOY; esto trae encima lo que se decidio en la
        # cotizacion vieja: lo quitado, lo anadido a mano y las piezas escritas.
        # Va ANTES de la mano de obra para que cada tarea copiada nazca atada a
        # su proceso.
        procesos = await self._copy_processes(original, nueva, mapa, avisos)
        await self._copy_labor(original, nueva, mapa, procesos, avisos, user)
        await self._copy_extras(original, nueva, mapa, avisos)
        await self._copy_planning(original, nueva, avisos, user)

        await refresh_firing(self._session, nueva)
        await refresh_pricing(self._session, nueva)
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_QUOTATION_ENTITY,
            entity_id=str(original.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"event": EVENT_DUPLICATED, "new_id": str(nueva.id), "new_code": nueva.code},
        )
        self._audit.record_action(
            entity_type=V2_QUOTATION_ENTITY,
            entity_id=str(nueva.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "event": EVENT_DUPLICATED_FROM,
                "source_id": str(original.id),
                "source_code": original.code,
                "warnings": ",".join(sorted({a["code"] for a in avisos})),
            },
        )
        return DuplicateResult(quotation=nueva, created=True, warnings=avisos)

    async def _copy_firing(
        self,
        original: V2Quotation,
        nueva: V2Quotation,
        avisos: list[dict[str, Any]],
        user: AuthenticatedUser,
    ) -> None:
        """Horno y quemas. Nunca las tarifas pactadas: son economia de entonces."""
        datos: dict[str, Any] = {
            "low_fire_enabled": original.low_fire_enabled,
            "high_fire_enabled": original.high_fire_enabled,
        }
        if original.kiln_id is not None and original.kiln_id != nueva.kiln_id:
            datos["kiln_id"] = original.kiln_id
        try:
            async with self._session.begin_nested():
                await self._firing.set_firing(nueva.id, datos, user=user)
        except APIError:
            avisos.append({"code": DUP_KILN_UNAVAILABLE, "name": original.kiln_name_snapshot})
            datos.pop("kiln_id", None)
            # Sin horno pedido no hay nada que el maestro pueda rechazar; aun asi
            # un fallo aqui no tumba la duplicacion: las quemas quedan como las
            # dejo la configuracion de hoy y el borrador se revisa.
            try:
                async with self._session.begin_nested():
                    await self._firing.set_firing(nueva.id, datos, user=user)
            except APIError:
                pass

    async def _copy_lines(
        self,
        original: V2Quotation,
        nueva: V2Quotation,
        avisos: list[dict[str, Any]],
        user: AuthenticatedUser,
    ) -> dict[int, int]:
        """Cada pieza como ALTA NUEVA, y el mapa de ids viejos a nuevos.

        Alta y no copia de fila: `add_line` trata el material como elegido hoy,
        de modo que un material que ya no sirve se rechaza en vez de conservar
        su costo congelado —que es lo que haria una edicion—. Tampoco se pasa
        ningun override de costo.
        """
        mapa: dict[int, int] = {}
        for linea in await self._lines(original.id):
            if linea.product_id is not None:
                producto = await self._session.get(Product, linea.product_id)
                if producto is None or not producto.active:
                    avisos.append(
                        {"code": DUP_PRODUCT_UNAVAILABLE, "name": linea.product_name_snapshot}
                    )
                    continue
            base: dict[str, Any] = {
                "product_id": linea.product_id,
                "quantity": linea.quantity,
                "length_cm": linea.length_cm,
                "width_cm": linea.width_cm,
                "height_cm": linea.height_cm,
                "body_unit_weight": linea.body_unit_weight,
                "client_observation": linea.client_observation,
            }
            if linea.product_id is None:
                base["product_name"] = linea.product_name_snapshot
            pasta = {"body_material_id": linea.body_material_id}
            esmalte: dict[str, Any] = {"requires_glaze": linea.requires_glaze}
            if linea.requires_glaze and not linea.glaze_is_reference:
                esmalte["glaze_material_id"] = linea.glaze_material_id

            nueva_linea = await self._try_add_line(nueva.id, {**base, **pasta, **esmalte}, user)
            if nueva_linea is None and linea.body_material_id is not None:
                # Primero se sospecha del esmalte elegido a mano: la pasta es la
                # que mas pesa en el precio y conviene conservarla si sirve.
                if "glaze_material_id" in esmalte:
                    nueva_linea = await self._try_add_line(
                        nueva.id, {**base, **pasta, "requires_glaze": True}, user
                    )
                    if nueva_linea is not None:
                        avisos.append(
                            {
                                "code": DUP_GLAZE_MATERIAL_UNAVAILABLE,
                                "name": linea.glaze_material_name_snapshot,
                            }
                        )
                if nueva_linea is None:
                    nueva_linea = await self._try_add_line(nueva.id, {**base, **esmalte}, user)
                    if nueva_linea is not None:
                        avisos.append(
                            {
                                "code": DUP_BODY_MATERIAL_UNAVAILABLE,
                                "name": linea.body_material_name_snapshot,
                            }
                        )
            if nueva_linea is None:
                nueva_linea = await self._try_add_line(
                    nueva.id, {**base, "requires_glaze": False}, user
                )
                if nueva_linea is not None:
                    avisos.append(
                        {
                            "code": DUP_BODY_MATERIAL_UNAVAILABLE,
                            "name": linea.body_material_name_snapshot,
                        }
                    )
            if nueva_linea is None:
                avisos.append(
                    {"code": DUP_PRODUCT_UNAVAILABLE, "name": linea.product_name_snapshot}
                )
                continue
            mapa[linea.id] = nueva_linea.id
        return mapa

    async def _try_add_line(
        self, quotation_id: int, data: dict[str, Any], user: AuthenticatedUser
    ) -> V2QuotationProduct | None:
        try:
            async with self._session.begin_nested():
                linea, _ = await self._materials.add_line(quotation_id, data, user=user)
                return linea
        except APIError:
            return None

    async def _copy_labor(
        self,
        original: V2Quotation,
        nueva: V2Quotation,
        mapa: dict[int, int],
        procesos: dict[tuple[int, int], V2QuotationProcess],
        avisos: list[dict[str, Any]],
        user: AuthenticatedUser,
    ) -> None:
        """Quien hace que, y cuanto. La tarifa por hora es la de hoy.

        Las horas pactadas SI se traen: son una estimacion de tiempo del
        encargo, no un precio. La tarifa pactada no: es economia de entonces.
        """
        tareas = (
            await self._session.scalars(
                select(V2QuotationLabor)
                .where(V2QuotationLabor.v2_quotation_id == original.id)
                .order_by(V2QuotationLabor.sort_order, V2QuotationLabor.id)
            )
        ).all()
        for tarea in tareas:
            linea_nueva: int | None = None
            if tarea.v2_quotation_product_id is not None:
                linea_nueva = mapa.get(tarea.v2_quotation_product_id)
                if linea_nueva is None:
                    avisos.append(
                        {"code": DUP_LABOR_UNAVAILABLE, "name": tarea.technique_name_snapshot}
                    )
                    continue
            proceso = (
                None if linea_nueva is None else procesos.get((linea_nueva, tarea.technique_id))
            )
            if proceso is not None and proceso.removed_at is not None:
                # La cotizacion vieja habia quitado ese proceso: su tarea no
                # vuelve, o el duplicado cobraria un trabajo que se descarto.
                continue
            datos: dict[str, Any] = {
                "worker_id": tarea.worker_id,
                "technique_id": tarea.technique_id,
                "quantity": tarea.quantity,
                "v2_quotation_product_id": linea_nueva,
                "is_additional_personnel": tarea.is_additional_personnel,
                "v2_quotation_process_id": None if proceso is None else proceso.id,
            }
            if tarea.hours_overridden:
                datos["final_hours_override"] = tarea.final_hours
            try:
                async with self._session.begin_nested():
                    await self._labor.add_labor(nueva.id, datos, user=user)
            except APIError:
                avisos.append(
                    {
                        "code": DUP_LABOR_UNAVAILABLE,
                        "name": f"{tarea.worker_name_snapshot} · {tarea.technique_name_snapshot}",
                    }
                )

    async def _copy_processes(
        self,
        original: V2Quotation,
        nueva: V2Quotation,
        mapa: dict[int, int],
        avisos: list[dict[str, Any]],
    ) -> dict[tuple[int, int], V2QuotationProcess]:
        """Las decisiones de procesos de la cotizacion vieja, sobre la nueva.

        Lo que el catalogo pide hoy ya esta puesto. Aqui se recuperan las tres
        decisiones que son de la cotizacion y no del maestro: lo que se quito,
        lo que se anadio a mano y las piezas que alguien escribio. Sin esto, un
        duplicado devolveria el acabado que el cliente no queria.
        """
        viejos = (
            await self._session.scalars(
                select(V2QuotationProcess)
                .where(V2QuotationProcess.v2_quotation_id == original.id)
                .order_by(V2QuotationProcess.sort_order, V2QuotationProcess.id)
            )
        ).all()
        nuevos = {
            (proceso.v2_quotation_product_id, proceso.technique_id): proceso
            for proceso in (
                await self._session.scalars(
                    select(V2QuotationProcess).where(
                        V2QuotationProcess.v2_quotation_id == nueva.id
                    )
                )
            ).all()
        }
        if not viejos:
            return nuevos

        activas = set(
            (
                await self._session.scalars(
                    select(V2Technique.id).where(
                        V2Technique.id.in_([proceso.technique_id for proceso in viejos]),
                        V2Technique.active.is_(True),
                    )
                )
            ).all()
        )

        for viejo in viejos:
            linea_nueva = mapa.get(viejo.v2_quotation_product_id)
            if linea_nueva is None:
                continue
            proceso = nuevos.get((linea_nueva, viejo.technique_id))
            if proceso is None:
                if viejo.removed_at is not None:
                    # Estaba quitado y el catalogo ya no lo pide: nada que hacer.
                    continue
                if viejo.technique_id not in activas:
                    avisos.append({"code": DUP_LABOR_UNAVAILABLE, "name": viejo.technique.name})
                    continue
                proceso = V2QuotationProcess(
                    v2_quotation_id=nueva.id,
                    v2_quotation_product_id=linea_nueva,
                    technique_id=viejo.technique_id,
                    sort_order=viejo.sort_order,
                    origin=viejo.origin,
                    quantity=viejo.quantity,
                    quantity_overridden=viejo.quantity_overridden,
                )
                self._session.add(proceso)
                nuevos[(linea_nueva, viejo.technique_id)] = proceso
            elif viejo.removed_at is not None:
                proceso.removed_at = datetime.now(UTC)
            else:
                proceso.quantity = viejo.quantity
                proceso.quantity_overridden = viejo.quantity_overridden
        await self._session.flush()
        return nuevos

    async def _copy_extras(
        self,
        original: V2Quotation,
        nueva: V2Quotation,
        mapa: dict[int, int],
        avisos: list[dict[str, Any]],
    ) -> None:
        """Los adicionales, al precio de HOY.

        Misma regla que los materiales: el concepto se vuelve a valorar con el
        maestro actual y el precio pactado entonces no viaja. Un concepto
        retirado no se recotiza a ciegas: se avisa y no entra.
        """
        viejos = (
            await self._session.scalars(
                select(V2QuotationExtra)
                .where(V2QuotationExtra.v2_quotation_id == original.id)
                .order_by(V2QuotationExtra.sort_order, V2QuotationExtra.id)
            )
        ).all()
        for viejo in viejos:
            concepto = await self._session.get(V2Extra, viejo.v2_extra_id)
            if concepto is None or not concepto.active:
                avisos.append({"code": DUP_EXTRA_UNAVAILABLE, "name": viejo.name_snapshot})
                continue
            linea_nueva = (
                mapa.get(viejo.v2_quotation_product_id)
                if viejo.v2_quotation_product_id is not None
                else None
            )
            if viejo.v2_quotation_product_id is not None and linea_nueva is None:
                avisos.append({"code": DUP_EXTRA_UNAVAILABLE, "name": viejo.name_snapshot})
                continue
            self._session.add(
                V2QuotationExtra(
                    v2_quotation_id=nueva.id,
                    v2_quotation_product_id=linea_nueva,
                    v2_extra_id=concepto.id,
                    name_snapshot=concepto.name,
                    unit_snapshot=concepto.unit,
                    unit_cost_snapshot=concepto.unit_cost,
                    unit_cost_is_override=False,
                    description=viejo.description,
                    quantity=viejo.quantity,
                    total_cost=viejo.quantity * concepto.unit_cost,
                    sort_order=viejo.sort_order,
                )
            )
        await self._session.flush()

    async def _copy_planning(
        self,
        original: V2Quotation,
        nueva: V2Quotation,
        avisos: list[dict[str, Any]],
        user: AuthenticatedUser,
    ) -> None:
        """Dias de taller e ilustracion. La ilustracion, con el jornal de hoy."""
        if original.effective_work_days is not None:
            # Con SAVEPOINT como el resto: si un recalculo futuro llegara a
            # rechazar los dias, la duplicacion sigue y el borrador pide decidirlos.
            try:
                async with self._session.begin_nested():
                    await self._labor.set_planning(
                        nueva.id, original.effective_work_days, user=user
                    )
            except APIError:
                avisos.append({"code": DUP_PLANNING_UNAVAILABLE, "name": None})
        if original.illustration_enabled:
            try:
                async with self._session.begin_nested():
                    await self._labor.set_illustration(
                        nueva.id,
                        {
                            "illustration_enabled": True,
                            "illustration_quantity": original.illustration_quantity,
                            "illustration_notes": original.illustration_notes,
                        },
                        user=user,
                    )
            except APIError:
                avisos.append({"code": DUP_ILLUSTRATION_UNAVAILABLE, "name": None})

    # ------------------------------------------------------------------
    # Apoyo
    # ------------------------------------------------------------------
    async def _locked(self, quotation_id: int) -> V2Quotation:
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
        if quotation is None:
            raise V2LifecycleNotFoundError()
        return quotation

    async def _lines(self, quotation_id: int) -> list[V2QuotationProduct]:
        return list(
            (
                await self._session.scalars(
                    select(V2QuotationProduct)
                    .where(V2QuotationProduct.v2_quotation_id == quotation_id)
                    .order_by(V2QuotationProduct.sort_order, V2QuotationProduct.id)
                )
            ).all()
        )

    async def _handoff_of(self, quotation_id: int) -> V2ProductionHandoff | None:
        return await self._session.scalar(
            select(V2ProductionHandoff).where(V2ProductionHandoff.v2_quotation_id == quotation_id)
        )

    async def _issuance(self, quotation: V2Quotation) -> dict[str, str | None]:
        """Lo que el PDF dira del cliente y de las condiciones.

        En un borrador sale del maestro y de la configuracion de HOY: es lo que
        se congelaria si se emitiera ahora. En una emitida sale de lo que se
        congelo. Resumen, huella y emision leen de aqui, y por eso no pueden
        discrepar.
        """
        if quotation.status is not V2QuotationStatus.DRAFT:
            return {
                "customer_name": quotation.customer_name_snapshot,
                "customer_document_type": quotation.customer_document_type_snapshot,
                "customer_document_number": quotation.customer_document_number_snapshot,
                "customer_address": quotation.customer_address_snapshot,
                "customer_email": quotation.customer_email_snapshot,
                "customer_phone": quotation.customer_phone_snapshot,
                "conditions": quotation.conditions_snapshot,
                "payment_notes": quotation.payment_notes_snapshot,
            }
        cliente = (
            await self._session.get(Partner, quotation.customer_id)
            if quotation.customer_id is not None
            else None
        )
        politica = await self._settings.commercial_policy()
        return {
            "customer_name": cliente.name if cliente else quotation.customer_name_snapshot,
            "customer_document_type": (
                cliente.document_type.value if cliente and cliente.document_type else None
            ),
            "customer_document_number": cliente.document_number if cliente else None,
            "customer_address": cliente.address if cliente else None,
            "customer_email": cliente.email if cliente else None,
            "customer_phone": (cliente.phone or cliente.mobile) if cliente else None,
            "conditions": politica.general_conditions,
            "payment_notes": politica.payment_notes,
        }

    async def _customer_usable(self, customer_id: int) -> bool:
        cliente = await self._session.get(Partner, customer_id)
        return cliente is not None and cliente.active and cliente.role in CUSTOMER_ROLES

    async def _validity_days(self, quotation: V2Quotation) -> int | None:
        """La vigencia que se congelara.

        La del borrador si la tiene —se copio al crearlo, igual que el IGV—. Un
        borrador de 010A nacio sin ella, y entonces manda la configuracion de
        hoy: es la unica vigencia que alguien aprobo.
        """
        if quotation.validity_days_snapshot is not None:
            return quotation.validity_days_snapshot
        if quotation.status is not V2QuotationStatus.DRAFT:
            return None
        return (await self._settings.get()).quotation_validity_days

    async def _blockers(
        self, quotation: V2Quotation, lines: list[V2QuotationProduct], dias: int | None
    ) -> list[Blocker]:
        """Todo lo que impide emitir. Ninguna de estas es una preferencia.

        Se miran los datos CONGELADOS del borrador, no el estado de hoy de cada
        maestro. Un material desactivado despues de elegirlo no bloquea: la
        linea conserva el costo con el que se valorizo, y es lo que 010C
        decidio. Lo que si bloquea es el cliente archivado, porque el papel se
        emite a su nombre hoy.
        """
        bloqueos: list[Blocker] = []

        if quotation.customer_id is None:
            bloqueos.append(Blocker(BLOCK_CUSTOMER_REQUIRED))
        elif not await self._customer_usable(quotation.customer_id):
            bloqueos.append(Blocker(BLOCK_CUSTOMER_INACTIVE))

        if not lines:
            bloqueos.append(Blocker(BLOCK_NO_LINES))
        for linea in lines:
            if not linea.product_id and not (linea.product_name_snapshot or "").strip():
                bloqueos.append(Blocker(BLOCK_LINE_PRODUCT, linea.id))
            if linea.quantity <= 0:
                bloqueos.append(Blocker(BLOCK_LINE_QUANTITY, linea.id))
            if linea.body_material_id is None:
                bloqueos.append(Blocker(BLOCK_LINE_BODY_MATERIAL, linea.id))
            if linea.body_unit_weight is None or linea.body_unit_weight <= ZERO:
                bloqueos.append(Blocker(BLOCK_LINE_BODY_WEIGHT, linea.id))
            if linea.quantity > 0 and linea.unit_price <= ZERO:
                bloqueos.append(Blocker(BLOCK_LINE_PRICE, linea.id))

        factor = quotation.commercial_factor
        minimo = quotation.commercial_factor_min_snapshot
        maximo = quotation.commercial_factor_max_snapshot
        if factor is None:
            bloqueos.append(Blocker(BLOCK_FACTOR_REQUIRED))
        elif (minimo is not None and factor < minimo) or (maximo is not None and factor > maximo):
            bloqueos.append(Blocker(BLOCK_FACTOR_OUT_OF_RANGE))

        if quotation.tax_percent_snapshot is None:
            bloqueos.append(Blocker(BLOCK_TAX_REQUIRED))
        if quotation.rounding_step_snapshot is None or quotation.rounding_step_snapshot <= ZERO:
            bloqueos.append(Blocker(BLOCK_ROUNDING_REQUIRED))
        if not quotation.currency_code_snapshot:
            bloqueos.append(Blocker(BLOCK_CURRENCY_REQUIRED))
        elif (
            quotation.currency_code_snapshot.upper() != "PEN"
            and quotation.exchange_rate_snapshot is None
        ):
            bloqueos.append(Blocker(BLOCK_EXCHANGE_RATE_REQUIRED))
        if dias is None or dias <= 0 or dias > MAX_VALIDITY_DAYS:
            bloqueos.append(Blocker(BLOCK_VALIDITY_INVALID))
        if quotation.effective_work_days is None:
            bloqueos.append(Blocker(BLOCK_WORK_DAYS_REQUIRED))
        quemas = bool(quotation.low_fire_enabled) or bool(quotation.high_fire_enabled)
        if quemas and quotation.kiln_id is None:
            bloqueos.append(Blocker(BLOCK_KILN_REQUIRED))
        if quotation.total_amount <= ZERO:
            bloqueos.append(Blocker(BLOCK_TOTAL_REQUIRED))
        return bloqueos


__all__ = [
    "Blocker",
    "ConfirmationPreview",
    "DuplicateResult",
    "LifecycleView",
    "V2LifecycleService",
    "V2QuotationAlreadyIssuedError",
    "V2QuotationChangedError",
    "V2QuotationIncompleteError",
    "V2QuotationNotCancellableError",
    "V2QuotationNotConfirmableError",
    "V2QuotationNotDuplicableError",
    "V2QuotationNotSendableError",
    "document_payload",
]
