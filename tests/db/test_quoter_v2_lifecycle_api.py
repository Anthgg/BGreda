"""Fase 010H — emitir, vencer, duplicar, anular y pasar a produccion, contra PostgreSQL.

Cada prueba recorre la API que usa la pantalla. Las que hablan de carreras
lanzan las peticiones A LA VEZ: comprobar el doble clic en serie solo prueba
que la segunda peticion lee la primera, no que la base impida dos emisiones.
"""

from __future__ import annotations

import asyncio
import io
import re
from decimal import Decimal
from typing import Any

import httpx
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quoter_v2_excel_smoke import (
    COMMERCIAL,
    V2_SETTINGS,
    preparar_configuracion,
    preparar_maestros,
)

V2 = "/api/v1/quotations-v2"
PARTNERS = "/api/v1/partners"


# ---------------------------------------------------------------------------
# Apoyo
# ---------------------------------------------------------------------------
def h(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def cliente(api: httpx.AsyncClient, csrf: str, nombre: str = "Cerámicas Andinas SAC") -> int:
    r = await api.post(
        PARTNERS,
        json={
            "name": nombre,
            "role": "CLIENT",
            "document_type": "RUC",
            "document_number": "20600000001",
            "address": "Jr. Barro 456, Lima",
        },
        headers=h(csrf),
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def cotizacion_completa(
    api: httpx.AsyncClient,
    csrf: str,
    *,
    currency: str | None = None,
    productos: tuple[tuple[str, int], ...] = (("Plato hondo", 100),),
) -> dict[str, Any]:
    """Una cotizacion lista para emitir, armada por las rutas del asistente."""
    kiln_id = await preparar_configuracion(api, csrf)
    pasta_id, worker_id, technique_id = await preparar_maestros(api, csrf)
    customer_id = await cliente(api, csrf)

    payload: dict[str, Any] = {
        "name": "Pedido de prueba 010H",
        "customer_id": customer_id,
        "client_notes": "Entrega en taller.",
    }
    if currency:
        payload["currency_code"] = currency
    creada = await api.post(V2, json=payload, headers=h(csrf))
    assert creada.status_code == 201, creada.text
    qid = int(creada.json()["id"])

    lineas: list[int] = []
    for nombre, cantidad in productos:
        r = await api.post(
            f"{V2}/{qid}/products",
            json={
                "product_name": nombre,
                "quantity": cantidad,
                "length_cm": "20",
                "width_cm": "20",
                "height_cm": "4",
                "body_material_id": pasta_id,
                "body_unit_weight": "450",
                "client_observation": f"Observacion de {nombre}",
            },
            headers=h(csrf),
        )
        assert r.status_code == 201, r.text
        lineas.append(int(r.json()["id"]))

    tarea = await api.post(
        f"{V2}/{qid}/labor",
        json={
            "v2_quotation_product_id": lineas[0],
            "worker_id": worker_id,
            "technique_id": technique_id,
            "quantity": str(productos[0][1]),
        },
        headers=h(csrf),
    )
    assert tarea.status_code == 201, tarea.text
    plan = await api.put(f"{V2}/{qid}/planning", json={"effective_work_days": 2}, headers=h(csrf))
    assert plan.status_code == 200, plan.text

    return {
        "id": qid,
        "kiln_id": kiln_id,
        "pasta_id": pasta_id,
        "worker_id": worker_id,
        "technique_id": technique_id,
        "customer_id": customer_id,
        "lines": lineas,
    }


async def preview(api: httpx.AsyncClient, qid: int) -> dict[str, Any]:
    r = await api.get(f"{V2}/{qid}/confirmation-preview")
    assert r.status_code == 200, r.text
    return dict(r.json())


async def emitir(api: httpx.AsyncClient, csrf: str, qid: int) -> dict[str, Any]:
    resumen = await preview(api, qid)
    assert resumen["can_confirm"], resumen["blockers"]
    r = await api.post(
        f"{V2}/{qid}/confirm",
        json={"expected_fingerprint": resumen["fingerprint"]},
        headers=h(csrf),
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def vencer(db: AsyncSession, qid: int, dias: int = 40) -> None:
    """Mueve la emision al pasado sin tocar ni una cifra."""
    await db.execute(
        text(
            "UPDATE v2_quotations SET"
            " issued_at = issued_at - make_interval(days => :d),"
            " valid_until = valid_until - :d,"
            " expires_at = expires_at - make_interval(days => :d)"
            " WHERE id = :id"
        ),
        {"d": dias, "id": qid},
    )
    await db.commit()


async def pdf_texto(api: httpx.AsyncClient, qid: int) -> str:
    r = await api.get(f"{V2}/{qid}/pdf")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "no-store"
    return " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(r.content)).pages)


def compacto(texto: str) -> str:
    """Sin espacios ni mayusculas: `pypdf` reparte los blancos como quiere."""
    return re.sub(r"\s+", "", texto).lower()


# ---------------------------------------------------------------------------
# CASO 1: borrador -> confirmar -> PDF
# ---------------------------------------------------------------------------
class TestEmitir:
    async def test_borrador_confirmar_pdf(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]

        borrador = (await api.get(f"{V2}/{qid}")).json()
        assert borrador["effective_status"] == "DRAFT"
        assert borrador["issued_at"] is None

        emitida = await emitir(api, admin_csrf, qid)
        assert emitida["status"] == "CONFIRMED"
        assert emitida["effective_status"] == "CONFIRMED"
        assert emitida["issued_at"] and emitida["expires_at"] and emitida["valid_until"]
        assert emitida["validity_days"] == 20

        texto = compacto(await pdf_texto(api, qid))
        assert compacto(emitida["code"]) in texto
        assert "andinassac" in texto
        assert "platohondo" in texto
        assert "igv" in texto and "total" in texto
        assert "válidahasta" in texto or "validahasta" in texto
        for prohibido in (
            "costoreal",
            "costodeproducción",
            "gasreal",
            "ganancia",
            "margen",
            "tarifaporhora",
            "factor",
            "rendimiento",
            "stock",
        ):
            assert prohibido not in texto, prohibido

    async def test_un_borrador_no_tiene_pdf(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        r = await api.get(f"{V2}/{datos['id']}/pdf")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "V2_QUOTATION_PDF_DRAFT_BLOCKED"

    async def test_incompleta_no_se_emite(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        creada = await api.post(V2, json={"name": "Vacia"}, headers=h(admin_csrf))
        qid = int(creada.json()["id"])
        resumen = await preview(api, qid)
        assert not resumen["can_confirm"]
        codigos = {b["code"] for b in resumen["blockers"]}
        assert {"V2_CONFIRM_CUSTOMER_REQUIRED", "V2_CONFIRM_NO_LINES"} <= codigos

        r = await api.post(
            f"{V2}/{qid}/confirm",
            json={"expected_fingerprint": resumen["fingerprint"]},
            headers=h(admin_csrf),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "V2_QUOTATION_INCOMPLETE"
        assert (await api.get(f"{V2}/{qid}")).json()["status"] == "DRAFT"

    async def test_emitida_queda_congelada(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        await emitir(api, admin_csrf, qid)

        intentos = (
            api.put(f"{V2}/{qid}", json={"name": "otro"}, headers=h(admin_csrf)),
            api.put(
                f"{V2}/{qid}/pricing", json={"commercial_factor": "2.5"}, headers=h(admin_csrf)
            ),
            api.put(f"{V2}/{qid}/planning", json={"effective_work_days": 9}, headers=h(admin_csrf)),
            api.put(f"{V2}/{qid}/firing", json={"low_fire_enabled": False}, headers=h(admin_csrf)),
            api.put(
                f"{V2}/{qid}/products/{datos['lines'][0]}",
                json={"quantity": 1},
                headers=h(admin_csrf),
            ),
            api.post(f"{V2}/{qid}/products", json={"product_name": "x"}, headers=h(admin_csrf)),
        )
        for respuesta in await asyncio.gather(*intentos):
            assert respuesta.status_code == 409, respuesta.text

    async def test_cambio_despues_del_resumen_da_conflicto(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Usuario A revisa; usuario B cambia una cantidad; A confirma -> 409."""
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        visto = await preview(api, qid)

        cambio = await api.put(
            f"{V2}/{qid}/products/{datos['lines'][0]}",
            json={"quantity": 120},
            headers=h(admin_csrf),
        )
        assert cambio.status_code == 200, cambio.text

        r = await api.post(
            f"{V2}/{qid}/confirm",
            json={"expected_fingerprint": visto["fingerprint"]},
            headers=h(admin_csrf),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "V2_QUOTATION_CHANGED"
        assert (await api.get(f"{V2}/{qid}")).json()["status"] == "DRAFT"

    async def test_releer_el_resumen_no_cambia_la_huella(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        primera = await preview(api, datos["id"])
        segunda = await preview(api, datos["id"])
        assert primera["fingerprint"] == segunda["fingerprint"]


# ---------------------------------------------------------------------------
# CASO 4: doble confirmar -> una sola emision
# ---------------------------------------------------------------------------
class TestDobleConfirmar:
    async def test_dos_confirmaciones_simultaneas_una_emision(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        resumen = await preview(api, qid)
        correlativo_antes = await db_session.scalar(
            text("SELECT current_value FROM document_sequences WHERE sequence_type = 'QUOTE_V2'")
        )

        respuestas = await asyncio.gather(
            *(
                api.post(
                    f"{V2}/{qid}/confirm",
                    json={"expected_fingerprint": resumen["fingerprint"]},
                    headers=h(admin_csrf),
                )
                for _ in range(5)
            )
        )
        assert all(r.status_code == 200 for r in respuestas), [r.text for r in respuestas]
        assert len({r.json()["issued_at"] for r in respuestas}) == 1
        assert len({r.json()["code"] for r in respuestas}) == 1

        eventos = await db_session.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE entity_type = 'v2_quotation'"
                " AND entity_id = :id AND metadata->>'event' = 'CONFIRMED'"
            ),
            {"id": str(qid)},
        )
        assert eventos == 1
        correlativo_despues = await db_session.scalar(
            text("SELECT current_value FROM document_sequences WHERE sequence_type = 'QUOTE_V2'")
        )
        assert correlativo_despues == correlativo_antes, "confirmar no gasta correlativo"


# ---------------------------------------------------------------------------
# CASOS 2 y 3: vencida -> duplicar con precios de hoy; la antigua no cambia
# ---------------------------------------------------------------------------
class TestVencerYDuplicar:
    async def test_vencida_duplicada_recotiza_y_la_antigua_conserva_todo(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf, currency="USD")
        qid = datos["id"]
        antigua = await emitir(api, admin_csrf, qid)
        pdf_antes = compacto(await pdf_texto(api, qid))
        precio_antes = (await api.get(f"{V2}/{qid}/pricing")).json()

        await vencer(db_session, qid)
        vencida = (await api.get(f"{V2}/{qid}")).json()
        assert vencida["effective_status"] == "EXPIRED"

        # No se acepta con el precio viejo.
        r = await api.post(f"{V2}/{qid}/send-to-production", headers=h(admin_csrf))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "V2_QUOTATION_EXPIRED"

        # Hoy todo cuesta distinto: TC, pasta, gas, vigencia e IGV.
        ajustes = (await api.get(V2_SETTINGS)).json()["settings"]
        cambio = await api.put(
            V2_SETTINGS,
            json={
                "expected_version": ajustes["version"],
                "default_exchange_rate": "3.82",
                "quotation_validity_days": 30,
            },
            headers=h(admin_csrf),
        )
        assert cambio.status_code == 200, cambio.text
        materiales = (await api.get("/api/v1/quoter-v2/materials")).json()["items"]
        material = next(m for m in materiales if m["product_id"] == datos["pasta_id"])
        pasta = await api.put(
            f"/api/v1/quoter-v2/materials/{datos['pasta_id']}",
            json={
                "expected_version": material["version"],
                "material_kind": "BODY",
                "origin": "PURCHASE",
                "purchase_quantity": "100000",
                "purchase_cost": "150",
                "transport_cost": "30",
            },
            headers=h(admin_csrf),
        )
        assert pasta.status_code == 200, pasta.text
        gas = await api.put(
            f"{V2_SETTINGS}/kiln-rates/{datos['kiln_id']}/LOW",
            json={"gas_cost": "40", "external_rate": "200"},
            headers=h(admin_csrf),
        )
        assert gas.status_code == 200, gas.text
        comercial = (await api.get(COMMERCIAL)).json()
        igv = await api.put(
            COMMERCIAL,
            json={"version": comercial["version"], "tax_percent": "19"},
            headers=h(admin_csrf),
        )
        assert igv.status_code == 200, igv.text

        dup = await api.post(f"{V2}/{qid}/duplicate", headers=h(admin_csrf))
        assert dup.status_code == 201, dup.text
        nueva = dup.json()["quotation"]
        assert nueva["id"] != qid
        assert nueva["code"] != antigua["code"]
        assert nueva["status"] == "DRAFT"
        assert nueva["duplicated_from_id"] == qid
        assert Decimal(nueva["exchange_rate"]) == Decimal("3.82")
        assert Decimal(nueva["tax_percent"]) == Decimal("19")
        assert nueva["validity_days"] == 30
        assert nueva["currency_code"] == "USD"

        lineas_nuevas = (await api.get(f"{V2}/{nueva['id']}/products")).json()["items"]
        assert [linea["product_name"] for linea in lineas_nuevas] == ["Plato hondo"]
        assert lineas_nuevas[0]["client_observation"] == "Observacion de Plato hondo"
        assert Decimal(lineas_nuevas[0]["body_cost_per_unit"]) == Decimal("0.0018")
        quema_nueva = (await api.get(f"{V2}/{nueva['id']}/firing")).json()
        assert Decimal(quema_nueva["gas_cost_low"]) == Decimal("40")

        # La antigua: mismo estado, mismas cifras, mismo PDF.
        otra_vez = (await api.get(f"{V2}/{qid}")).json()
        assert otra_vez["status"] == "CONFIRMED"
        assert otra_vez["effective_status"] == "EXPIRED"
        assert otra_vez["exchange_rate"] == antigua["exchange_rate"]
        assert otra_vez["tax_percent"] == antigua["tax_percent"]
        assert otra_vez["validity_days"] == 20
        assert otra_vez["open_duplicate_id"] == nueva["id"]
        assert (await api.get(f"{V2}/{qid}/pricing")).json()["total"] == precio_antes["total"]
        lineas_viejas = (await api.get(f"{V2}/{qid}/products")).json()["items"]
        assert Decimal(lineas_viejas[0]["body_cost_per_unit"]) == Decimal("0.0013")

        pdf_despues = compacto(await pdf_texto(api, qid))
        # Mismo documento salvo el distintivo de vencida.
        assert pdf_despues.replace("cotizaciónvencida", "") == pdf_antes
        tasa_vieja = Decimal(antigua["exchange_rate"]).quantize(Decimal("0.01"))
        assert format(tasa_vieja, "f") in pdf_despues
        assert "3.82" not in pdf_despues

    async def test_duplicar_dos_veces_devuelve_el_mismo_borrador(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        await emitir(api, admin_csrf, qid)
        await vencer(db_session, qid)

        respuestas = await asyncio.gather(
            *(api.post(f"{V2}/{qid}/duplicate", headers=h(admin_csrf)) for _ in range(4))
        )
        assert all(r.status_code in (200, 201) for r in respuestas), [r.text for r in respuestas]
        ids = {r.json()["quotation"]["id"] for r in respuestas}
        assert len(ids) == 1
        assert sum(1 for r in respuestas if r.json()["created"]) == 1
        abiertas = await db_session.scalar(
            text("SELECT count(*) FROM v2_quotations WHERE duplicated_from_id = :id"),
            {"id": qid},
        )
        assert abiertas == 1

    async def test_una_vigente_no_se_duplica(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        await emitir(api, admin_csrf, datos["id"])
        r = await api.post(f"{V2}/{datos['id']}/duplicate", headers=h(admin_csrf))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "V2_QUOTATION_NOT_DUPLICABLE"

    async def test_maestros_desactivados_se_avisan_y_no_se_copian(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        await emitir(api, admin_csrf, qid)
        pdf_antes = compacto(await pdf_texto(api, qid))
        await vencer(db_session, qid)

        await db_session.execute(
            text("UPDATE partners SET active = false WHERE id = :id"), {"id": datos["customer_id"]}
        )
        await db_session.execute(
            text("UPDATE products SET active = false WHERE id = :id"), {"id": datos["pasta_id"]}
        )
        await db_session.execute(
            text("UPDATE v2_workers SET active = false WHERE id = :id"), {"id": datos["worker_id"]}
        )
        await db_session.commit()

        dup = await api.post(f"{V2}/{qid}/duplicate", headers=h(admin_csrf))
        assert dup.status_code == 201, dup.text
        codigos = {aviso["code"] for aviso in dup.json()["warnings"]}
        assert "V2_DUPLICATE_CUSTOMER_UNAVAILABLE" in codigos
        assert "V2_DUPLICATE_BODY_MATERIAL_UNAVAILABLE" in codigos
        assert "V2_DUPLICATE_LABOR_UNAVAILABLE" in codigos
        nueva = dup.json()["quotation"]
        assert nueva["customer_id"] is None
        lineas = (await api.get(f"{V2}/{nueva['id']}/products")).json()["items"]
        assert lineas[0]["body_material_id"] is None

        # El PDF de la antigua no se entera de que el cliente se archivo.
        pdf_despues = compacto(await pdf_texto(api, qid))
        assert "andinassac" in pdf_despues
        assert pdf_despues.replace("cotizaciónvencida", "") == pdf_antes

        # Y la nueva no puede emitirse sin cliente.
        resumen = await preview(api, nueva["id"])
        assert "V2_CONFIRM_CUSTOMER_REQUIRED" in {b["code"] for b in resumen["blockers"]}


# ---------------------------------------------------------------------------
# CASO 5: doble enviar a produccion -> un solo puente, sin inventario
# ---------------------------------------------------------------------------
class TestProduccion:
    async def test_doble_envio_un_solo_puente_y_nada_de_inventario(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        await emitir(api, admin_csrf, qid)
        movimientos = await db_session.scalar(text("SELECT count(*) FROM stock_movements"))
        ordenes = await db_session.scalar(text("SELECT count(*) FROM production_orders"))

        respuestas = await asyncio.gather(
            *(api.post(f"{V2}/{qid}/send-to-production", headers=h(admin_csrf)) for _ in range(5))
        )
        assert all(r.status_code in (200, 201) for r in respuestas), [r.text for r in respuestas]
        assert len({r.json()["handoff"]["id"] for r in respuestas}) == 1
        assert sum(1 for r in respuestas if r.json()["created"]) == 1

        puentes = await db_session.scalar(
            text("SELECT count(*) FROM v2_production_handoffs WHERE v2_quotation_id = :id"),
            {"id": qid},
        )
        assert puentes == 1
        assert await db_session.scalar(text("SELECT count(*) FROM stock_movements")) == movimientos
        assert await db_session.scalar(text("SELECT count(*) FROM production_orders")) == ordenes

        vista = (await api.get(f"{V2}/{qid}")).json()
        assert vista["effective_status"] == "READY_FOR_PRODUCTION"
        assert vista["production_handoff"]["v2_quotation_id"] == qid

        # Ya aceptada: no vence, no se cancela.
        await vencer(db_session, qid)
        assert (await api.get(f"{V2}/{qid}")).json()["effective_status"] == "READY_FOR_PRODUCTION"
        r = await api.post(f"{V2}/{qid}/cancel", json={}, headers=h(admin_csrf))
        assert r.status_code == 409

        historia = (await api.get(f"{V2}/{qid}/history")).json()
        assert [e["event"] for e in historia] == ["CREATED", "CONFIRMED", "SENT_TO_PRODUCTION"]

    async def test_un_borrador_no_pasa_a_produccion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        r = await api.post(f"{V2}/{datos['id']}/send-to-production", headers=h(admin_csrf))
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "V2_QUOTATION_NOT_SENDABLE"

    async def test_el_taller_no_emite_ni_envia(
        self, api: httpx.AsyncClient, admin_csrf: str, operator_csrf: str
    ) -> None:
        # `operator_csrf` reautentica el cliente como operador.
        r = await api.post(f"{V2}/1/send-to-production", headers=h(operator_csrf))
        assert r.status_code == 403
        r = await api.post(
            f"{V2}/1/confirm", json={"expected_fingerprint": "0" * 64}, headers=h(operator_csrf)
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Anular
# ---------------------------------------------------------------------------
class TestAnular:
    async def test_anular_una_emitida_conserva_el_pdf_marcado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await cotizacion_completa(api, admin_csrf)
        qid = datos["id"]
        await emitir(api, admin_csrf, qid)
        r = await api.post(
            f"{V2}/{qid}/cancel", json={"reason": "Cliente desistio"}, headers=h(admin_csrf)
        )
        assert r.status_code == 200, r.text
        assert r.json()["effective_status"] == "CANCELLED"
        assert r.json()["cancel_reason"] == "Cliente desistio"
        # Idempotente.
        otra = await api.post(f"{V2}/{qid}/cancel", json={}, headers=h(admin_csrf))
        assert otra.status_code == 200
        assert otra.json()["cancelled_at"] == r.json()["cancelled_at"]

        texto = compacto(await pdf_texto(api, qid))
        assert "anulada" in texto
        dup = await api.post(f"{V2}/{qid}/duplicate", headers=h(admin_csrf))
        assert dup.status_code == 201, dup.text

    async def test_una_anulada_sin_emitir_no_tiene_pdf(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await api.post(V2, json={"name": "Nunca emitida"}, headers=h(admin_csrf))
        qid = int(creada.json()["id"])
        r = await api.post(f"{V2}/{qid}/cancel", json={}, headers=h(admin_csrf))
        assert r.status_code == 200, r.text
        pdf = await api.get(f"{V2}/{qid}/pdf")
        assert pdf.status_code == 409
        assert pdf.json()["error"]["code"] == "V2_QUOTATION_PDF_NOT_ISSUED"


# ---------------------------------------------------------------------------
# CASOS 7 y 8: USD con TC congelado y multiproducto
# ---------------------------------------------------------------------------
class TestDocumento:
    async def test_usd_multiproducto_conserva_tc_y_lineas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        datos = await cotizacion_completa(
            api,
            admin_csrf,
            currency="USD",
            productos=(("Plato hondo", 100), ("Taza de te", 50), ("Fuente grande", 5)),
        )
        qid = datos["id"]
        emitida = await emitir(api, admin_csrf, qid)
        tasa = Decimal(emitida["exchange_rate"])
        texto_antes = compacto(await pdf_texto(api, qid))
        for nombre in ("platohondo", "tazadete", "fuentegrande", "usd"):
            assert nombre in texto_antes, nombre
        assert format(tasa.quantize(Decimal("0.01")), "f") in texto_antes

        ajustes = (await api.get(V2_SETTINGS)).json()["settings"]
        cambio = await api.put(
            V2_SETTINGS,
            json={"expected_version": ajustes["version"], "default_exchange_rate": "4.25"},
            headers=h(admin_csrf),
        )
        assert cambio.status_code == 200, cambio.text

        assert compacto(await pdf_texto(api, qid)) == texto_antes, (
            "el PDF no puede leer el TC de hoy"
        )
        resumen = await preview(api, qid)
        assert Decimal(resumen["exchange_rate"]) == tasa
        assert len(resumen["lines"]) == 3
        assert Decimal(resumen["subtotal_amount"]) == sum(
            (Decimal(linea["line_subtotal"]) for linea in resumen["lines"]), Decimal(0)
        )
