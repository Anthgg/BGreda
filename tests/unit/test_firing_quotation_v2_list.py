"""Pruebas unitarias del listado de Solo Quema V2 (app.services.firing_quotation_v2)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.quoter_v2_lifecycle import V2EffectiveStatus
from app.models.firing_quotation_v2 import V2FiringQuotation
from app.models.quoter_v2 import V2QuotationStatus
from app.schemas.firing_quotation_v2 import V2FiringQuotationListItemOut
from app.services.firing_quotation_v2 import V2FiringQuotationService


def _crear_servicio(filas: list[V2FiringQuotation], total: int) -> V2FiringQuotationService:
    session = AsyncMock()
    # Mock para func.count()
    session.scalar.return_value = total

    # Mock para scalars().all()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = filas
    session.scalars.return_value = scalars_mock

    # Mock para session.get()
    session.get.return_value = None

    audit = MagicMock()
    servicio = V2FiringQuotationService(session=session, audit=audit)
    # db_now devuelve un instante fijo
    servicio.db_now = AsyncMock(return_value=datetime(2026, 9, 24, 12, 0, tzinfo=UTC))  # type: ignore[method-assign]
    return servicio


@pytest.mark.asyncio
async def test_list_quotations_con_cero_elementos() -> None:
    """La lista vacia devuelve 0 elementos sin error."""
    servicio = _crear_servicio(filas=[], total=0)
    items, total = await servicio.list_quotations(limit=10, offset=0)
    assert items == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_quotations_con_borrador_entrega_effective_status_serializable() -> None:
    """Un borrador debe entregar V2EffectiveStatus evaluado, no una corutina."""
    fila = V2FiringQuotation(
        id=1,
        code="Q-V2-2026-000001",
        status=V2QuotationStatus.DRAFT,
        name="Borrador de prueba",
        customer_name_snapshot="Cliente Prueba",
        currency_code_snapshot="PEN",
        total_amount=Decimal("150.00"),
        created_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        expires_at=None,
    )
    servicio = _crear_servicio(filas=[fila], total=1)

    items, total = await servicio.list_quotations(limit=10, offset=0)
    assert total == 1
    assert len(items) == 1

    cotizacion, estado_efectivo = items[0]
    assert cotizacion.id == 1

    # Verificacion critica: no debe ser una coroutine
    assert not hasattr(estado_efectivo, "__await__"), (
        f"effective_status es una coroutine sin await: {estado_efectivo!r}"
    )
    assert isinstance(estado_efectivo, V2EffectiveStatus)
    assert estado_efectivo == V2EffectiveStatus.DRAFT

    # Verificacion de serializacion con el esquema Pydantic de la ruta
    item_out = V2FiringQuotationListItemOut(
        id=cotizacion.id,
        code=cotizacion.code,
        status=cotizacion.status,
        effective_status=estado_efectivo,
        name=cotizacion.name,
        customer_name=cotizacion.customer_name_snapshot,
        currency_code=cotizacion.currency_code_snapshot,
        total_amount=cotizacion.total_amount,
        created_at=cotizacion.created_at,
        valid_until=None,
    )
    assert item_out.effective_status == V2EffectiveStatus.DRAFT
