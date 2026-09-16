"""Adicionales del Cotizador V2 (correccion 010H).

El empaque especial, el molde de una pieza rara, un sello personalizado. La hoja
«Cotizador V2» del Excel aprobado los tiene desde el principio —«Otros extras de
esta cotizacion», celda B25— y los suma al Costo de Produccion Y al Costo Real.
En V2 no existian: no habia donde ponerlos, y acababan escondidos en el precio o
disfrazados de material.

## Por que no son un material ni una tecnica

Un material se consume del inventario y tiene peso. Una tecnica tiene
rendimiento y horas de alguien. Un adicional no tiene ni lo uno ni lo otro: es
un costo que una persona decide, con su cantidad y su precio. Mezclarlo con los
otros dos es lo que produce doble cobro: el asa cobrada como tecnica y otra vez
como «adicional armado de asa».

## Maestro y cotizacion

El maestro guarda lo que suele costar cada concepto; la cotizacion guarda lo que
costo ESTA vez, congelado. Subir manana el precio del empaque no cambia una
cotizacion ya entregada.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct, V2QuotationStatus
from app.models.quoter_v2_processes import V2Extra, V2QuotationExtra
from app.services.audit import AuditRecorder
from app.services.quoter_v2_pricing import refresh_pricing

ZERO = Decimal(0)

V2_EXTRA_ENTITY = "v2_extra"
V2_QUOTATION_EXTRA_ENTITY = "v2_quotation_extra"

#: El concepto se retiro del maestro despues de entrar en esta cotizacion. La
#: linea se queda con lo que congelo; solo se avisa.
WARN_EXTRA_INACTIVE = "V2_EXTRA_INACTIVE"


class V2ExtraNotFoundError(APIError):
    status_code = 404
    code = "V2_EXTRA_NOT_FOUND"
    message = "El adicional no existe"


class V2ExtraInputInvalid(APIError):
    status_code = 422
    code = "V2_EXTRA_INPUT_INVALID"
    message = "Los datos del adicional no son validos"


class V2ExtraVersionConflictError(APIError):
    status_code = 409
    code = "V2_EXTRA_VERSION_CONFLICT"
    message = "Alguien cambio este adicional mientras usted lo editaba"


class V2ExtraQuotationNotEditableError(APIError):
    status_code = 409
    code = "V2_EXTRA_QUOTATION_NOT_EDITABLE"
    message = "La cotizacion ya no admite cambios"


class V2ExtraService:
    """Maestro de conceptos adicionales y adicionales de una cotizacion."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------
    # Maestro
    # ------------------------------------------------------------------
    async def list_extras(self, *, active_only: bool = False) -> list[V2Extra]:
        consulta = select(V2Extra).order_by(V2Extra.name)
        if active_only:
            consulta = consulta.where(V2Extra.active.is_(True))
        return list((await self._session.scalars(consulta)).all())

    async def create_extra(self, data: dict[str, Any], *, user: Any) -> V2Extra:
        nombre = str(data.get("name", "")).strip()
        if not nombre:
            raise V2ExtraInputInvalid("El adicional necesita un nombre")
        costo = Decimal(str(data.get("unit_cost", "0")))
        if costo < ZERO:
            raise V2ExtraInputInvalid("El costo unitario no puede ser negativo")
        repetido = await self._session.scalar(
            select(func.count())
            .select_from(V2Extra)
            .where(func.lower(V2Extra.name) == nombre.lower())
        )
        if repetido:
            raise V2ExtraInputInvalid("Ya existe un adicional con ese nombre")

        extra = V2Extra(
            name=nombre,
            unit=str(data.get("unit") or "servicio").strip() or "servicio",
            unit_cost=costo,
            active=bool(data.get("active", True)),
            notes=data.get("notes"),
            version=1,
        )
        self._session.add(extra)
        await self._session.flush()
        self._audit.record_action(
            entity_type=V2_EXTRA_ENTITY,
            entity_id=str(extra.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"name": extra.name},
        )
        return extra

    async def update_extra(self, extra_id: int, data: dict[str, Any], *, user: Any) -> V2Extra:
        # Con bloqueo: sin el, dos ediciones que leyeran la misma version
        # pasarian las dos la comprobacion y una se perderia sin que nadie lo
        # supiera, que es justo lo que `expected_version` existe para impedir.
        extra = (
            await self._session.scalars(
                select(V2Extra).where(V2Extra.id == extra_id).with_for_update()
            )
        ).one_or_none()
        if extra is None:
            raise V2ExtraNotFoundError()
        esperada = data.get("expected_version")
        if esperada is not None and int(esperada) != extra.version:
            raise V2ExtraVersionConflictError()

        if "name" in data:
            nombre = str(data["name"]).strip()
            if not nombre:
                raise V2ExtraInputInvalid("El adicional necesita un nombre")
            extra.name = nombre
        if data.get("unit"):
            extra.unit = str(data["unit"]).strip()
        if "unit_cost" in data and data["unit_cost"] is not None:
            costo = Decimal(str(data["unit_cost"]))
            if costo < ZERO:
                raise V2ExtraInputInvalid("El costo unitario no puede ser negativo")
            extra.unit_cost = costo
        if "active" in data:
            extra.active = bool(data["active"])
        if "notes" in data:
            extra.notes = data["notes"]
        extra.version += 1

        self._audit.record_action(
            entity_type=V2_EXTRA_ENTITY,
            entity_id=str(extra.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"name": extra.name},
        )
        await self._session.flush()
        return extra

    # ------------------------------------------------------------------
    # Adicionales de una cotizacion
    # ------------------------------------------------------------------
    async def list_quotation_extras(self, quotation_id: int) -> list[V2QuotationExtra]:
        consulta = (
            select(V2QuotationExtra)
            .where(V2QuotationExtra.v2_quotation_id == quotation_id)
            .order_by(V2QuotationExtra.sort_order, V2QuotationExtra.id)
        )
        return list((await self._session.scalars(consulta)).all())

    async def add_quotation_extra(
        self, quotation_id: int, data: dict[str, Any], *, user: Any
    ) -> tuple[V2QuotationExtra, list[str]]:
        quotation = await self._draft(quotation_id)
        extra = await self._session.get(V2Extra, int(data["v2_extra_id"]))
        if extra is None:
            raise V2ExtraNotFoundError()
        avisos = [] if extra.active else [WARN_EXTRA_INACTIVE]

        linea_id = data.get("v2_quotation_product_id")
        if linea_id is not None:
            await self._linea(quotation_id, int(linea_id))

        cantidad = Decimal(str(data.get("quantity", "0")))
        if cantidad < ZERO:
            raise V2ExtraInputInvalid("La cantidad no puede ser negativa")
        precio = data.get("unit_cost")
        siguiente = await self._session.scalar(
            select(func.coalesce(func.max(V2QuotationExtra.sort_order), -1) + 1).where(
                V2QuotationExtra.v2_quotation_id == quotation_id
            )
        )

        fila = V2QuotationExtra(
            v2_quotation_id=quotation.id,
            v2_quotation_product_id=int(linea_id) if linea_id is not None else None,
            v2_extra_id=extra.id,
            name_snapshot=extra.name,
            unit_snapshot=extra.unit,
            unit_cost_snapshot=Decimal(str(precio)) if precio is not None else extra.unit_cost,
            unit_cost_is_override=precio is not None,
            description=data.get("description"),
            quantity=cantidad,
            sort_order=int(siguiente or 0),
            total_cost=ZERO,
        )
        if fila.unit_cost_snapshot < ZERO:
            raise V2ExtraInputInvalid("El costo unitario no puede ser negativo")
        fila.total_cost = fila.quantity * fila.unit_cost_snapshot
        self._session.add(fila)
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_QUOTATION_EXTRA_ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id), "name": fila.name_snapshot},
        )
        avisos += await refresh_pricing(self._session, quotation)
        return fila, avisos

    async def update_quotation_extra(
        self, quotation_id: int, extra_id: int, data: dict[str, Any], *, user: Any
    ) -> tuple[V2QuotationExtra, list[str]]:
        quotation = await self._draft(quotation_id)
        fila = await self._fila(quotation_id, extra_id)

        if "quantity" in data and data["quantity"] is not None:
            cantidad = Decimal(str(data["quantity"]))
            if cantidad < ZERO:
                raise V2ExtraInputInvalid("La cantidad no puede ser negativa")
            fila.quantity = cantidad
        if "unit_cost" in data:
            if data["unit_cost"] is None:
                # Volver al precio del maestro: el override se retira.
                maestro = await self._session.get(V2Extra, fila.v2_extra_id)
                if maestro is not None:
                    fila.unit_cost_snapshot = maestro.unit_cost
                fila.unit_cost_is_override = False
            else:
                precio = Decimal(str(data["unit_cost"]))
                if precio < ZERO:
                    raise V2ExtraInputInvalid("El costo unitario no puede ser negativo")
                fila.unit_cost_snapshot = precio
                fila.unit_cost_is_override = True
        if "description" in data:
            fila.description = data["description"]
        if "v2_quotation_product_id" in data:
            linea_id = data["v2_quotation_product_id"]
            if linea_id is not None:
                await self._linea(quotation_id, int(linea_id))
            fila.v2_quotation_product_id = int(linea_id) if linea_id is not None else None

        fila.total_cost = fila.quantity * fila.unit_cost_snapshot
        await self._session.flush()
        self._audit.record_action(
            entity_type=V2_QUOTATION_EXTRA_ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        avisos = await refresh_pricing(self._session, quotation)
        return fila, avisos

    async def delete_quotation_extra(self, quotation_id: int, extra_id: int, *, user: Any) -> None:
        quotation = await self._draft(quotation_id)
        fila = await self._fila(quotation_id, extra_id)
        await self._session.delete(fila)
        self._audit.record_action(
            entity_type=V2_QUOTATION_EXTRA_ENTITY,
            entity_id=str(extra_id),
            action=AuditAction.DELETE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        await self._session.flush()
        await refresh_pricing(self._session, quotation)

    # ------------------------------------------------------------------
    async def _fila(self, quotation_id: int, extra_id: int) -> V2QuotationExtra:
        fila = (
            await self._session.scalars(
                select(V2QuotationExtra).where(
                    V2QuotationExtra.id == extra_id,
                    V2QuotationExtra.v2_quotation_id == quotation_id,
                )
            )
        ).one_or_none()
        if fila is None:
            raise V2ExtraNotFoundError("Ese adicional no es de esta cotizacion")
        return fila

    async def _linea(self, quotation_id: int, linea_id: int) -> V2QuotationProduct:
        linea = (
            await self._session.scalars(
                select(V2QuotationProduct).where(
                    V2QuotationProduct.id == linea_id,
                    V2QuotationProduct.v2_quotation_id == quotation_id,
                )
            )
        ).one_or_none()
        if linea is None:
            raise V2ExtraInputInvalid("Esa linea no es de esta cotizacion")
        return linea

    async def _draft(self, quotation_id: int) -> V2Quotation:
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
        if quotation is None:
            raise V2ExtraNotFoundError("La cotizacion V2 no existe")
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2ExtraQuotationNotEditableError()
        return quotation
