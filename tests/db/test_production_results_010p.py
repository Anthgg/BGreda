"""Resultados físicos de producción 010P: alta única, stock y atomicidad."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import MovementType, StockBalance, StockMovement
from app.models.masters import Product, ProductCategory, ProductType
from app.models.production import ProductionOrder, ProductionOrderResult, ProductionOrderStatus
from app.models.quoter_v2 import V2QuotationProduct
from app.services.audit import AuditRecorder
from tests.db.test_production_v2_consumption import consumir, orden_con_existencia
from tests.db.test_production_v2_origin import ORDERS, h


async def _orden_lista(api: httpx.AsyncClient, csrf: str, *, suffix: str) -> dict[str, Any]:
    """Una orden V2 real con una línea a medida sin product_id."""
    datos = await orden_con_existencia(api, csrf)
    # La cotización reutiliza nombres fijos en sus maestros; cada caso corre
    # con la DB reiniciada por el fixture de tests/db.
    line_id = int(datos["lines"][0])
    start = await api.post(f"{ORDERS}/{datos['order_id']}/start", headers=h(csrf))
    assert start.status_code == 200, start.text
    consume = await consumir(
        api,
        csrf,
        datos["order_id"],
        product_id=datos["pasta_id"],
        quantity="1",
        key=f"w2-custom-body-{suffix}",
        kind="BODY",
        v2_quotation_product_id=line_id,
    )
    assert consume.status_code == 201, consume.text
    return {**datos, "line_id": line_id}


def _resultado(line_id: int, *, good: str = "30", scrap: str = "70") -> dict[str, Any]:
    return {
        "results": [
            {
                "line_ref": f"V2P:{line_id}",
                "good_quantity": good,
                "scrap_quantity": scrap,
                "scrap_reason": "Merma registrada" if Decimal(scrap) else None,
            }
        ]
    }


async def _custom_product(db: AsyncSession, line_id: int) -> Product | None:
    return await db.scalar(select(Product).where(Product.source_v2_quotation_product_id == line_id))


@pytest.mark.asyncio
async def test_complete_linea_custom_crea_un_producto_categoria_y_stock_30(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _orden_lista(api, admin_csrf, suffix="30")
    response = await api.post(
        f"{ORDERS}/{datos['order_id']}/complete",
        json=_resultado(datos["line_id"]),
        headers=h(admin_csrf),
    )

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert len(results) == 1
    assert Decimal(results[0]["started_quantity"]) == Decimal(100)
    assert Decimal(results[0]["good_quantity"]) == Decimal(30)
    assert Decimal(results[0]["scrap_quantity"]) == Decimal(70)
    product = await _custom_product(db_session, datos["line_id"])
    assert product is not None
    assert product.product_type is ProductType.FINISHED_PRODUCT
    assert product.source_v2_quotation_product_id == datos["line_id"]
    line = await db_session.get(V2QuotationProduct, datos["line_id"])
    assert line is not None and line.product_id is None
    category = await db_session.get(ProductCategory, product.product_category_id)
    assert category is not None
    assert (category.name, category.display_path) == (
        "Piezas personalizadas",
        "Piezas personalizadas",
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.source_v2_quotation_product_id == datos["line_id"])
        )
        == 1
    )
    movement = await db_session.scalar(
        select(StockMovement).where(
            StockMovement.production_order_id == datos["order_id"],
            StockMovement.movement_type == MovementType.PRODUCTION_IN,
        )
    )
    assert movement is not None
    assert movement.product_id == product.id
    assert movement.v2_quotation_id == datos["id"]
    assert movement.quantity == Decimal(30)
    balance = await db_session.scalar(
        select(StockBalance.quantity).where(
            StockBalance.product_id == product.id,
            StockBalance.location_id == datos["location_id"],
        )
    )
    assert balance == Decimal(30)

    # Reintentar el cierre devuelve el resultado original, sin otra alta ni movimiento.
    retry = await api.post(
        f"{ORDERS}/{datos['order_id']}/complete",
        json=_resultado(datos["line_id"]),
        headers=h(admin_csrf),
    )
    assert retry.status_code == 200, retry.text
    assert await _custom_product(db_session, datos["line_id"]) == product
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(
                StockMovement.production_order_id == datos["order_id"],
                StockMovement.movement_type == MovementType.PRODUCTION_IN,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_complete_custom_concurrente_no_duplica_producto_resultado_ni_stock(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    datos = await _orden_lista(api, admin_csrf, suffix="concurrente")
    payload = _resultado(datos["line_id"])
    responses = await asyncio.gather(
        *(
            api.post(
                f"{ORDERS}/{datos['order_id']}/complete",
                json=payload,
                headers=h(admin_csrf),
            )
            for _ in range(2)
        )
    )

    assert [response.status_code for response in responses] == [200, 200]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProductionOrderResult)
            .where(ProductionOrderResult.production_order_id == datos["order_id"])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.source_v2_quotation_product_id == datos["line_id"])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(
                StockMovement.production_order_id == datos["order_id"],
                StockMovement.movement_type == MovementType.PRODUCTION_IN,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_complete_custom_error_despues_de_flush_revierte_todo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datos = await _orden_lista(api, admin_csrf, suffix="atomicidad")
    order_id = int(datos["order_id"])

    def fail_after_flush(self: AuditRecorder, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fallo inyectado después del flush final")

    monkeypatch.setattr(AuditRecorder, "record_changes", fail_after_flush)
    with pytest.raises(RuntimeError, match="fallo inyectado"):
        await api.post(
            f"{ORDERS}/{order_id}/complete",
            json=_resultado(datos["line_id"]),
            headers=h(admin_csrf),
        )

    assert (
        await db_session.scalar(
            select(ProductionOrder.status).where(ProductionOrder.id == order_id)
        )
        == ProductionOrderStatus.STARTED
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProductionOrderResult)
            .where(ProductionOrderResult.production_order_id == order_id)
        )
        == 0
    )
    assert await _custom_product(db_session, datos["line_id"]) is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(
                StockMovement.production_order_id == order_id,
                StockMovement.movement_type == MovementType.PRODUCTION_IN,
            )
        )
        == 0
    )
