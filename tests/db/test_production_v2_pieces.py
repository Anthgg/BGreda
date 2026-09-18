"""Fase 010I, bloque E — lo que el taller lee de una orden V2, contra PostgreSQL.

Una orden V2 no tiene lineas propias: sus piezas son las de la cotizacion V2
confirmada, que solo ADMIN puede leer porque lleva precios. La orden expone a
cambio una vista OPERACIONAL —que fabricar, cuantas, de que medidas, con que
material planificado— sin un solo importe. Lo que se fija:

- ADMIN y OPERATOR la reciben; el OPERATOR sin poder leer la cotizacion;
- multiproducto, en el orden de la cotizacion, con los datos congelados;
- ninguna clave economica en la respuesta, comprobado contra el modelo real;
- cliente y resumen en el listado, sin una consulta extra por fila;
- Legacy y muestras siguen como estaban; leer no toca cotizacion ni stock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.inventory import StockMovement
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate
from tests.db.test_production_v2_origin import ORDERS, crear_orden, otra_enviada_a_produccion
from tests.db.test_quoter_v2_lifecycle_api import cotizacion_completa, emitir, h

V2 = "/api/v1/quotations-v2"
LOCATIONS = "/api/v1/inventory/locations"

#: El separador del resumen: el signo de multiplicar, escapado como en el servicio.
POR = f" {chr(0xD7)} "

#: Fragmentos que delatan un importe. Se comprueban contra TODAS las claves de
#: la respuesta, a cualquier profundidad.
ECONOMICOS = (
    "cost",
    "price",
    "factor",
    "margin",
    "profit",
    "subtotal",
    "igv",
    "tax",
    "amount",
    "exchange",
    "fx_",
    "currency",
    "total_price",
)


# ---------------------------------------------------------------------------
# Apoyo
# ---------------------------------------------------------------------------
async def orden_multiproducto(api: httpx.AsyncClient, csrf: str) -> dict[str, Any]:
    """Una CTZ V2 de dos piezas, emitida, enviada a produccion y con su orden."""
    datos = await cotizacion_completa(
        api, csrf, productos=(("Taza de cafe", 20), ("Plato hondo", 5))
    )
    await emitir(api, csrf, datos["id"])
    enviada = await api.post(f"{V2}/{datos['id']}/send-to-production", headers=h(csrf))
    assert enviada.status_code in (200, 201), enviada.text
    almacen = await api.post(
        LOCATIONS, json={"name": f"Taller piezas {datos['id']}"}, headers=h(csrf)
    )
    assert almacen.status_code == 201, almacen.text
    location_id = int(almacen.json()["id"])
    orden = await crear_orden(api, csrf, v2_quotation_id=datos["id"], location_id=location_id)
    assert orden.status_code == 201, orden.text
    return {**datos, "location_id": location_id, "order_id": int(orden.json()["id"])}


def claves(valor: Any) -> set[str]:
    """Todas las claves de un JSON, a cualquier profundidad."""
    if isinstance(valor, dict):
        return set(valor) | {k for v in valor.values() for k in claves(v)}
    if isinstance(valor, list):
        return {k for v in valor for k in claves(v)}
    return set()


@contextmanager
def contar_consultas(engine: AsyncEngine) -> Iterator[list[str]]:
    sentencias: list[str] = []

    def _anotar(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        sentencias.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _anotar)
    try:
        yield sentencias
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _anotar)


# ---------------------------------------------------------------------------
# Las piezas de la ficha
# ---------------------------------------------------------------------------
class TestPiezasDeLaFicha:
    async def test_el_admin_lee_las_piezas_congeladas_en_su_orden(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_multiproducto(api, admin_csrf)

        r = await api.get(f"{ORDERS}/{datos['order_id']}")

        assert r.status_code == 200, r.text
        piezas = r.json()["v2_pieces"]
        assert [p["id"] for p in piezas] == datos["lines"]
        taza, plato = piezas
        assert (taza["product_name"], taza["quantity"]) == ("Taza de cafe", 20)
        assert (plato["product_name"], plato["quantity"]) == ("Plato hondo", 5)
        for pieza in piezas:
            assert Decimal(pieza["length_cm"]) == Decimal("20")
            assert Decimal(pieza["width_cm"]) == Decimal("20")
            assert Decimal(pieza["height_cm"]) == Decimal("4")
            assert pieza["body_material_id"] == datos["pasta_id"]
            assert pieza["body_material_name"]
            assert Decimal(pieza["body_unit_weight"]) == Decimal("450")
            assert pieza["requires_glaze"] is False
            assert pieza["glaze_material_id"] is None
            assert Decimal(pieza["glaze_total_weight"]) == 0
        assert Decimal(taza["body_total_weight"]) == Decimal(450 * 20)
        assert Decimal(plato["body_total_weight"]) == Decimal(450 * 5)

        # Lo mismo que dice la cotizacion congelada, campo a campo.
        db_session.expire_all()
        filas = (
            await db_session.scalars(
                select(V2QuotationProduct)
                .where(V2QuotationProduct.v2_quotation_id == datos["id"])
                .order_by(V2QuotationProduct.sort_order, V2QuotationProduct.id)
            )
        ).all()
        for pieza, fila in zip(piezas, filas, strict=True):
            assert pieza["body_material_name"] == fila.body_material_name_snapshot
            assert pieza["body_uom"] == fila.body_uom_snapshot
            assert Decimal(pieza["body_total_weight"]) == fila.body_total_weight

    async def test_el_operador_lee_las_piezas_sin_poder_leer_la_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Es a proposito: la orden le da lo operacional; la cotizacion, no."""
        datos = await orden_multiproducto(api, admin_csrf)
        await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        cotizacion = await api.get(f"{V2}/{datos['id']}")
        orden = await api.get(f"{ORDERS}/{datos['order_id']}")

        assert cotizacion.status_code == 403, cotizacion.text
        assert orden.status_code == 200, orden.text
        assert [p["product_name"] for p in orden.json()["v2_pieces"]] == [
            "Taza de cafe",
            "Plato hondo",
        ]
        assert orden.json()["customer_name"]

    async def test_el_esmalte_planificado_se_lee_de_la_pieza(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_multiproducto(api, admin_csrf)
        # Directo en la base, como en el bloque C: la V2 emitida no se edita
        # por la API, y lo que se prueba es la lectura.
        await db_session.execute(
            update(V2QuotationProduct)
            .where(V2QuotationProduct.id == datos["lines"][1])
            .values(
                requires_glaze=True,
                glaze_material_id=datos["pasta_id"],
                glaze_material_name_snapshot="Esmalte blanco",
                glaze_is_reference=True,
                glaze_total_weight=Decimal("125"),
            )
        )
        await db_session.commit()

        piezas = (await api.get(f"{ORDERS}/{datos['order_id']}")).json()["v2_pieces"]

        taza, plato = piezas
        assert taza["requires_glaze"] is False and taza["glaze_material_name"] is None
        assert plato["requires_glaze"] is True
        assert plato["glaze_material_id"] == datos["pasta_id"]
        assert plato["glaze_material_name"] == "Esmalte blanco"
        assert plato["glaze_is_reference"] is True
        assert Decimal(plato["glaze_total_weight"]) == Decimal("125")


# ---------------------------------------------------------------------------
# Ni un importe
# ---------------------------------------------------------------------------
class TestSinImportes:
    async def test_la_ficha_y_el_listado_no_llevan_ninguna_clave_economica(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await orden_multiproducto(api, admin_csrf)

        ficha = (await api.get(f"{ORDERS}/{datos['order_id']}")).json()
        listado = (await api.get(ORDERS, params={"v2_quotation_id": datos["id"]})).json()

        for respuesta in (ficha, listado):
            sospechosas = sorted(
                k for k in claves(respuesta) if any(t in k.lower() for t in ECONOMICOS)
            )
            assert sospechosas == [], sospechosas

    def test_ninguna_columna_economica_de_la_pieza_llega_al_esquema(self) -> None:
        """Contra los nombres REALES del modelo, no contra una lista escrita a mano.

        Si manana la linea V2 gana otra columna de costo, esta prueba la ve.
        """
        from app.schemas.production import V2ProductionPieceOut

        columnas = {c.name for c in V2QuotationProduct.__table__.columns}
        economicas = {c for c in columnas if any(t in c for t in ECONOMICOS)}
        # La prueba solo vale si de verdad hay columnas economicas que excluir.
        assert {
            "body_cost",
            "glaze_cost",
            "firing_commercial_cost",
            "firing_gas_cost",
            "direct_cost",
            "allocated_real_cost",
            "line_price",
        } <= economicas, sorted(economicas)
        assert not (set(V2ProductionPieceOut.model_fields) & economicas)
        assert not [
            campo
            for campo in V2ProductionPieceOut.model_fields
            if any(t in campo for t in ECONOMICOS)
        ]


# ---------------------------------------------------------------------------
# El listado
# ---------------------------------------------------------------------------
class TestListado:
    async def test_cliente_y_resumen_de_piezas(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_multiproducto(api, admin_csrf)
        db_session.expire_all()
        cliente = await db_session.scalar(
            select(V2Quotation.customer_name_snapshot).where(V2Quotation.id == datos["id"])
        )
        assert cliente

        r = await api.get(ORDERS, params={"v2_quotation_id": datos["id"]})

        [fila] = r.json()["items"]
        assert fila["customer_name"] == cliente
        assert fila["pieces_summary"] == f"20{POR}Taza de cafe, 5{POR}Plato hondo"
        ficha = (await api.get(f"{ORDERS}/{datos['order_id']}")).json()
        assert ficha["customer_name"] == cliente
        assert ficha["pieces_summary"] == fila["pieces_summary"]

    async def test_el_resumen_se_corta_a_cuatro_piezas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await cotizacion_completa(
            api,
            admin_csrf,
            productos=tuple((f"Pieza {i}", i) for i in range(1, 7)),
        )
        await emitir(api, admin_csrf, datos["id"])
        await api.post(f"{V2}/{datos['id']}/send-to-production", headers=h(admin_csrf))
        almacen = await api.post(LOCATIONS, json={"name": "Taller resumen"}, headers=h(admin_csrf))
        orden = await crear_orden(
            api, admin_csrf, v2_quotation_id=datos["id"], location_id=int(almacen.json()["id"])
        )
        assert orden.status_code == 201, orden.text

        fila = (await api.get(ORDERS, params={"v2_quotation_id": datos["id"]})).json()["items"][0]

        assert fila["pieces_summary"] == (
            f"1{POR}Pieza 1, 2{POR}Pieza 2, 3{POR}Pieza 3, 4{POR}Pieza 4 +2 más"
        )

    async def test_el_listado_no_hace_una_consulta_por_fila(
        self,
        api: httpx.AsyncClient,
        admin_csrf: str,
        db_engine: AsyncEngine,
    ) -> None:
        """BLOCKER del bloque E: tres filas V2 cuestan lo mismo que una."""
        primera = await orden_multiproducto(api, admin_csrf)
        for _ in range(2):
            otra = await otra_enviada_a_produccion(api, admin_csrf, primera)
            creada = await crear_orden(
                api, admin_csrf, v2_quotation_id=otra["id"], location_id=otra["location_id"]
            )
            assert creada.status_code == 201, creada.text

        with contar_consultas(db_engine) as una:
            r1 = await api.get(ORDERS, params={"limit": 1})
        with contar_consultas(db_engine) as tres:
            r3 = await api.get(ORDERS, params={"limit": 3})

        assert len(r1.json()["items"]) == 1
        assert len(r3.json()["items"]) == 3
        assert all(f["origin_type"] == "V2_QUOTATION" for f in r3.json()["items"])
        assert all(f["pieces_summary"] and f["customer_name"] for f in r3.json()["items"])
        assert len(tres) == len(una), (len(una), len(tres))


# ---------------------------------------------------------------------------
# Legacy, muestras y efectos
# ---------------------------------------------------------------------------
class TestCompatibilidad:
    async def test_una_orden_legacy_no_tiene_piezas_v2_y_si_su_cliente(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        from tests.db.test_production_orders_api import confirmada_y_pagada, escenario
        from tests.db.test_production_orders_api import crear_orden as crear_orden_legacy

        datos = await escenario(api, admin_csrf, db_session, suffix="_piezas_legacy")
        confirmada = await confirmada_y_pagada(api, admin_csrf, datos["quotation"])
        orden = await crear_orden_legacy(
            api, admin_csrf, quotation_id=confirmada["id"], location_id=datos["location_id"]
        )
        assert orden.status_code == 201, orden.text

        ficha = (await api.get(f"{ORDERS}/{orden.json()['id']}")).json()

        assert ficha["origin_type"] == "QUOTATION"
        assert ficha["v2_pieces"] == []
        assert ficha["lines"], "la Legacy conserva sus lineas"
        assert ficha["customer_name"] == ficha["quotation_customer_name"]
        esperado = ", ".join(
            f"{linea['quantity']}{POR}{linea['product_name']}" for linea in ficha["lines"][:4]
        )
        assert ficha["pieces_summary"].startswith(esperado)

    async def test_una_orden_de_muestra_sigue_sin_cliente_ni_piezas_v2(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        from tests.db.test_production_orders_api import crear_ubicacion
        from tests.db.test_prototype_production_order import _cpr_confirmada
        from tests.db.test_prototype_quotations import cobrar

        escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_piezas_muestra")
        almacen = await crear_ubicacion(api, admin_csrf, "Almacen piezas muestra")
        pagada = await cobrar(
            api, admin_csrf, escenario["documento"]["id"], stock_location_id=almacen
        )

        ficha = (await api.get(f"{ORDERS}/{pagada.json()['production_order_id']}")).json()

        assert ficha["origin_type"] == "PROTOTYPE"
        assert ficha["v2_pieces"] == []
        assert ficha["customer_name"] is None
        assert ficha["lines"], "la muestra conserva su linea"

    async def test_leer_la_orden_no_toca_cotizacion_ni_inventario(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await orden_multiproducto(api, admin_csrf)
        cotizacion = (await api.get(f"{V2}/{datos['id']}")).json()
        db_session.expire_all()
        movimientos = await db_session.scalar(select(func.count()).select_from(StockMovement))

        for _ in range(2):
            assert (await api.get(f"{ORDERS}/{datos['order_id']}")).status_code == 200
            assert (await api.get(ORDERS)).status_code == 200

        assert (await api.get(f"{V2}/{datos['id']}")).json() == cotizacion
        db_session.expire_all()
        assert await db_session.scalar(select(func.count()).select_from(StockMovement)) == (
            movimientos
        )
