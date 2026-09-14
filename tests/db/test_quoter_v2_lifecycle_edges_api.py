"""Fase 010H — los casos limite del ciclo de vida V2, contra PostgreSQL.

Complementa `test_quoter_v2_lifecycle_api.py` con lo que la revision adversarial
pidio expresamente: vigencia de un dia por la API, maestros de catalogo dados
de baja (producto, tecnica, esmalte, horno), un rango de factor que cambia
entre la emision y la duplicacion, y dos decisiones contrarias a la vez
(anular y pasar a produccion).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quoter_v2_excel_smoke import V2_SETTINGS
from tests.db.test_quoter_v2_lifecycle_api import (
    V2,
    cotizacion_completa,
    emitir,
    h,
    vencer,
)

LIMA = timedelta(hours=-5)


async def _ajustes(api: httpx.AsyncClient, csrf: str, **cambios: Any) -> None:
    actual = (await api.get(V2_SETTINGS)).json()["settings"]
    r = await api.put(
        V2_SETTINGS, json={"expected_version": actual["version"], **cambios}, headers=h(csrf)
    )
    assert r.status_code == 200, r.text


async def _producto_de_catalogo(api: httpx.AsyncClient, csrf: str, nombre: str) -> int:
    categoria = await api.post(
        "/api/v1/categories", json={"name": f"Cat {nombre}", "parent_id": None}, headers=h(csrf)
    )
    assert categoria.status_code == 201, categoria.text
    producto = await api.post(
        "/api/v1/products",
        json={
            "name": nombre,
            "product_type": "FINISHED_PRODUCT",
            "product_category_id": int(categoria.json()["id"]),
            "base_uom_code": "NIU",
            "sellable": True,
        },
        headers=h(csrf),
    )
    assert producto.status_code == 201, producto.text
    return int(producto.json()["id"])


async def _esmalte(api: httpx.AsyncClient, csrf: str, nombre: str, costo: str) -> int:
    categoria = await api.post(
        "/api/v1/categories", json={"name": f"Cat {nombre}", "parent_id": None}, headers=h(csrf)
    )
    assert categoria.status_code == 201, categoria.text
    producto = await api.post(
        "/api/v1/products",
        json={
            "name": nombre,
            "product_type": "RAW_MATERIAL",
            "product_category_id": int(categoria.json()["id"]),
            "base_uom_code": "g",
            "purchasable": True,
        },
        headers=h(csrf),
    )
    assert producto.status_code == 201, producto.text
    pid = int(producto.json()["id"])
    alta = await api.put(
        f"/api/v1/quoter-v2/materials/{pid}",
        json={
            "material_kind": "GLAZE",
            "origin": "PURCHASE",
            "purchase_quantity": "1000",
            "purchase_cost": costo,
            "transport_cost": "0",
        },
        headers=h(csrf),
    )
    assert alta.status_code == 200, alta.text
    return pid


class TestVigencia:
    async def test_vigencia_de_un_dia_por_la_api(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Configurada a 1 dia: vale hoy y manana (calendario de Lima), vence pasado manana."""
        datos = await cotizacion_completa(api, admin_csrf)
        await _ajustes(api, admin_csrf, quotation_validity_days=1)
        # El borrador ya habia congelado 20 al crearse: la vigencia es de ESA cotizacion.
        emitida = await emitir(api, admin_csrf, datos["id"])
        assert emitida["validity_days"] == 20

        otra = await api.post(
            V2,
            json={"name": "Un dia", "customer_id": datos["customer_id"]},
            headers=h(admin_csrf),
        )
        assert otra.status_code == 201, otra.text
        assert otra.json()["validity_days"] == 1

    async def test_emitida_valida_hasta_es_dia_de_lima(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        emitida = await emitir(api, admin_csrf, datos["id"])
        emision = datetime.fromisoformat(emitida["issued_at"])
        dia_lima = emision.astimezone(UTC).replace(tzinfo=None) + LIMA
        esperado = dia_lima.date() + timedelta(days=emitida["validity_days"])
        assert date.fromisoformat(emitida["valid_until"]) == esperado
        vence = datetime.fromisoformat(emitida["expires_at"]).astimezone(UTC)
        assert (
            vence == datetime.combine(esperado + timedelta(days=1), datetime.min.time(), UTC) - LIMA
        )


class TestMaestrosDeBaja:
    async def test_producto_tecnica_esmalte_y_horno_de_baja(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        plato = await _producto_de_catalogo(api, admin_csrf, "Plato catalogo 010H")
        esmalte = await _esmalte(api, admin_csrf, "Esmalte cobalto 010H", "200")

        con_catalogo = await api.post(
            f"{V2}/{qid}/products",
            json={
                "product_id": plato,
                "quantity": 10,
                "length_cm": "20",
                "width_cm": "20",
                "height_cm": "3",
                "body_material_id": datos["pasta_id"],
                "body_unit_weight": "300",
            },
            headers=h(admin_csrf),
        )
        assert con_catalogo.status_code == 201, con_catalogo.text
        con_esmalte = await api.post(
            f"{V2}/{qid}/products",
            json={
                "product_name": "Taza esmaltada",
                "quantity": 10,
                "length_cm": "8",
                "width_cm": "8",
                "height_cm": "9",
                "body_material_id": datos["pasta_id"],
                "body_unit_weight": "200",
                "requires_glaze": True,
                "glaze_material_id": esmalte,
            },
            headers=h(admin_csrf),
        )
        assert con_esmalte.status_code == 201, con_esmalte.text

        await emitir(api, admin_csrf, qid)
        await vencer(db_session, qid)

        for sql, ident in (
            ("UPDATE products SET active = false WHERE id = :id", plato),
            ("UPDATE products SET active = false WHERE id = :id", esmalte),
            ("UPDATE v2_techniques SET active = false WHERE id = :id", datos["technique_id"]),
            ("UPDATE kilns SET active = false WHERE id = :id", datos["kiln_id"]),
        ):
            await db_session.execute(text(sql), {"id": ident})
        await db_session.commit()

        dup = await api.post(f"{V2}/{qid}/duplicate", headers=h(admin_csrf))
        assert dup.status_code == 201, dup.text
        avisos = {aviso["code"] for aviso in dup.json()["warnings"]}
        assert "V2_DUPLICATE_PRODUCT_UNAVAILABLE" in avisos
        assert "V2_DUPLICATE_GLAZE_MATERIAL_UNAVAILABLE" in avisos
        assert "V2_DUPLICATE_LABOR_UNAVAILABLE" in avisos
        assert "V2_DUPLICATE_KILN_UNAVAILABLE" in avisos

        nueva = dup.json()["quotation"]
        lineas = (await api.get(f"{V2}/{nueva['id']}/products")).json()["items"]
        nombres = [linea["product_name"] for linea in lineas]
        assert "Plato catalogo 010H" not in nombres, "un producto de baja no se copia"
        assert "Taza esmaltada" in nombres
        taza = next(linea for linea in lineas if linea["product_name"] == "Taza esmaltada")
        assert taza["requires_glaze"] is True
        assert taza["glaze_material_id"] != esmalte, "el esmalte de baja no vuelve a elegirse"

        quema = (await api.get(f"{V2}/{nueva['id']}/firing")).json()
        assert quema["kiln_id"] != datos["kiln_id"] or quema["kiln_id"] is None


class TestFactor:
    async def test_duplicar_usa_el_rango_y_el_default_de_hoy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        antigua = await emitir(api, admin_csrf, qid)
        assert Decimal(antigua["commercial_factor_max"]) == Decimal(3)
        await vencer(db_session, qid)

        await _ajustes(
            api,
            admin_csrf,
            commercial_factor_min="2",
            commercial_factor_default="3.5",
            commercial_factor_max="4",
        )
        dup = await api.post(f"{V2}/{qid}/duplicate", headers=h(admin_csrf))
        assert dup.status_code == 201, dup.text
        nueva = dup.json()["quotation"]
        assert Decimal(nueva["commercial_factor"]) == Decimal("3.5")
        assert Decimal(nueva["commercial_factor_max"]) == Decimal(4)

        vieja = (await api.get(f"{V2}/{qid}")).json()
        assert Decimal(vieja["commercial_factor"]) == Decimal(antigua["commercial_factor"])
        assert Decimal(vieja["commercial_factor_max"]) == Decimal(3)


class TestDecisionesContrarias:
    async def test_anular_y_enviar_a_la_vez_solo_gana_una(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        await emitir(api, admin_csrf, qid)

        anular, enviar = await asyncio.gather(
            api.post(f"{V2}/{qid}/cancel", json={}, headers=h(admin_csrf)),
            api.post(f"{V2}/{qid}/send-to-production", headers=h(admin_csrf)),
        )
        exitos = [r for r in (anular, enviar) if r.status_code in (200, 201)]
        assert len(exitos) == 1, (anular.text, enviar.text)

        fila = (
            await db_session.execute(
                text(
                    "SELECT q.status, count(p.id) AS puentes FROM v2_quotations q"
                    " LEFT JOIN v2_production_handoffs p ON p.v2_quotation_id = q.id"
                    " WHERE q.id = :id GROUP BY q.status"
                ),
                {"id": qid},
            )
        ).one()
        assert (fila.status == "CANCELLED") == (fila.puentes == 0)

    async def test_confirmar_y_anular_a_la_vez_deja_un_estado_coherente(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        resumen = (await api.get(f"{V2}/{qid}/confirmation-preview")).json()
        confirmar, anular = await asyncio.gather(
            api.post(
                f"{V2}/{qid}/confirm",
                json={"expected_fingerprint": resumen["fingerprint"]},
                headers=h(admin_csrf),
            ),
            api.post(f"{V2}/{qid}/cancel", json={}, headers=h(admin_csrf)),
        )
        assert anular.status_code == 200, anular.text
        final = (await api.get(f"{V2}/{qid}")).json()
        assert final["status"] == "CANCELLED"
        # Si la emision gano la carrera, la anulada conserva su emision; si perdio,
        # la emision se rechazo porque ya estaba anulada.
        if confirmar.status_code == 200:
            assert final["issued_at"] is not None
        else:
            assert confirmar.status_code == 409
            assert final["issued_at"] is None
