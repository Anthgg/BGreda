"""Servicios de hornos, tarifas y hojas de quema.

El backend es la autoridad: capacidades, tarifas, factores, volumenes, reparto,
costos, estados y correlativos se resuelven aqui. Lo que llegue del cliente en
esos campos se ignora; solo se aceptan los datos de captura (que horno, que
piezas, que dimensiones).

## Capacidad y ocupacion

El documento funcional mide la ocupacion **por pieza contra la capacidad del
horno elegido** (``volumen_linea / capacidad``), no por sesion. Es lo que da
sentido economico al factor: quien mete una pieza que solo ocupa el 5 % del
horno paga x3, porque la quema se paga entera. Por eso el bloqueo por capacidad
se aplica cuando una **linea** supera el 100 % del horno que la valora, y la
ocupacion agregada de cada sesion se calcula y se informa, pero no bloquea: en
la propia hoja de referencia las tres piezas suman mas que el horno pequeno
porque describen varias hornadas, no una sola carga.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.core.firings import (
    FiringError,
    FiringRateMissingError,
    KilnCapacityExceededError,
    LineInput,
    OccupancyFactorMissingError,
    SessionInput,
    compute_firing,
)
from app.models.audit import AuditAction
from app.models.firings import (
    Firing,
    FiringKilnSession,
    FiringLine,
    FiringStatus,
    FiringType,
    Kiln,
    KilnOccupancyFactor,
    KilnRate,
)
from app.models.masters import Product
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.schemas.firings import (
    FiringCalculateOut,
    FiringIn,
    FiringLineIn,
    FiringLineOut,
    FiringOut,
    FiringSessionIn,
    FiringSessionOut,
    FiringSummaryOut,
    KilnCreate,
    KilnOccupancyFactorIn,
    KilnOccupancyFactorOut,
    KilnOut,
    KilnRateIn,
    KilnRateOut,
    KilnUpdate,
)
from app.services.audit import AuditRecorder
from app.services.sequences import SequenceService

KILN_ENTITY = "kiln"
KILN_RATE_ENTITY = "kiln_rate"
FIRING_ENTITY = "firing"

#: Forma de fila de un ``Select``, para que el filtro compartido del listado
#: conserve el tipo de la consulta que recibe.
_Row = TypeVar("_Row", bound=tuple[Any, ...])


class KilnNotFoundError(APIError):
    status_code = 404
    code = "KILN_NOT_FOUND"
    message = "El horno no existe"


class KilnInactiveError(APIError):
    status_code = 422
    code = "KILN_INACTIVE"
    message = "El horno esta desactivado y no puede usarse en una quema"


class ProductNotFoundError(APIError):
    status_code = 422
    code = "PRODUCT_NOT_FOUND"
    message = "Uno o más productos seleccionados no existen"


class FiringNotFoundError(APIError):
    status_code = 404
    code = "FIRING_NOT_FOUND"
    message = "La hoja de quema no existe"


class FiringNotEditableError(APIError):
    status_code = 409
    code = "FIRING_NOT_EDITABLE"
    message = "Solo puede modificarse una hoja de quema en borrador"


class FiringNotConfirmableError(APIError):
    status_code = 409
    code = "FIRING_NOT_CONFIRMABLE"
    message = "Solo puede confirmarse una hoja de quema en borrador"


class FiringAlreadyCancelledError(APIError):
    status_code = 409
    code = "FIRING_ALREADY_CANCELLED"
    message = "La hoja de quema ya esta anulada"


def _session_key(kiln_id: int, firing_type: FiringType | str) -> str:
    return f"{kiln_id}:{FiringType(firing_type).value}"


# ---------------------------------------------------------------------------
# Hornos y tarifas
# ---------------------------------------------------------------------------
class KilnService:
    """Maestro de hornos, sus tarifas vigentes y su tabla de factores."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    async def _get(self, kiln_id: int) -> Kiln:
        # `populate_existing` no es opcional: si la instancia ya esta en el mapa
        # de identidad —expirada tras el commit de una peticion anterior— los
        # cargadores `selectinload` no repueblan sus colecciones, y el primer
        # acceso cae en una carga perezosa que en una sesion asincrona termina
        # en MissingGreenlet.
        result = await self._session.execute(
            select(Kiln)
            .where(Kiln.id == kiln_id)
            .options(selectinload(Kiln.rates), selectinload(Kiln.occupancy_factors))
            .execution_options(populate_existing=True)
        )
        kiln = result.scalar_one_or_none()
        if kiln is None:
            raise KilnNotFoundError()
        return kiln

    async def _next_code(self) -> str:
        """Codigo interno correlativo del maestro (KILN-001, KILN-002...).

        No es un correlativo documental: no se emite con ``SequenceService``
        porque no numera documentos. Se calcula sobre el maximo existente y la
        unicidad la garantiza la restriccion UNIQUE de la columna.
        """
        result = await self._session.execute(select(func.count()).select_from(Kiln))
        return f"KILN-{(result.scalar_one() or 0) + 1:03d}"

    @staticmethod
    def _validate_occupancy_factors(
        factors: Sequence[KilnOccupancyFactorIn],
    ) -> list[KilnOccupancyFactorIn]:
        if not factors:
            raise FiringError("La tabla de factores de ocupación no puede estar vacía")
        sorted_factors = sorted(factors, key=lambda f: f.min_percentage)
        if sorted_factors[0].min_percentage != 1:
            raise FiringError("El primer tramo de ocupación debe comenzar en 1 %")
        if sorted_factors[-1].max_percentage != 100:
            raise FiringError("El último tramo de ocupación debe terminar en 100 %")
        for i in range(1, len(sorted_factors)):
            expected_min = sorted_factors[i - 1].max_percentage + 1
            if sorted_factors[i].min_percentage != expected_min:
                raise FiringError(
                    "Discontinuidad o solapamiento en tramos: "
                    f"se esperaba inicio en {expected_min} % "
                    f"pero se encontró {sorted_factors[i].min_percentage} %"
                )
        return sorted_factors

    def _to_out(self, kiln: Kiln, *, effective_date: date | None = None) -> KilnOut:
        today = effective_date or datetime.now(UTC).date()
        low = high = None
        for rate in kiln.rates:
            if rate.valid_from <= today and (rate.valid_to is None or rate.valid_to > today):
                if rate.firing_type is FiringType.LOW and low is None:
                    low = rate.rate
                elif rate.firing_type is FiringType.HIGH and high is None:
                    high = rate.rate
        return KilnOut(
            id=kiln.id,
            code=kiln.code,
            name=kiln.name,
            capacity_volume_cm3=kiln.capacity_volume_cm3,
            active=kiln.active,
            notes=kiln.notes,
            created_at=kiln.created_at,
            updated_at=kiln.updated_at,
            current_low_rate=low,
            current_high_rate=high,
            occupancy_factors=[
                KilnOccupancyFactorOut.model_validate(factor) for factor in kiln.occupancy_factors
            ],
        )

    async def list_kilns(
        self,
        *,
        search: str | None = None,
        active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[KilnOut], int]:
        stmt = (
            select(Kiln)
            .options(selectinload(Kiln.rates), selectinload(Kiln.occupancy_factors))
            .execution_options(populate_existing=True)
        )
        count_stmt = select(func.count()).select_from(Kiln)

        if search:
            pattern = f"%{search.strip()}%"
            condition = or_(Kiln.name.ilike(pattern), Kiln.code.ilike(pattern))
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        if active is not None:
            stmt = stmt.where(Kiln.active.is_(active))
            count_stmt = count_stmt.where(Kiln.active.is_(active))

        total = (await self._session.execute(count_stmt)).scalar_one()
        result = await self._session.execute(stmt.order_by(Kiln.code).limit(limit).offset(offset))
        return [self._to_out(kiln) for kiln in result.scalars().unique()], total

    async def get_kiln(self, kiln_id: int) -> KilnOut:
        return self._to_out(await self._get(kiln_id))

    async def create_kiln(self, payload: KilnCreate, *, user: AuthenticatedUser) -> KilnOut:
        kiln = Kiln(
            code=await self._next_code(),
            name=payload.name,
            capacity_volume_cm3=payload.capacity_volume_cm3,
            active=payload.active,
            notes=payload.notes,
        )
        self._session.add(kiln)
        await self._session.flush()

        if payload.occupancy_factors:
            validated = self._validate_occupancy_factors(payload.occupancy_factors)
            for f in validated:
                self._session.add(
                    KilnOccupancyFactor(
                        kiln_id=kiln.id,
                        min_percentage=f.min_percentage,
                        max_percentage=f.max_percentage,
                        factor=f.factor,
                    )
                )
            await self._session.flush()

        self._audit.record_action(
            entity_type=KILN_ENTITY,
            entity_id=str(kiln.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": kiln.code,
                "name": kiln.name,
                "capacity_volume_cm3": str(kiln.capacity_volume_cm3),
            },
        )
        await self._session.refresh(kiln, ["rates", "occupancy_factors"])
        return self._to_out(kiln)

    async def update_kiln(
        self, kiln_id: int, payload: KilnUpdate, *, user: AuthenticatedUser
    ) -> KilnOut:
        kiln = await self._get(kiln_id)
        changes: dict[str, tuple[object, object]] = {}

        for field in ("name", "capacity_volume_cm3", "active", "notes"):
            if field not in payload.model_fields_set:
                continue
            new = getattr(payload, field)
            old = getattr(kiln, field)
            if old != new:
                changes[field] = (old, new)
                setattr(kiln, field, new)

        if changes:
            self._audit.record_changes(
                entity_type=KILN_ENTITY,
                entity_id=str(kiln.id),
                changes=changes,
                user_id=user.id,
                user_display_name=user.display_name,
            )
        await self._session.flush()
        # `updated_at` lleva `onupdate=func.now()`: el UPDATE lo deja expirado y
        # leerlo intentaria recargarlo por su cuenta, lo que en una sesion
        # asincrona termina en MissingGreenlet. Se recarga explicitamente.
        await self._session.refresh(kiln, ["updated_at"])
        return self._to_out(kiln)

    async def set_occupancy_factors(
        self,
        kiln_id: int,
        factors_in: list[KilnOccupancyFactorIn],
        *,
        user: AuthenticatedUser,
    ) -> list[KilnOccupancyFactorOut]:
        """Configura o reemplaza la tabla completa de factores de ocupacion del horno."""
        kiln = await self._get(kiln_id)
        validated = self._validate_occupancy_factors(factors_in)

        await self._session.execute(
            delete(KilnOccupancyFactor).where(KilnOccupancyFactor.kiln_id == kiln.id)
        )
        await self._session.flush()

        created_factors: list[KilnOccupancyFactor] = []
        for f in validated:
            factor_row = KilnOccupancyFactor(
                kiln_id=kiln.id,
                min_percentage=f.min_percentage,
                max_percentage=f.max_percentage,
                factor=f.factor,
            )
            self._session.add(factor_row)
            created_factors.append(factor_row)
        await self._session.flush()

        self._audit.record_action(
            entity_type=KILN_ENTITY,
            entity_id=str(kiln.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "action": "set_occupancy_factors",
                "kiln_code": kiln.code,
                "factor_count": len(created_factors),
            },
        )
        return [KilnOccupancyFactorOut.model_validate(f) for f in created_factors]

    async def set_rate(
        self, kiln_id: int, payload: KilnRateIn, *, user: AuthenticatedUser
    ) -> KilnRateOut:
        """Abre una tarifa nueva cerrando la vigente.

        Nunca se sobrescribe el importe anterior: una quema confirmada guarda su
        propio ``rate_snapshot``, y ademas el historial conserva desde y hasta
        cuando rigio cada tarifa.
        """
        kiln = await self._get(kiln_id)
        valid_from = payload.valid_from or datetime.now(UTC).date()

        previous: KilnRate | None = None
        for rate in kiln.rates:
            if rate.firing_type is payload.firing_type and rate.valid_to is None:
                previous = rate
                break

        if previous is not None:
            if previous.rate == payload.rate and previous.valid_from == valid_from:
                return KilnRateOut.model_validate(previous)
            # La vigencia anterior se cierra cuando empieza la nueva. Si la
            # nueva se fecha antes, se cierra el mismo dia en que abrio: la
            # restriccion exige `valid_to >= valid_from`.
            previous.valid_to = max(valid_from, previous.valid_from)
            self._audit.record_changes(
                entity_type=KILN_RATE_ENTITY,
                entity_id=str(previous.id),
                changes={"rate": (previous.rate, payload.rate)},
                user_id=user.id,
                user_display_name=user.display_name,
            )
            # El indice parcial solo admite una tarifa abierta: hay que cerrar la
            # anterior antes de insertar la nueva.
            await self._session.flush()

        created = KilnRate(
            kiln_id=kiln.id,
            firing_type=payload.firing_type,
            rate=payload.rate,
            valid_from=valid_from,
        )
        self._session.add(created)
        await self._session.flush()

        self._audit.record_action(
            entity_type=KILN_RATE_ENTITY,
            entity_id=str(created.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "kiln_id": kiln.id,
                "firing_type": payload.firing_type.value,
                "rate": str(payload.rate),
                "valid_from": valid_from.isoformat(),
            },
        )
        return KilnRateOut.model_validate(created)

    async def list_rates(self, kiln_id: int) -> list[KilnRateOut]:
        await self._get(kiln_id)
        result = await self._session.execute(
            select(KilnRate)
            .where(KilnRate.kiln_id == kiln_id)
            .order_by(KilnRate.firing_type, KilnRate.valid_from.desc(), KilnRate.id.desc())
        )
        return [KilnRateOut.model_validate(rate) for rate in result.scalars()]


# ---------------------------------------------------------------------------
# Hojas de quema
# ---------------------------------------------------------------------------
class FiringService:
    """Simulacion, captura, confirmacion y anulacion de hojas de quema."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        sequences: SequenceService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._sequences = sequences

    # -- Resolucion de datos maestros ---------------------------------------
    async def _load_kilns(self, kiln_ids: Sequence[int]) -> dict[int, Kiln]:
        if not kiln_ids:
            raise FiringError("La hoja de quema necesita al menos una sesion de horno")
        result = await self._session.execute(
            select(Kiln)
            .where(Kiln.id.in_(set(kiln_ids)))
            .options(selectinload(Kiln.occupancy_factors))
            .execution_options(populate_existing=True)
        )
        kilns = {kiln.id: kiln for kiln in result.scalars().unique()}
        missing = set(kiln_ids) - kilns.keys()
        if missing:
            raise KilnNotFoundError(f"Horno inexistente: {sorted(missing)}")
        return kilns

    async def _current_rates(
        self, kiln_ids: Sequence[int], *, effective_date: date
    ) -> dict[tuple[int, str], Decimal]:
        """Tarifas vigentes en la fecha efectiva indicada."""
        result = await self._session.execute(
            select(KilnRate)
            .where(
                KilnRate.kiln_id.in_(set(kiln_ids)),
                KilnRate.valid_from <= effective_date,
                or_(KilnRate.valid_to.is_(None), KilnRate.valid_to > effective_date),
            )
            .order_by(KilnRate.valid_from.desc(), KilnRate.id.desc())
        )
        rates: dict[tuple[int, str], Decimal] = {}
        for rate in result.scalars():
            key = (rate.kiln_id, FiringType(rate.firing_type).value)
            if key not in rates:
                rates[key] = rate.rate
        return rates

    async def _factor_tables(
        self, kilns: dict[int, Kiln]
    ) -> dict[int, list[tuple[int, int, Decimal]]]:
        tables: dict[int, list[tuple[int, int, Decimal]]] = {}
        for kiln_id, kiln in kilns.items():
            if not kiln.occupancy_factors:
                raise OccupancyFactorMissingError(
                    f"El horno «{kiln.name}» no tiene configurada su tabla de factores de ocupación"
                )
            tables[kiln_id] = [
                (factor.min_percentage, factor.max_percentage, factor.factor)
                for factor in sorted(kiln.occupancy_factors, key=lambda f: f.min_percentage)
            ]
        return tables

    async def _validate_products(self, payload: FiringIn) -> dict[int, str]:
        product_ids = {line.product_id for line in payload.lines if line.product_id is not None}
        if not product_ids:
            return {}
        result = await self._session.execute(
            select(Product.id, Product.internal_reference).where(Product.id.in_(product_ids))
        )
        found = {row[0]: row[1] for row in result.all()}
        missing = product_ids - found.keys()
        if missing:
            missing_str = ", ".join(str(i) for i in sorted(missing))
            raise ProductNotFoundError(
                f"Uno o más productos seleccionados no existen (IDs: {missing_str})"
            )
        return found

    async def _build(
        self, payload: FiringIn
    ) -> tuple[
        list[SessionInput],
        list[LineInput],
        dict[int, list[tuple[int, int, Decimal]]],
        dict[int, Kiln],
        dict[int, str],
    ]:
        """Traduce el cuerpo recibido a las entradas puras del calculo."""
        if not payload.sessions:
            raise FiringError("La hoja de quema necesita al menos una sesion de horno")
        if not payload.lines:
            raise FiringError("La hoja de quema necesita al menos una pieza")

        declared = {(item.kiln_id, item.firing_type.value) for item in payload.sessions}
        if len(declared) != len(payload.sessions):
            raise FiringError("Hay sesiones de horno repetidas en la hoja")

        references = await self._validate_products(payload)

        kiln_ids = [item.kiln_id for item in payload.sessions]
        kilns = await self._load_kilns(kiln_ids)
        for kiln in kilns.values():
            if not kiln.active:
                raise KilnInactiveError(f"El horno «{kiln.name}» esta desactivado")

        effective_date = payload.firing_date or payload.scheduled_date or datetime.now(UTC).date()
        rates = await self._current_rates(kiln_ids, effective_date=effective_date)
        sessions: list[SessionInput] = []
        for item in payload.sessions:
            rate_key = (item.kiln_id, item.firing_type.value)
            if rate_key not in rates:
                raise FiringRateMissingError(
                    f"El horno «{kilns[item.kiln_id].name}» no tiene tarifa vigente "
                    f"para la quema {item.firing_type.value}"
                )
            sessions.append(
                SessionInput(
                    key=_session_key(item.kiln_id, item.firing_type),
                    kiln_id=item.kiln_id,
                    firing_type=item.firing_type.value,
                    rate=rates[rate_key],
                    capacity=kilns[item.kiln_id].capacity_volume_cm3,
                )
            )

        known = {session.key for session in sessions}
        lines: list[LineInput] = []
        for line in payload.lines:
            keys: list[str] = []
            if line.low_kiln_id is not None:
                key = _session_key(line.low_kiln_id, FiringType.LOW)
                if key not in known:
                    raise FiringError(
                        f"«{line.description}»: no hay una sesion de quema baja "
                        "para el horno elegido"
                    )
                keys.append(key)
            if line.high_kiln_id is not None:
                key = _session_key(line.high_kiln_id, FiringType.HIGH)
                if key not in known:
                    raise FiringError(
                        f"«{line.description}»: no hay una sesion de quema alta "
                        "para el horno elegido"
                    )
                keys.append(key)
            if not keys:
                raise FiringError(
                    f"«{line.description}»: hay que indicar al menos un horno "
                    "para la quema baja o la alta"
                )
            if line.factor_kiln_id is not None and line.factor_kiln_id not in kilns:
                raise FiringError(
                    f"«{line.description}»: el horno del factor no participa en la hoja"
                )
            lines.append(
                LineInput(
                    quantity=line.quantity,
                    length_cm=line.length_cm,
                    width_cm=line.width_cm,
                    height_cm=line.height_cm,
                    session_keys=tuple(keys),
                    factor_kiln_id=line.factor_kiln_id,
                )
            )

        factors = await self._factor_tables(kilns)
        return sessions, lines, factors, kilns, references

    # -- Simulador ----------------------------------------------------------
    async def calculate(self, payload: FiringIn) -> FiringCalculateOut:
        """Calcula el costo de una hoja **sin persistir nada**.

        No inserta, no actualiza, no consume correlativo y no genera ningun
        movimiento de inventario.
        """
        sessions, lines, factors, kilns, references = await self._build(payload)
        math = compute_firing(sessions, lines, factors)

        session_out = [
            FiringSessionOut(
                kiln_id=result.kiln_id,
                kiln_code=kilns[result.kiln_id].code,
                kiln_name=kilns[result.kiln_id].name,
                firing_type=FiringType(result.firing_type),
                rate_snapshot=result.rate,
                capacity_snapshot=result.capacity,
                assigned_volume_cm3=result.assigned_volume_cm3,
                physical_occupancy_percentage=result.physical_occupancy_percentage,
                subtotal=result.subtotal,
                capacity_exceeded=result.capacity_exceeded,
                sort_order=item.sort_order,
            )
            for result, item in zip(math.sessions, payload.sessions, strict=True)
        ]

        line_out = [
            FiringLineOut(
                product_id=item.product_id,
                product_internal_reference=references.get(item.product_id)
                if item.product_id
                else None,
                description=item.description,
                quantity=item.quantity,
                length_cm=item.length_cm,
                width_cm=item.width_cm,
                height_cm=item.height_cm,
                unit_volume_cm3=result.unit_volume_cm3,
                total_volume_cm3=result.total_volume_cm3,
                low_kiln_id=item.low_kiln_id,
                high_kiln_id=item.high_kiln_id,
                factor_kiln_id=result.factor_kiln_id,
                volume_share=result.share,
                occupancy_percentage=result.occupancy_percentage,
                occupancy_bracket=result.occupancy_bracket,
                occupancy_factor=result.occupancy_factor,
                base_cost=result.base_cost,
                allocated_cost=result.allocated_cost,
                capacity_exceeded=result.capacity_exceeded,
                notes=item.notes,
                sort_order=item.sort_order,
            )
            for result, item in zip(math.lines, payload.lines, strict=True)
        ]

        return FiringCalculateOut(
            total_volume_cm3=math.total_volume_cm3,
            subtotal=math.subtotal,
            total_cost=math.total_cost,
            occupancy_percentage=math.occupancy_percentage,
            occupancy_factor=math.occupancy_factor,
            capacity_exceeded=math.capacity_exceeded,
            sessions=session_out,
            lines=line_out,
        )

    async def _product_references(self, product_ids: Sequence[int]) -> dict[int, str]:
        if not product_ids:
            return {}
        result = await self._session.execute(
            select(Product.id, Product.internal_reference).where(Product.id.in_(set(product_ids)))
        )
        return {row[0]: row[1] for row in result.all()}

    # -- Persistencia -------------------------------------------------------
    async def _apply(self, firing: Firing, payload: FiringIn) -> None:
        """Reescribe sesiones y lineas de la hoja y recalcula todos los costos."""
        sessions, lines, factors, _kilns, _references = await self._build(payload)
        math = compute_firing(sessions, lines, factors)

        firing.scheduled_date = payload.scheduled_date
        firing.firing_date = payload.firing_date
        firing.notes = payload.notes
        firing.total_volume_cm3 = math.total_volume_cm3
        firing.subtotal = math.subtotal
        firing.total_cost = math.total_cost
        firing.occupancy_percentage = math.occupancy_percentage
        firing.occupancy_factor = math.occupancy_factor

        # Se borra con DELETE en vez de vaciar las colecciones del ORM: tocar
        # `firing.sessions` obligaria a cargarlas primero, y en una sesion
        # asincrona esa carga perezosa revienta con MissingGreenlet. Ademas no
        # tiene sentido traer filas solo para descartarlas.
        await self._session.execute(delete(FiringLine).where(FiringLine.firing_id == firing.id))
        await self._session.execute(
            delete(FiringKilnSession).where(FiringKilnSession.firing_id == firing.id)
        )
        await self._session.flush()

        created: dict[str, FiringKilnSession] = {}
        for result, item in zip(math.sessions, payload.sessions, strict=True):
            session_row = FiringKilnSession(
                firing_id=firing.id,
                kiln_id=result.kiln_id,
                firing_type=FiringType(result.firing_type),
                rate_snapshot=result.rate,
                capacity_snapshot=result.capacity,
                subtotal=result.subtotal,
                sort_order=item.sort_order,
            )
            self._session.add(session_row)
            created[result.key] = session_row
        await self._session.flush()

        for line_result, line_item in zip(math.lines, payload.lines, strict=True):
            low = (
                created.get(_session_key(line_item.low_kiln_id, FiringType.LOW))
                if line_item.low_kiln_id
                else None
            )
            high = (
                created.get(_session_key(line_item.high_kiln_id, FiringType.HIGH))
                if line_item.high_kiln_id
                else None
            )
            self._session.add(
                FiringLine(
                    firing_id=firing.id,
                    product_id=line_item.product_id,
                    description=line_item.description,
                    quantity=line_item.quantity,
                    length_cm=line_item.length_cm,
                    width_cm=line_item.width_cm,
                    height_cm=line_item.height_cm,
                    unit_volume_cm3=line_result.unit_volume_cm3,
                    total_volume_cm3=line_result.total_volume_cm3,
                    low_session_id=low.id if low else None,
                    high_session_id=high.id if high else None,
                    factor_kiln_id=line_result.factor_kiln_id,
                    occupancy_percentage=line_result.occupancy_percentage,
                    occupancy_bracket=line_result.occupancy_bracket,
                    occupancy_factor=line_result.occupancy_factor,
                    base_cost=line_result.base_cost,
                    allocated_cost=line_result.allocated_cost,
                    notes=line_item.notes,
                    sort_order=line_item.sort_order,
                )
            )
        await self._session.flush()
        self._session.expire(firing, ["sessions", "lines"])

    async def create(self, payload: FiringIn, *, user: AuthenticatedUser) -> FiringOut:
        """Crea la hoja en borrador consumiendo un correlativo FIRING."""
        code = await self._sequences.issue(SequenceType.FIRING, user_id=user.id)
        firing = Firing(
            code=code,
            status=FiringStatus.DRAFT,
            created_by_id=user.id,
        )
        self._session.add(firing)
        await self._session.flush()

        await self._apply(firing, payload)

        self._audit.record_action(
            entity_type=FIRING_ENTITY,
            entity_id=str(firing.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": firing.code, "status": firing.status.value},
        )
        return await self.get(firing.id)

    async def update(
        self, firing_id: int, payload: FiringIn, *, user: AuthenticatedUser
    ) -> FiringOut:
        firing = await self._get(firing_id, for_update=True)
        if firing.status is not FiringStatus.DRAFT:
            raise FiringNotEditableError()

        await self._apply(firing, payload)
        self._audit.record_action(
            entity_type=FIRING_ENTITY,
            entity_id=str(firing.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"code": firing.code, "total_cost": str(firing.total_cost)},
        )
        return await self.get(firing.id)

    async def confirm(self, firing_id: int, *, user: AuthenticatedUser) -> FiringOut:
        """Congela la hoja: recalcula, valida capacidad y fija los snapshots."""
        firing = await self._get(firing_id, for_update=True)
        if firing.status is not FiringStatus.DRAFT:
            raise FiringNotConfirmableError()

        # Se recalcula desde los datos capturados con las tarifas vigentes hoy:
        # confirmar es el momento en que el costo queda fijado.
        payload = self._to_input(firing)
        sessions, lines, factors, _, _ = await self._build(payload)
        math = compute_firing(sessions, lines, factors)

        excedidas = [
            payload.lines[index].description
            for index, line in enumerate(math.lines)
            if line.capacity_exceeded
        ]
        if excedidas:
            raise KilnCapacityExceededError(
                "El volumen supera la capacidad del horno en: " + ", ".join(excedidas)
            )

        await self._apply(firing, payload)
        firing.status = FiringStatus.CONFIRMED
        firing.confirmed_at = datetime.now(UTC)
        await self._session.flush()

        self._audit.record_action(
            entity_type=FIRING_ENTITY,
            entity_id=str(firing.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": firing.code,
                "status": FiringStatus.CONFIRMED.value,
                "total_cost": str(firing.total_cost),
                "subtotal": str(firing.subtotal),
            },
        )
        return await self.get(firing.id)

    async def cancel(self, firing_id: int, *, user: AuthenticatedUser) -> FiringOut:
        """Anula la hoja. Nunca se borra: el historial no se reescribe."""
        firing = await self._get(firing_id, for_update=True)
        if firing.status is FiringStatus.CANCELLED:
            raise FiringAlreadyCancelledError()

        previous = firing.status
        firing.status = FiringStatus.CANCELLED
        firing.cancelled_at = datetime.now(UTC)
        await self._session.flush()

        self._audit.record_action(
            entity_type=FIRING_ENTITY,
            entity_id=str(firing.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": firing.code,
                "status": FiringStatus.CANCELLED.value,
                "previous_status": previous.value,
            },
        )
        return await self.get(firing.id)

    def _to_input(self, firing: Firing) -> FiringIn:
        """Reconstruye el cuerpo de captura a partir de la hoja almacenada."""
        by_id = {session.id: session for session in firing.sessions}
        return FiringIn(
            scheduled_date=firing.scheduled_date,
            firing_date=firing.firing_date,
            notes=firing.notes,
            sessions=[
                FiringSessionIn(
                    kiln_id=session.kiln_id,
                    firing_type=session.firing_type,
                    sort_order=session.sort_order,
                )
                for session in firing.sessions
            ],
            lines=[
                FiringLineIn(
                    product_id=line.product_id,
                    description=line.description,
                    quantity=line.quantity,
                    length_cm=line.length_cm,
                    width_cm=line.width_cm,
                    height_cm=line.height_cm,
                    low_kiln_id=(
                        by_id[line.low_session_id].kiln_id if line.low_session_id in by_id else None
                    ),
                    high_kiln_id=(
                        by_id[line.high_session_id].kiln_id
                        if line.high_session_id in by_id
                        else None
                    ),
                    factor_kiln_id=line.factor_kiln_id,
                    notes=line.notes,
                    sort_order=line.sort_order,
                )
                for line in firing.lines
            ],
        )

    # -- Lectura ------------------------------------------------------------
    async def _get(self, firing_id: int, *, for_update: bool = False) -> Firing:
        stmt = (
            select(Firing)
            .where(Firing.id == firing_id)
            .options(selectinload(Firing.sessions), selectinload(Firing.lines))
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        firing = result.scalar_one_or_none()
        if firing is None:
            raise FiringNotFoundError()
        return firing

    async def get(self, firing_id: int) -> FiringOut:
        firing = await self._get(firing_id)
        kiln_ids = {session.kiln_id for session in firing.sessions}
        kilns = await self._load_kilns(sorted(kiln_ids)) if kiln_ids else {}
        by_id = {session.id: session for session in firing.sessions}
        references = await self._product_references(
            [line.product_id for line in firing.lines if line.product_id is not None]
        )
        total = firing.total_volume_cm3 or Decimal(0)

        return FiringOut(
            id=firing.id,
            code=firing.code,
            status=firing.status,
            scheduled_date=firing.scheduled_date,
            firing_date=firing.firing_date,
            notes=firing.notes,
            total_volume_cm3=firing.total_volume_cm3,
            occupancy_percentage=firing.occupancy_percentage,
            occupancy_factor=firing.occupancy_factor,
            subtotal=firing.subtotal,
            total_cost=firing.total_cost,
            created_by_id=firing.created_by_id,
            confirmed_at=firing.confirmed_at,
            cancelled_at=firing.cancelled_at,
            created_at=firing.created_at,
            updated_at=firing.updated_at,
            sessions=[
                FiringSessionOut(
                    id=session.id,
                    kiln_id=session.kiln_id,
                    kiln_code=kilns[session.kiln_id].code,
                    kiln_name=kilns[session.kiln_id].name,
                    firing_type=session.firing_type,
                    rate_snapshot=session.rate_snapshot,
                    capacity_snapshot=session.capacity_snapshot,
                    assigned_volume_cm3=sum(
                        (
                            line.total_volume_cm3
                            for line in firing.lines
                            if session.id in (line.low_session_id, line.high_session_id)
                        ),
                        Decimal(0),
                    ),
                    physical_occupancy_percentage=self._session_occupancy(firing, session),
                    subtotal=session.subtotal,
                    capacity_exceeded=self._session_occupancy(firing, session) > Decimal(100),
                    sort_order=session.sort_order,
                )
                for session in firing.sessions
            ],
            lines=[
                FiringLineOut(
                    id=line.id,
                    product_id=line.product_id,
                    product_internal_reference=references.get(line.product_id)
                    if line.product_id
                    else None,
                    description=line.description,
                    quantity=line.quantity,
                    length_cm=line.length_cm,
                    width_cm=line.width_cm,
                    height_cm=line.height_cm,
                    unit_volume_cm3=line.unit_volume_cm3,
                    total_volume_cm3=line.total_volume_cm3,
                    low_kiln_id=by_id[line.low_session_id].kiln_id
                    if line.low_session_id in by_id
                    else None,
                    high_kiln_id=by_id[line.high_session_id].kiln_id
                    if line.high_session_id in by_id
                    else None,
                    factor_kiln_id=line.factor_kiln_id,
                    volume_share=(line.total_volume_cm3 / total) if total > 0 else Decimal(0),
                    occupancy_percentage=line.occupancy_percentage,
                    occupancy_bracket=line.occupancy_bracket,
                    occupancy_factor=line.occupancy_factor,
                    base_cost=line.base_cost,
                    allocated_cost=line.allocated_cost,
                    capacity_exceeded=line.occupancy_percentage > Decimal(100),
                    notes=line.notes,
                    sort_order=line.sort_order,
                )
                for line in firing.lines
            ],
        )

    @staticmethod
    def _session_occupancy(firing: Firing, session: FiringKilnSession) -> Decimal:
        assigned = sum(
            (
                line.total_volume_cm3
                for line in firing.lines
                if session.id in (line.low_session_id, line.high_session_id)
            ),
            Decimal(0),
        )
        if session.capacity_snapshot <= 0:
            return Decimal(0)
        return assigned / session.capacity_snapshot * Decimal(100)

    async def list_firings(
        self,
        *,
        search: str | None = None,
        status: FiringStatus | None = None,
        kiln_id: int | None = None,
        firing_type: FiringType | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FiringSummaryOut], int]:
        """Listado paginado y filtrado en el servidor."""

        def apply(stmt: Select[_Row]) -> Select[_Row]:
            if search:
                stmt = stmt.where(Firing.code.ilike(f"%{search.strip()}%"))
            if status is not None:
                stmt = stmt.where(Firing.status == status)
            if date_from is not None:
                stmt = stmt.where(Firing.firing_date >= date_from)
            if date_to is not None:
                stmt = stmt.where(Firing.firing_date <= date_to)
            if kiln_id is not None or firing_type is not None:
                condition = select(FiringKilnSession.id).where(
                    FiringKilnSession.firing_id == Firing.id
                )
                if kiln_id is not None:
                    condition = condition.where(FiringKilnSession.kiln_id == kiln_id)
                if firing_type is not None:
                    condition = condition.where(FiringKilnSession.firing_type == firing_type)
                stmt = stmt.where(condition.exists())
            return stmt

        total = (
            await self._session.execute(apply(select(func.count()).select_from(Firing)))
        ).scalar_one()

        result = await self._session.execute(
            apply(select(Firing))
            .options(selectinload(Firing.sessions), selectinload(Firing.lines))
            .execution_options(populate_existing=True)
            .order_by(Firing.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items = [
            FiringSummaryOut(
                id=firing.id,
                code=firing.code,
                status=firing.status,
                scheduled_date=firing.scheduled_date,
                firing_date=firing.firing_date,
                total_volume_cm3=firing.total_volume_cm3,
                total_cost=firing.total_cost,
                line_count=len(firing.lines),
                session_count=len(firing.sessions),
                created_at=firing.created_at,
            )
            for firing in result.scalars().unique()
        ]
        return items, int(total)


__all__ = [
    "FiringAlreadyCancelledError",
    "FiringNotConfirmableError",
    "FiringNotEditableError",
    "FiringNotFoundError",
    "FiringService",
    "KilnInactiveError",
    "KilnNotFoundError",
    "KilnService",
]
