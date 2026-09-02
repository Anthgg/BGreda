"""Fase 009I — la frontera: que operaciones NO pueden tocar el inventario.

Es la prueba mas aburrida de la fase y la que mas vale. Todo el flujo comercial
—guardar, confirmar, cobrar, cambiar dias, crear la orden— pasa por aqui, y de
todo ese recorrido solo hay UN punto en el que un gramo puede moverse.

Escrito al reves: si alguien anade manana un descuento de material a confirmar
o a cobrar, esta prueba se pone roja antes de que llegue a produccion.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from tests.db.test_production_orders_api import (
    ORDERS,
    confirmada_y_pagada,
    confirmar,
    crear_orden,
    escenario,
)
from tests.db.test_quotation_builder_api import head

BUILDER = "/api/v1/quotation-builder"


async def _foto(db_session: AsyncSession) -> tuple[int, list[tuple[int, int, Decimal]]]:
    """Movimientos y saldos, exactamente como estan ahora mismo."""
    db_session.expire_all()
    movimientos = int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
    saldos = [
        (fila.product_id, fila.location_id, fila.quantity)
        for fila in (await db_session.execute(select(StockBalance).order_by(StockBalance.id)))
        .scalars()
        .all()
    ]
    return movimientos, saldos


@pytest.mark.asyncio
async def test_solo_arrancar_mueve_inventario_en_todo_el_flujo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DRAFT / CONFIRM / MARK_PAID / DAY_ADJUSTMENT / CREATE_ORDER: 0 mutaciones.

    Se recorre el flujo entero con la foto del inventario delante y se exige
    que no cambie en ningun paso. Solo el ultimo, arrancar, tiene permiso.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_frontera")
    referencia = await _foto(db_session)

    # ---- 1. Guardar el borrador ------------------------------------------
    borrador = datos["quotation"]
    actualizado = await api.put(
        f"{BUILDER}/{borrador['id']}",
        json={
            "name": "Pedido reeditado",
            "customer_id": borrador["customer_id"],
            "kiln_id": borrador["kiln_id"],
            "expected_updated_at": borrador["updated_at"],
            "items": [
                {
                    "product_id": datos["producto"]["id"],
                    "quantity": 100,
                    "dimensions": {"width": "24", "length": "24", "height": "8"},
                    "recipe_id": datos["receta"]["id"],
                    "recipe_version_id": datos["receta"]["current_version"]["id"],
                    "material_grams_per_piece": "10",
                    # 2. Y de paso se mueven los dias, que en 009G.1 cambian el
                    #    costo operativo. Cambiar dinero no es mover materia.
                    "days_adjustment": 3,
                    "waiting_days": 2,
                    "other_costs": [],
                    "markup_percent": "100",
                    "commercial_sale_unit_price": "8.50",
                    "sort_order": 0,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert actualizado.status_code == 200, actualizado.text
    assert await _foto(db_session) == referencia, "guardar el borrador movio inventario"

    # ---- 3. Confirmar ----------------------------------------------------
    confirmada = await confirmada_y_pagada(api, admin_csrf, actualizado.json())
    assert await _foto(db_session) == referencia, "confirmar movio inventario"

    # ---- 4. Cobrar -------------------------------------------------------
    pagada = await api.post(f"{BUILDER}/{confirmada['id']}/mark-paid", headers=head(admin_csrf))
    assert pagada.status_code == 200, pagada.text
    assert await _foto(db_session) == referencia, "cobrar movio inventario"

    # ---- 5. Crear la orden ------------------------------------------------
    orden = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert orden.status_code == 201, orden.text
    assert await _foto(db_session) == referencia, "crear la orden movio inventario"

    # ---- 6. Leer, listar y pedir el documento -----------------------------
    assert (await api.get(f"{ORDERS}/{orden.json()['id']}")).status_code == 200
    assert (await api.get(ORDERS)).status_code == 200
    documento = await api.get(f"{ORDERS}/{orden.json()['id']}/document")
    assert documento.status_code == 200, documento.text
    assert await _foto(db_session) == referencia, "leer la orden movio inventario"

    # ---- 7. Y ahora si ----------------------------------------------------
    arrancada = await api.post(f"{ORDERS}/{orden.json()['id']}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text
    assert await _foto(db_session) != referencia, (
        "arrancar es el unico paso que DEBE mover inventario, y no lo movio"
    )


@pytest.mark.asyncio
async def test_evaluar_la_disponibilidad_no_escribe_nada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """READINESS es de solo lectura, y se puede pedir cuantas veces se quiera.

    La interfaz la consulta cada vez que abre la orden. Si consultarla tuviera
    consecuencias, mirar la pantalla cambiaria el almacen.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_readonly")
    confirmada = await confirmar(api, admin_csrf, datos["quotation"])
    orden = await crear_orden(
        api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
    )
    assert orden.status_code == 201, orden.text
    referencia = await _foto(db_session)

    for _ in range(5):
        leida = await api.get(f"{ORDERS}/{orden.json()['id']}")
        assert leida.status_code == 200, leida.text
        assert leida.json()["readiness"]["ready"] is True

    assert await _foto(db_session) == referencia
