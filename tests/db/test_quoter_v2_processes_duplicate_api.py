"""Duplicar una cotizacion con procesos y adicionales (correccion 010H).

Duplicar recotiza con los maestros de HOY, pero las decisiones que tomo quien
cotizo son de la cotizacion, no del maestro: lo que quito sigue quitado, lo que
anadio sigue puesto y las piezas que escribio siguen siendo las suyas. Un
duplicado que devolviera el acabado que el cliente no queria seria una oferta
distinta de la que se pidio copiar.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from tests.db.test_quoter_v2_labor_api import crear_cotizacion, crear_tecnica, crear_trabajador
from tests.db.test_quoter_v2_processes_api import (
    EXTRAS,
    PROCESOS,
    V2,
    h,
    linea_de,
    pieza_con_procesos,
    procesos_de,
)
from tests.db.v2_capacidades import habilitar


async def anular(api: httpx.AsyncClient, csrf: str, qid: int) -> None:
    """Anulada se duplica igual que vencida, y no exige una cotizacion completa.

    Lo que se prueba aqui es que la COPIA hereda las decisiones de procesos y
    adicionales; por que camino dejo de ser borrador da lo mismo.
    """
    respuesta = await api.post(
        f"{V2}/{qid}/cancel", json={"reason": "Prueba de duplicado"}, headers=h(csrf)
    )
    assert respuesta.status_code == 200, respuesta.text


class TestDuplicarProcesos:
    async def test_lo_quitado_sigue_quitado_y_lo_anadido_sigue_puesto(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        torno = await crear_tecnica(api, admin_csrf, "dup-torno", name="Torno")
        acabado = await crear_tecnica(api, admin_csrf, "dup-acabado", name="Acabado")
        pulido = await crear_tecnica(api, admin_csrf, "dup-pulido", name="Pulido")
        pieza = await pieza_con_procesos(
            api, admin_csrf, "Taza duplicable", [torno["id"], acabado["id"]]
        )
        original = await crear_cotizacion(api, admin_csrf)
        linea = await linea_de(api, admin_csrf, original, pieza, 20)

        sobra = next(
            p for p in await procesos_de(api, original) if p["technique_name"] == "Acabado"
        )
        await api.delete(f"{V2}/{original}/{PROCESOS}/{sobra['id']}", headers=h(admin_csrf))
        await api.post(
            f"{V2}/{original}/{PROCESOS}",
            json={"v2_quotation_product_id": linea, "technique_id": pulido["id"]},
            headers=h(admin_csrf),
        )
        torno_original = next(
            p for p in await procesos_de(api, original) if p["technique_name"] == "Torno"
        )
        await api.put(
            f"{V2}/{original}/{PROCESOS}/{torno_original['id']}/quantity",
            json={"quantity": "7"},
            headers=h(admin_csrf),
        )
        await anular(api, admin_csrf, original)

        respuesta = await api.post(f"{V2}/{original}/duplicate", json={}, headers=h(admin_csrf))
        assert respuesta.status_code == 201, respuesta.text
        copia = int(respuesta.json()["quotation"]["id"])

        procesos = {p["technique_name"]: p for p in await procesos_de(api, copia)}
        assert "Acabado" not in procesos
        assert "Pulido" in procesos
        assert Decimal(procesos["Torno"]["quantity"]) == Decimal(7)
        assert procesos["Torno"]["quantity_overridden"] is True

    async def test_la_tarea_copiada_vuelve_a_colgar_de_su_proceso(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sin el enlace, el mismo trabajo se veria dos veces: sin asignar y suelto."""
        torno = await crear_tecnica(api, admin_csrf, "dup-enlace", name="Torno")
        pieza = await pieza_con_procesos(api, admin_csrf, "Plato enlazado", [torno["id"]])
        obrero = await crear_trabajador(api, admin_csrf, "Juan duplicado")
        await habilitar(api, admin_csrf, obrero["id"], torno["id"])
        original = await crear_cotizacion(api, admin_csrf)
        await linea_de(api, admin_csrf, original, pieza, 20)
        proceso = (await procesos_de(api, original))[0]
        await api.post(
            f"{V2}/{original}/{PROCESOS}/{proceso['id']}/assign",
            json={"worker_id": obrero["id"]},
            headers=h(admin_csrf),
        )
        await anular(api, admin_csrf, original)

        copia = int(
            (await api.post(f"{V2}/{original}/duplicate", json={}, headers=h(admin_csrf))).json()[
                "quotation"
            ]["id"]
        )

        procesos = await procesos_de(api, copia)
        assert len(procesos) == 1
        assert procesos[0]["worker_name"] == "Juan duplicado"
        tareas = (await api.get(f"{V2}/{copia}/labor")).json()["items"]
        assert len(tareas) == 1

    async def test_el_adicional_se_recotiza_con_el_precio_de_hoy(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        concepto = (
            await api.post(
                EXTRAS,
                json={"name": "Empaque duplicable", "unit_cost": "20"},
                headers=h(admin_csrf),
            )
        ).json()
        original = await crear_cotizacion(api, admin_csrf)
        await api.post(
            f"{V2}/{original}/products",
            json={"product_name": "Pieza", "quantity": 2},
            headers=h(admin_csrf),
        )
        await api.post(
            f"{V2}/{original}/extras",
            json={"v2_extra_id": concepto["id"], "quantity": "3"},
            headers=h(admin_csrf),
        )
        # Sube el maestro DESPUES de emitir: la vieja conserva su precio.
        await api.put(
            f"{EXTRAS}/{concepto['id']}",
            json={"expected_version": concepto["version"], "unit_cost": "50"},
            headers=h(admin_csrf),
        )
        await anular(api, admin_csrf, original)

        copia = int(
            (await api.post(f"{V2}/{original}/duplicate", json={}, headers=h(admin_csrf))).json()[
                "quotation"
            ]["id"]
        )

        vieja = (await api.get(f"{V2}/{original}/extras")).json()
        nueva = (await api.get(f"{V2}/{copia}/extras")).json()
        assert Decimal(vieja["extras_cost_total"]) == Decimal(60)
        assert Decimal(nueva["extras_cost_total"]) == Decimal(150)
        assert nueva["items"][0]["unit_cost_is_override"] is False

    async def test_un_concepto_retirado_no_se_recotiza_a_ciegas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        concepto = (
            await api.post(
                EXTRAS,
                json={"name": "Concepto que se retira", "unit_cost": "10"},
                headers=h(admin_csrf),
            )
        ).json()
        original = await crear_cotizacion(api, admin_csrf)
        await api.post(
            f"{V2}/{original}/products",
            json={"product_name": "Pieza", "quantity": 1},
            headers=h(admin_csrf),
        )
        await api.post(
            f"{V2}/{original}/extras",
            json={"v2_extra_id": concepto["id"], "quantity": "1"},
            headers=h(admin_csrf),
        )
        await api.put(
            f"{EXTRAS}/{concepto['id']}",
            json={"expected_version": concepto["version"], "active": False},
            headers=h(admin_csrf),
        )
        await anular(api, admin_csrf, original)

        respuesta = await api.post(f"{V2}/{original}/duplicate", json={}, headers=h(admin_csrf))

        assert respuesta.status_code == 201, respuesta.text
        assert any(
            aviso["code"] == "V2_DUPLICATE_EXTRA_UNAVAILABLE"
            for aviso in respuesta.json()["warnings"]
        )
        copia = int(respuesta.json()["quotation"]["id"])
        assert (await api.get(f"{V2}/{copia}/extras")).json()["items"] == []
