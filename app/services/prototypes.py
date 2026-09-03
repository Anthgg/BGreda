"""Prototipos: registrar una muestra, fabricarla y decidir si vale.

Dominio propio y no una rama dentro de produccion. La diferencia esta en de
donde salen los materiales: una orden de produccion los DERIVA de la cotizacion
confirmada y nadie los toca; una muestra lleva los que alguien eligio para ella.
Meter las dos cosas en el mismo servicio obligaria a un `if prototipo` en cada
paso del calculo, y el dia que uno se olvidara la produccion final aceptaria
materiales a mano.

Lo que este modulo NO hace, tambien a proposito:

- **No cobra.** El eje de pago es el de la cotizacion (Fase 009H.1). No hay
  `payment_status` de prototipo ni endpoint para marcarlo pagado.
- **No fabrica al aprobar.** Aprobar satisface un guardia; no crea ni arranca
  ninguna orden.
- **No reescribe lo rechazado.** Si hace falta otra muestra se crea la
  siguiente y se dice de cual viene.

El unico punto que mueve inventario es `start`. Crear, editar, anadir material,
enlazar la cotizacion, cobrarla, completar, aprobar o rechazar no descuentan un
gramo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.inventory import MovementType, StockBalance, StockLocation
from app.models.masters import Product, ProductType
from app.models.production import ProductionOrder
from app.models.prototypes import (
    Prototype,
    PrototypeApproval,
    PrototypeMaterialLine,
    PrototypeStatus,
)
from app.models.quotations import Quotation, QuotationItem, QuotationPaymentStatus
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.inventory import InventoryService
from app.services.sequences import SequenceService

PROTOTYPE_ENTITY = "prototype"

#: Lo que se puede gastar fisicamente. Un producto terminado o un servicio no
#: son insumos: no hay nada que descontar de ellos para fabricar una muestra.
CONSUMABLE_TYPES = frozenset({ProductType.RAW_MATERIAL, ProductType.PREPARED_MATERIAL})


class PrototypeReadinessCode(StrEnum):
    """Por que una muestra todavia no puede fabricarse.

    Son codigos y los traduce el frontend, como los de 009I. Y son varios y no
    un `ready=false` a secas: «falta pagar» y «falta barro» mandan a personas
    distintas, y decir solo que no se puede deja a quien esta en el taller
    buscando el problema equivocado.
    """

    INVALID_STATE = "INVALID_STATE"
    NO_QUOTATION = "NO_QUOTATION"
    QUOTATION_UNPAID = "QUOTATION_UNPAID"
    NO_STOCK_LOCATION = "NO_STOCK_LOCATION"
    NO_MATERIALS = "NO_MATERIALS"
    STOCK_MISSING = "STOCK_MISSING"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"


# ---------------------------------------------------------------------------
# Errores de dominio
# ---------------------------------------------------------------------------
class PrototypeNotFoundError(APIError):
    status_code = 404
    code = "PROTOTYPE_NOT_FOUND"
    message = "El prototipo no existe"


class PrototypeNotEditableError(APIError):
    """Una muestra ya arrancada consumio material: sus lineas son historia."""

    status_code = 409
    code = "PROTOTYPE_NOT_EDITABLE"
    message = "Solo se puede editar un prototipo que todavia no ha arrancado"


class PrototypeNotStartableError(APIError):
    status_code = 409
    code = "PROTOTYPE_NOT_STARTABLE"
    message = "El prototipo no esta en un estado que permita arrancarlo"


class PrototypeNotReadyError(APIError):
    """Falta algo para fabricar. Lleva el detalle para poder corregirlo."""

    status_code = 409
    code = "PROTOTYPE_NOT_READY"
    message = "Faltan condiciones para fabricar el prototipo"

    def __init__(self, issues: list[PrototypeIssue]) -> None:
        super().__init__(details=[issue.as_detail() for issue in issues])
        self.issues = issues


class PrototypeNotCompletableError(APIError):
    status_code = 409
    code = "PROTOTYPE_NOT_COMPLETABLE"
    message = "Solo un prototipo arrancado puede completarse"


class PrototypeNotDecidableError(APIError):
    """No se aprueba ni se rechaza una muestra que todavia no existe."""

    status_code = 409
    code = "PROTOTYPE_NOT_DECIDABLE"
    message = "Solo un prototipo completado y sin decidir puede aprobarse o rechazarse"


class PrototypeNotCancellableError(APIError):
    status_code = 409
    code = "PROTOTYPE_NOT_CANCELLABLE"
    message = "Una muestra ya arrancada no puede anularse: el material ya se gasto"


class PrototypeMaterialNotConsumableError(APIError):
    status_code = 422
    code = "PROTOTYPE_MATERIAL_NOT_CONSUMABLE"
    message = "Solo materia prima o material preparado puede consumirse en un prototipo"


class PrototypeMaterialDuplicatedError(APIError):
    status_code = 422
    code = "PROTOTYPE_MATERIAL_DUPLICATED"
    message = "Un material no puede repetirse en el mismo prototipo"


class PrototypeMaterialWithoutUomError(APIError):
    status_code = 422
    code = "PROTOTYPE_MATERIAL_WITHOUT_UOM"
    message = "El material no tiene unidad base y no puede descontarse"


class PrototypeProductNotInQuotationError(APIError):
    """Enlazar una muestra a un producto que ese pedido no lleva."""

    status_code = 422
    code = "PROTOTYPE_PRODUCT_NOT_IN_QUOTATION"
    message = "El producto del prototipo no pertenece a la cotizacion enlazada"


class PrototypeLineageInvalidError(APIError):
    status_code = 422
    code = "PROTOTYPE_LINEAGE_INVALID"
    message = "La cadena de iteraciones del prototipo no es valida"


class PrototypeAlreadySupersededError(APIError):
    status_code = 409
    code = "PROTOTYPE_ALREADY_SUPERSEDED"
    message = "Ese prototipo ya tiene una iteracion posterior"


@dataclass(frozen=True, slots=True)
class PrototypeIssue:
    """Un bloqueo concreto, en codigo. El texto lo pone el frontend."""

    code: PrototypeReadinessCode
    product_id: int | None = None
    product_name: str | None = None
    required_quantity: Decimal | None = None
    available_quantity: Decimal | None = None
    uom: str | None = None

    def as_detail(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "required_quantity": (
                None if self.required_quantity is None else str(self.required_quantity)
            ),
            "available_quantity": (
                None if self.available_quantity is None else str(self.available_quantity)
            ),
            "uom": self.uom,
        }


@dataclass(frozen=True, slots=True)
class PrototypeReadiness:
    ready: bool
    issues: list[PrototypeIssue]


@dataclass(frozen=True, slots=True)
class MaterialInput:
    """Un material elegido para la muestra, tal y como llega del cliente."""

    product_id: int
    quantity: Decimal


class PrototypeService:
    """Reglas de las muestras fisicas."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        sequences: SequenceService,
        inventory: InventoryService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._sequences = sequences
        self._inventory = inventory

    # -- Lectura -----------------------------------------------------------
    def _base_query(self) -> Select[tuple[Prototype]]:
        return select(Prototype).options(selectinload(Prototype.lines))

    async def get(self, prototype_id: int, *, for_update: bool = False) -> Prototype:
        statement = self._base_query().where(Prototype.id == prototype_id)
        if for_update:
            # `of` evita bloquear las lineas por el join del selectinload: lo
            # que hay que retener es la muestra, no su lista de materiales.
            statement = statement.with_for_update(of=Prototype)
        row = await self._session.scalar(statement)
        if row is None:
            raise PrototypeNotFoundError()
        return row

    async def list_prototypes(
        self,
        *,
        status: PrototypeStatus | None = None,
        approval: PrototypeApproval | None = None,
        quotation_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Prototype], int]:
        statement = self._base_query()
        counter = select(func.count()).select_from(Prototype)
        if status is not None:
            statement = statement.where(Prototype.status == status)
            counter = counter.where(Prototype.status == status)
        if approval is not None:
            statement = statement.where(Prototype.approval == approval)
            counter = counter.where(Prototype.approval == approval)
        if quotation_id is not None:
            statement = statement.where(Prototype.quotation_id == quotation_id)
            counter = counter.where(Prototype.quotation_id == quotation_id)

        total = int(await self._session.scalar(counter) or 0)
        rows = await self._session.scalars(
            statement.order_by(Prototype.id.desc()).limit(limit).offset(offset)
        )
        return list(rows.all()), total

    # -- Alta y edicion ----------------------------------------------------
    async def _validate_links(
        self, *, quotation_id: int | None, product_id: int | None, stock_location_id: int | None
    ) -> None:
        """Comprueba que lo enlazado existe y encaja entre si.

        Si vienen cotizacion Y producto, el producto tiene que estar de verdad
        en ese pedido. Sin esta comprobacion, un identificador enviado a mano
        crearia una muestra «de la jarra» colgando de una cotizacion de platos,
        y el guardia de produccion final bloquearia un pedido por una muestra
        que no le corresponde.
        """
        if quotation_id is not None:
            quotation = await self._session.get(Quotation, quotation_id)
            if quotation is None:
                raise PrototypeLineageInvalidError("La cotizacion enlazada no existe")
        if product_id is not None:
            product = await self._session.get(Product, product_id)
            if product is None:
                raise PrototypeLineageInvalidError("El producto enlazado no existe")
        if stock_location_id is not None:
            location = await self._session.get(StockLocation, stock_location_id)
            if location is None or not location.active:
                raise PrototypeLineageInvalidError("El almacen enlazado no existe o esta inactivo")

        if quotation_id is not None and product_id is not None:
            pertenece = await self._session.scalar(
                select(func.count())
                .select_from(QuotationItem)
                .where(
                    QuotationItem.quotation_id == quotation_id,
                    QuotationItem.product_id == product_id,
                )
            )
            if not pertenece:
                raise PrototypeProductNotInQuotationError()

    async def create(
        self,
        *,
        name: str,
        quantity: int,
        quotation_id: int | None,
        product_id: int | None,
        stock_location_id: int | None,
        target_days: int | None,
        notes: str | None,
        materials: list[MaterialInput],
        user: AuthenticatedUser,
        supersedes_prototype_id: int | None = None,
    ) -> Prototype:
        """Registra la muestra. No mueve ni un gramo.

        El correlativo lo emite el backend: nadie teclea el suyo, igual que en
        cotizaciones, quemas, preparaciones y ordenes.
        """
        await self._validate_links(
            quotation_id=quotation_id,
            product_id=product_id,
            stock_location_id=stock_location_id,
        )

        prototype = Prototype(
            code=await self._sequences.issue(SequenceType.PROTOTYPE, user_id=user.id),
            name=name.strip(),
            quotation_id=quotation_id,
            product_id=product_id,
            stock_location_id=stock_location_id,
            quantity=quantity,
            status=PrototypeStatus.CREATED,
            approval=PrototypeApproval.PENDING,
            requested_at=datetime.now(UTC),
            target_days=target_days,
            notes=notes,
            supersedes_prototype_id=supersedes_prototype_id,
            created_by=user.id,
            created_by_name=user.display_name,
        )
        self._session.add(prototype)
        await self._session.flush()

        if materials:
            await self._replace_materials(prototype, materials)

        self._audit.record_action(
            entity_type=PROTOTYPE_ENTITY,
            entity_id=str(prototype.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": prototype.code,
                "name": prototype.name,
                "quotation_id": quotation_id,
                "product_id": product_id,
                "status": prototype.status.value,
                "material_count": len(materials),
                "supersedes_prototype_id": supersedes_prototype_id,
            },
        )
        return prototype

    async def update(
        self,
        prototype_id: int,
        *,
        name: str | None,
        quantity: int | None,
        quotation_id: int | None,
        product_id: int | None,
        stock_location_id: int | None,
        target_days: int | None,
        notes: str | None,
        user: AuthenticatedUser,
    ) -> Prototype:
        """Edita la muestra mientras todavia no ha gastado nada."""
        prototype = await self.get(prototype_id, for_update=True)
        if prototype.status is not PrototypeStatus.CREATED:
            raise PrototypeNotEditableError()

        await self._validate_links(
            quotation_id=quotation_id if quotation_id is not None else prototype.quotation_id,
            product_id=product_id if product_id is not None else prototype.product_id,
            stock_location_id=(
                stock_location_id if stock_location_id is not None else prototype.stock_location_id
            ),
        )

        cambios: dict[str, tuple[object, object]] = {}
        for campo, valor in (
            ("name", None if name is None else name.strip()),
            ("quantity", quantity),
            ("quotation_id", quotation_id),
            ("product_id", product_id),
            ("stock_location_id", stock_location_id),
            ("target_days", target_days),
            ("notes", notes),
        ):
            if valor is None:
                continue
            anterior = getattr(prototype, campo)
            if anterior != valor:
                cambios[campo] = (anterior, valor)
                setattr(prototype, campo, valor)

        if cambios:
            prototype.updated_at = datetime.now(UTC)
            await self._session.flush()
            self._audit.record_changes(
                entity_type=PROTOTYPE_ENTITY,
                entity_id=str(prototype.id),
                changes={
                    k: (str(a) if a is not None else None, str(b)) for k, (a, b) in cambios.items()
                },
                user_id=user.id,
                user_display_name=user.display_name,
            )
        return prototype

    # -- Materiales --------------------------------------------------------
    async def _replace_materials(
        self, prototype: Prototype, materials: list[MaterialInput]
    ) -> None:
        vistos: set[int] = set()
        prototype.lines.clear()
        await self._session.flush()

        for orden, entrada in enumerate(materials, start=1):
            if entrada.product_id in vistos:
                raise PrototypeMaterialDuplicatedError()
            vistos.add(entrada.product_id)

            product = await self._session.get(Product, entrada.product_id)
            if product is None:
                raise PrototypeLineageInvalidError("El material no existe")
            if product.product_type not in CONSUMABLE_TYPES:
                raise PrototypeMaterialNotConsumableError()
            if not product.base_uom_code:
                raise PrototypeMaterialWithoutUomError()

            prototype.lines.append(
                PrototypeMaterialLine(
                    product_id=product.id,
                    sort_order=orden,
                    quantity=entrada.quantity,
                    uom_code=product.base_uom_code,
                    product_name_snapshot=product.name,
                    product_internal_reference_snapshot=product.internal_reference,
                )
            )
        await self._session.flush()

    async def set_materials(
        self, prototype_id: int, materials: list[MaterialInput], *, user: AuthenticatedUser
    ) -> Prototype:
        """Fija los materiales que ESTA muestra va a gastar.

        Se reemplaza la lista entera en vez de parchear linea a linea: lo que
        importa es que el conjunto sea el que alguien decidio, y un parcheo
        incremental deja la puerta abierta a que quede un material olvidado de
        una version anterior.

        No mueve inventario: el consumo ocurre al arrancar, y solo entonces.
        """
        prototype = await self.get(prototype_id, for_update=True)
        if prototype.status is not PrototypeStatus.CREATED:
            raise PrototypeNotEditableError()

        antes = [
            f"{linea.product_internal_reference_snapshot}:{linea.quantity}"
            for linea in prototype.lines
        ]
        await self._replace_materials(prototype, materials)
        despues = [
            f"{linea.product_internal_reference_snapshot}:{linea.quantity}"
            for linea in prototype.lines
        ]

        if antes != despues:
            prototype.updated_at = datetime.now(UTC)
            await self._session.flush()
            self._audit.record_changes(
                entity_type=PROTOTYPE_ENTITY,
                entity_id=str(prototype.id),
                changes={"materials": (", ".join(antes) or None, ", ".join(despues))},
                user_id=user.id,
                user_display_name=user.display_name,
            )
        return prototype

    # -- Disponibilidad ----------------------------------------------------
    async def evaluate_readiness(
        self, prototype: Prototype, *, lock: bool = False
    ) -> PrototypeReadiness:
        """Que falta para poder fabricar. No escribe nada.

        Se puede pedir tantas veces como haga falta: la pantalla la consulta
        cada vez que abre la muestra, y si consultarla tuviera consecuencias
        mirar el almacen lo cambiaria.
        """
        issues: list[PrototypeIssue] = []

        if prototype.status is not PrototypeStatus.CREATED:
            issues.append(PrototypeIssue(PrototypeReadinessCode.INVALID_STATE))

        if prototype.quotation_id is None:
            # Sin cotizacion no hay nada que cobrar, y sin cobro no se gasta
            # material. No es que falte un dato: es que no hay pedido.
            issues.append(PrototypeIssue(PrototypeReadinessCode.NO_QUOTATION))
        else:
            quotation = await self._session.get(Quotation, prototype.quotation_id)
            if quotation is None or quotation.payment_status is not QuotationPaymentStatus.PAID:
                issues.append(PrototypeIssue(PrototypeReadinessCode.QUOTATION_UNPAID))

        if prototype.stock_location_id is None:
            issues.append(PrototypeIssue(PrototypeReadinessCode.NO_STOCK_LOCATION))

        if not prototype.lines:
            issues.append(PrototypeIssue(PrototypeReadinessCode.NO_MATERIALS))
        elif prototype.stock_location_id is not None:
            issues.extend(await self._stock_issues(prototype, lock=lock))

        return PrototypeReadiness(ready=not issues, issues=issues)

    async def _stock_issues(self, prototype: Prototype, *, lock: bool) -> list[PrototypeIssue]:
        """Comprueba TODOS los saldos antes de tocar ninguno.

        Los cerrojos se toman en orden de producto para que dos arranques
        simultaneos no se queden esperandose en cruz.
        """
        issues: list[PrototypeIssue] = []
        for linea in sorted(prototype.lines, key=lambda item: item.product_id):
            statement = select(StockBalance).where(
                StockBalance.product_id == linea.product_id,
                StockBalance.location_id == prototype.stock_location_id,
            )
            if lock:
                statement = statement.with_for_update()
            balance = await self._session.scalar(statement)

            if balance is None:
                issues.append(
                    PrototypeIssue(
                        PrototypeReadinessCode.STOCK_MISSING,
                        product_id=linea.product_id,
                        product_name=linea.product_name_snapshot,
                        required_quantity=linea.quantity,
                        uom=linea.uom_code,
                    )
                )
            elif balance.quantity < linea.quantity:
                issues.append(
                    PrototypeIssue(
                        PrototypeReadinessCode.INSUFFICIENT_STOCK,
                        product_id=linea.product_id,
                        product_name=linea.product_name_snapshot,
                        required_quantity=linea.quantity,
                        available_quantity=balance.quantity,
                        uom=linea.uom_code,
                    )
                )
        return issues

    # -- Fabricacion -------------------------------------------------------
    async def start(self, prototype_id: int, *, user: AuthenticatedUser) -> tuple[Prototype, bool]:
        """Consume el material elegido y deja la muestra en STARTED.

        Todo ocurre en una sola transaccion y en este orden:

        1. bloquear la muestra;
        2. si ya estaba arrancada, salir sin tocar nada;
        3. comprobar pago, almacen, materiales y saldos —bloqueando los saldos
           en orden de producto—;
        4. comprobar que alcanzan TODOS antes de descontar ninguno;
        5. descontar y dejar el movimiento que lo prueba;
        6. marcar STARTED.

        El punto 4 es lo que hace la operacion atomica de verdad: si un solo
        material no alcanza, no se ha descontado nada todavia y la excepcion
        deshace la transaccion entera. Nunca queda media muestra consumida.

        El punto 2 es la idempotencia por el hecho fisico, no por la peticion:
        pulsar «Arrancar» dos veces no gasta el barro dos veces.
        """
        prototype = await self.get(prototype_id, for_update=True)
        if prototype.status is PrototypeStatus.STARTED:
            return prototype, False
        if prototype.status is not PrototypeStatus.CREATED:
            raise PrototypeNotStartableError()

        readiness = await self.evaluate_readiness(prototype, lock=True)
        if not readiness.ready:
            # Nada se ha descontado: los cerrojos se sueltan al deshacer la
            # transaccion y el inventario queda exactamente como estaba.
            raise PrototypeNotReadyError(readiness.issues)

        location = await self._session.get(StockLocation, prototype.stock_location_id)
        assert location is not None
        consumido: list[str] = []
        for linea in sorted(prototype.lines, key=lambda item: item.product_id):
            product = await self._session.get(Product, linea.product_id)
            assert product is not None
            await self._inventory.apply_movement(
                product=product,
                location=location,
                quantity=-linea.quantity,
                movement_type=MovementType.PROTOTYPE_OUT,
                reason=f"Prototipo {prototype.code}",
                user_id=user.id,
                user_name=user.display_name,
                prototype_id=prototype.id,
            )
            consumido.append(f"{linea.product_internal_reference_snapshot}:{linea.quantity}")

        momento = datetime.now(UTC)
        prototype.status = PrototypeStatus.STARTED
        prototype.started_at = momento
        prototype.updated_at = momento
        await self._session.flush()

        self._audit.record_changes(
            entity_type=PROTOTYPE_ENTITY,
            entity_id=str(prototype.id),
            changes={
                "status": (PrototypeStatus.CREATED.value, prototype.status.value),
                "started_at": (None, momento.isoformat()),
                "consumed": (None, ", ".join(consumido)),
            },
            user_id=user.id,
            user_display_name=user.display_name,
        )
        return prototype, True

    async def complete(
        self, prototype_id: int, *, user: AuthenticatedUser
    ) -> tuple[Prototype, bool]:
        """Cierra la fabricacion. NO vuelve a consumir ni decide nada."""
        prototype = await self.get(prototype_id, for_update=True)
        if prototype.status is PrototypeStatus.COMPLETED:
            return prototype, False
        if prototype.status is not PrototypeStatus.STARTED:
            raise PrototypeNotCompletableError()

        momento = datetime.now(UTC)
        prototype.status = PrototypeStatus.COMPLETED
        prototype.completed_at = momento
        prototype.updated_at = momento
        await self._session.flush()

        self._audit.record_changes(
            entity_type=PROTOTYPE_ENTITY,
            entity_id=str(prototype.id),
            changes={
                "status": (PrototypeStatus.STARTED.value, prototype.status.value),
                "completed_at": (None, momento.isoformat()),
            },
            user_id=user.id,
            user_display_name=user.display_name,
        )
        return prototype, True

    async def cancel(self, prototype_id: int, *, user: AuthenticatedUser) -> tuple[Prototype, bool]:
        """Anula una muestra que todavia no gasto nada.

        Una arrancada no: anularla no devuelve el barro al saco, y fingir que
        si lo hace convierte el inventario en una opinion.
        """
        prototype = await self.get(prototype_id, for_update=True)
        if prototype.status is PrototypeStatus.CANCELLED:
            return prototype, False
        if prototype.status is not PrototypeStatus.CREATED:
            raise PrototypeNotCancellableError()

        momento = datetime.now(UTC)
        prototype.status = PrototypeStatus.CANCELLED
        prototype.cancelled_at = momento
        prototype.updated_at = momento
        await self._session.flush()

        self._audit.record_changes(
            entity_type=PROTOTYPE_ENTITY,
            entity_id=str(prototype.id),
            changes={
                "status": (PrototypeStatus.CREATED.value, prototype.status.value),
                "cancelled_at": (None, momento.isoformat()),
            },
            user_id=user.id,
            user_display_name=user.display_name,
        )
        return prototype, True

    # -- Evaluacion --------------------------------------------------------
    async def _decide(
        self,
        prototype_id: int,
        decision: PrototypeApproval,
        *,
        user: AuthenticatedUser,
        note: str | None,
    ) -> Prototype:
        prototype = await self.get(prototype_id, for_update=True)
        if (
            prototype.status is not PrototypeStatus.COMPLETED
            or prototype.approval is not PrototypeApproval.PENDING
        ):
            raise PrototypeNotDecidableError()

        momento = datetime.now(UTC)
        anterior = prototype.approval
        prototype.approval = decision
        prototype.decided_at = momento
        prototype.updated_at = momento
        if note:
            prototype.notes = f"{prototype.notes}\n{note}" if prototype.notes else note
        await self._session.flush()

        self._audit.record_changes(
            entity_type=PROTOTYPE_ENTITY,
            entity_id=str(prototype.id),
            changes={
                "approval": (anterior.value, decision.value),
                "decided_at": (None, momento.isoformat()),
            },
            user_id=user.id,
            user_display_name=user.display_name,
        )
        return prototype

    async def approve(
        self, prototype_id: int, *, user: AuthenticatedUser, note: str | None = None
    ) -> Prototype:
        """Da por buena la muestra. **No crea ni arranca ninguna produccion.**"""
        return await self._decide(prototype_id, PrototypeApproval.APPROVED, user=user, note=note)

    async def reject(
        self, prototype_id: int, *, user: AuthenticatedUser, note: str | None = None
    ) -> Prototype:
        """Descarta la muestra. Lo rechazado no se reescribe: se itera."""
        return await self._decide(prototype_id, PrototypeApproval.REJECTED, user=user, note=note)

    # -- Iteraciones -------------------------------------------------------
    async def create_successor(
        self, prototype_id: int, *, user: AuthenticatedUser, notes: str | None = None
    ) -> Prototype:
        """Crea la siguiente muestra a partir de una que no valio.

        Copia la INTENCION —a que pedido y producto va, de que almacen sale,
        cuantas piezas, con que materiales— y nada de la historia: ni el
        estado, ni las fechas, ni la decision, ni un solo movimiento. La
        anterior se queda como estaba, que es el punto: un rechazo no se
        reescribe.
        """
        anterior = await self.get(prototype_id, for_update=True)
        await self._guard_lineage(anterior)

        sucesor = await self.create(
            name=anterior.name,
            quantity=anterior.quantity,
            quotation_id=anterior.quotation_id,
            product_id=anterior.product_id,
            stock_location_id=anterior.stock_location_id,
            target_days=anterior.target_days,
            notes=notes,
            materials=[
                MaterialInput(product_id=linea.product_id, quantity=linea.quantity)
                for linea in anterior.lines
            ],
            user=user,
            supersedes_prototype_id=anterior.id,
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # El UNIQUE de sucesor unico. Se traduce a dominio: un error de
            # PostgreSQL en la respuesta no le dice nada a quien lo lee.
            raise PrototypeAlreadySupersededError() from exc
        return sucesor

    async def _guard_lineage(self, anterior: Prototype) -> None:
        """Impide cadenas imposibles antes de tocar la base.

        El auto-reemplazo lo corta un CHECK de fila; los ciclos largos no los
        ve la base —requieren recorrer la cadena— asi que se recorren aqui. Sin
        esto, A→B→C→A dejaria un grupo de muestras donde ninguna es la vigente
        y el guardia de produccion no sabria que responder.
        """
        visitados: set[int] = {anterior.id}
        actual = anterior.supersedes_prototype_id
        while actual is not None:
            if actual in visitados:
                raise PrototypeLineageInvalidError(
                    "La cadena de iteraciones se cierra sobre si misma"
                )
            visitados.add(actual)
            actual = await self._session.scalar(
                select(Prototype.supersedes_prototype_id).where(Prototype.id == actual)
            )

    # -- Requisito vigente -------------------------------------------------
    async def current_effective(self, prototype: Prototype) -> Prototype:
        """La ultima muestra de la cadena: la que manda hoy.

        Se recorre la cadena hacia adelante en vez de coger el id mas alto: dos
        cadenas independientes de la misma cotizacion tienen ids entremezclados
        y «la mas nueva» no es «la que sustituye a esta».
        """
        actual = prototype
        visitados: set[int] = {actual.id}
        while True:
            siguiente = await self._session.scalar(
                self._base_query().where(Prototype.supersedes_prototype_id == actual.id)
            )
            if siguiente is None or siguiente.id in visitados:
                return actual
            visitados.add(siguiente.id)
            actual = siguiente

    async def blocking_prototypes(self, quotation_id: int) -> list[Prototype]:
        """Las muestras de esa cotizacion que hoy impiden fabricar en serie.

        Solo cuentan las VIGENTES de cada cadena: una rechazada que ya tiene
        sucesora aprobada no bloquea nada, porque la decision vigente es la de
        la sucesora. Y una cadena cuya vigente esta anulada deja de ser un
        requisito: si se creo por error, no puede dejar el pedido sin producir
        para siempre.
        """
        raices = list(
            (
                await self._session.scalars(
                    self._base_query().where(Prototype.quotation_id == quotation_id)
                )
            ).all()
        )
        vigentes: dict[int, Prototype] = {}
        for raiz in raices:
            vigente = await self.current_effective(raiz)
            vigentes[vigente.id] = vigente

        return [
            vigente
            for vigente in vigentes.values()
            if vigente.status is not PrototypeStatus.CANCELLED
            and vigente.approval is not PrototypeApproval.APPROVED
        ]

    # -- Presentacion ------------------------------------------------------
    async def present(self, prototype: Prototype):  # type: ignore[no-untyped-def]
        """Arma la respuesta completa, con la disponibilidad recalculada.

        La disponibilidad se calcula aqui y viaja como codigos. No se manda al
        navegador lo necesario para que la deduzca por su cuenta: una segunda
        implementacion de la regla es una segunda regla, y el dia que discrepen
        ganara la que no consume material.
        """
        from app.schemas.prototypes import (
            PrototypeIssueOut,
            PrototypeMaterialOut,
            PrototypeOut,
            PrototypeReadinessOut,
        )

        quotation = (
            await self._session.get(Quotation, prototype.quotation_id)
            if prototype.quotation_id is not None
            else None
        )
        readiness = await self.evaluate_readiness(prototype)

        return PrototypeOut(
            id=prototype.id,
            code=prototype.code,
            name=prototype.name,
            status=prototype.status,
            approval=prototype.approval,
            quotation_id=prototype.quotation_id,
            quotation_code=quotation.code if quotation else None,
            product_id=prototype.product_id,
            stock_location_id=prototype.stock_location_id,
            quantity=prototype.quantity,
            target_days=prototype.target_days,
            requested_at=prototype.requested_at,
            started_at=prototype.started_at,
            completed_at=prototype.completed_at,
            cancelled_at=prototype.cancelled_at,
            decided_at=prototype.decided_at,
            supersedes_prototype_id=prototype.supersedes_prototype_id,
            material_count=len(prototype.lines),
            notes=prototype.notes,
            quotation_payment_status=(
                quotation.payment_status.value
                if quotation is not None and quotation.payment_status is not None
                else None
            ),
            materials=[
                PrototypeMaterialOut(
                    id=linea.id,
                    product_id=linea.product_id,
                    sort_order=linea.sort_order,
                    product_name=linea.product_name_snapshot,
                    product_internal_reference=linea.product_internal_reference_snapshot,
                    quantity=linea.quantity,
                    uom_code=linea.uom_code,
                )
                for linea in prototype.lines
            ],
            readiness=PrototypeReadinessOut(
                ready=readiness.ready,
                issues=[
                    PrototypeIssueOut(
                        code=issue.code,
                        product_id=issue.product_id,
                        product_name=issue.product_name,
                        required_quantity=(
                            None
                            if issue.required_quantity is None
                            else str(issue.required_quantity)
                        ),
                        available_quantity=(
                            None
                            if issue.available_quantity is None
                            else str(issue.available_quantity)
                        ),
                        uom=issue.uom,
                    )
                    for issue in readiness.issues
                ],
            ),
        )

    async def present_summary(self, prototype: Prototype):  # type: ignore[no-untyped-def]
        from app.schemas.prototypes import PrototypeSummaryOut

        quotation = (
            await self._session.get(Quotation, prototype.quotation_id)
            if prototype.quotation_id is not None
            else None
        )
        return PrototypeSummaryOut(
            id=prototype.id,
            code=prototype.code,
            name=prototype.name,
            status=prototype.status,
            approval=prototype.approval,
            quotation_id=prototype.quotation_id,
            quotation_code=quotation.code if quotation else None,
            product_id=prototype.product_id,
            stock_location_id=prototype.stock_location_id,
            quantity=prototype.quantity,
            target_days=prototype.target_days,
            requested_at=prototype.requested_at,
            started_at=prototype.started_at,
            completed_at=prototype.completed_at,
            cancelled_at=prototype.cancelled_at,
            decided_at=prototype.decided_at,
            supersedes_prototype_id=prototype.supersedes_prototype_id,
            material_count=len(prototype.lines),
        )


async def assert_prototypes_approved(
    session: AsyncSession, service: PrototypeService, order: ProductionOrder
) -> None:
    """Guardia de la produccion final. Fase 009K.

    Vive fuera de `PrototypeService` para que `ProductionOrderService` no tenga
    que conocerlo entero: solo necesita preguntar una cosa.

    Una orden cubre la cotizacion completa, asi que basta con que UNA muestra
    vigente de ese pedido no este aprobada para que no se pueda arrancar: no
    hay forma de fabricar media orden.
    """
    pendientes = await service.blocking_prototypes(order.quotation_id)
    if pendientes:
        raise ProductionOrderPrototypeNotApprovedError(
            details=[
                {
                    "code": prototipo.code,
                    "status": prototipo.status.value,
                    "approval": prototipo.approval.value,
                }
                for prototipo in pendientes
            ]
        )


class ProductionOrderPrototypeNotApprovedError(APIError):
    """La muestra de ese pedido todavia no esta aprobada. Fase 009K.

    Es 409 y no 403 por lo mismo que el guardia de pago: no es un problema de
    permisos sino del estado del negocio, y un 403 mandaria a buscar a un
    administrador que no puede arreglarlo dando permisos.
    """

    status_code = 409
    code = "PRODUCTION_ORDER_PROTOTYPE_NOT_APPROVED"
    message = "El prototipo de esta cotizacion debe estar aprobado antes de producir"


__all__ = [
    "CONSUMABLE_TYPES",
    "PROTOTYPE_ENTITY",
    "MaterialInput",
    "ProductionOrderPrototypeNotApprovedError",
    "PrototypeAlreadySupersededError",
    "PrototypeIssue",
    "PrototypeLineageInvalidError",
    "PrototypeMaterialDuplicatedError",
    "PrototypeMaterialNotConsumableError",
    "PrototypeMaterialWithoutUomError",
    "PrototypeNotCancellableError",
    "PrototypeNotCompletableError",
    "PrototypeNotDecidableError",
    "PrototypeNotEditableError",
    "PrototypeNotFoundError",
    "PrototypeNotReadyError",
    "PrototypeNotStartableError",
    "PrototypeProductNotInQuotationError",
    "PrototypeReadiness",
    "PrototypeReadinessCode",
    "PrototypeService",
    "assert_prototypes_approved",
]
