"""Correccion 010H — el trabajador tiene sus tecnicas; la cotizacion las carga.

Regla explicita del taller, que manda sobre el Excel y sobre 010D: la capacidad
es del MAESTRO del trabajador. Al elegirlo en una cotizacion se cargan sus
tecnicas habilitadas y activas; quien cotiza quita las que no tocan, y quitar en
una cotizacion no toca el maestro. El rendimiento sigue siendo de la tecnica y
la formula de 010D no cambia.

La pantalla solo ofrece lo habilitado, pero la pantalla no es la barrera: estas
pruebas mandan a mano el request que la pantalla nunca mandaria.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quoter_v2_labor_api import (
    TECHNIQUES,
    V2,
    WORKERS,
    crear_cotizacion,
    crear_tecnica,
    crear_trabajador,
)

JORNADA = Decimal(8)


def h(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def ficha(api: httpx.AsyncClient, worker_id: int) -> dict[str, Any]:
    fichas = (await api.get(WORKERS)).json()["items"]
    return dict(next(item for item in fichas if item["id"] == worker_id))


async def linea(api: httpx.AsyncClient, csrf: str, qid: int, nombre: str, piezas: int) -> int:
    r = await api.post(
        f"{V2}/{qid}/products",
        json={"product_name": nombre, "quantity": piezas},
        headers=h(csrf),
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def cargar(
    api: httpx.AsyncClient, csrf: str, qid: int, worker_id: int, **extra: Any
) -> httpx.Response:
    return await api.post(
        f"{V2}/{qid}/labor/load-worker",
        json={"worker_id": worker_id, **extra},
        headers=h(csrf),
    )


async def tornero_con_tres(
    api: httpx.AsyncClient, csrf: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tecnicas = [
        await crear_tecnica(api, csrf, "wt-torno", default_capacity_per_workday="50"),
        await crear_tecnica(api, csrf, "wt-asas", default_capacity_per_workday="80"),
        await crear_tecnica(api, csrf, "wt-acabado", default_capacity_per_workday="40"),
    ]
    worker = await crear_trabajador(
        api,
        csrf,
        "Juan Perez",
        daily_rate="120",
        technique_ids=[t["id"] for t in tecnicas],
    )
    return worker, tecnicas


# ---------------------------------------------------------------------------
# Maestro
# ---------------------------------------------------------------------------
class TestMaestro:
    async def test_asignar_y_leer_tres_tecnicas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        assert sorted(worker["technique_ids"]) == sorted(t["id"] for t in tecnicas)
        assert sorted((await ficha(api, worker["id"]))["technique_ids"]) == sorted(
            t["id"] for t in tecnicas
        )

    async def test_quitar_una_del_maestro(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        r = await api.put(
            f"{WORKERS}/{worker['id']}",
            json={
                "expected_version": worker["version"],
                "technique_ids": [tecnicas[0]["id"], tecnicas[1]["id"]],
            },
            headers=h(admin_csrf),
        )
        assert r.status_code == 200, r.text
        assert sorted(r.json()["technique_ids"]) == sorted([tecnicas[0]["id"], tecnicas[1]["id"]])
        assert r.json()["version"] == worker["version"] + 1

    async def test_interno_y_externo_igual(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        tecnica = await crear_tecnica(api, admin_csrf, "wt-externo")
        otra = await crear_tecnica(api, admin_csrf, "wt-externo-2")
        externo = await crear_trabajador(
            api,
            admin_csrf,
            "Externo con dos",
            worker_type="EXTERNAL",
            technique_ids=[tecnica["id"], otra["id"]],
        )
        assert len(externo["technique_ids"]) == 2

    async def test_una_tecnica_inexistente_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        r = await api.post(
            WORKERS,
            json={
                "name": "Con fantasma",
                "worker_type": "INTERNAL",
                "daily_rate": "100",
                "technique_ids": [999999],
            },
            headers=h(admin_csrf),
        )
        assert r.status_code == 404

    async def test_edicion_concurrente_del_maestro_choca(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        primera, segunda = await asyncio.gather(
            api.put(
                f"{WORKERS}/{worker['id']}",
                json={"expected_version": worker["version"], "technique_ids": [tecnicas[0]["id"]]},
                headers=h(admin_csrf),
            ),
            api.put(
                f"{WORKERS}/{worker['id']}",
                json={"expected_version": worker["version"], "technique_ids": [tecnicas[1]["id"]]},
                headers=h(admin_csrf),
            ),
        )
        estados = sorted([primera.status_code, segunda.status_code])
        assert estados == [200, 409], (primera.text, segunda.text)

    async def test_el_taller_no_configura_capacidades(
        self, api: httpx.AsyncClient, operator_csrf: str
    ) -> None:
        r = await api.post(
            WORKERS,
            json={"name": "x", "worker_type": "INTERNAL", "daily_rate": "1", "technique_ids": []},
            headers=h(operator_csrf),
        )
        assert r.status_code == 403
        assert (
            await api.post(
                f"{V2}/1/labor/load-worker", json={"worker_id": 1}, headers=h(operator_csrf)
            )
        ).status_code == 403


# ---------------------------------------------------------------------------
# Cotizacion
# ---------------------------------------------------------------------------
class TestCargaEnLaCotizacion:
    async def test_elegir_trabajador_carga_sus_tres_tecnicas_con_las_piezas_del_producto(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        qid = await crear_cotizacion(api, admin_csrf)
        jarra = await linea(api, admin_csrf, qid, "Jarra", 20)

        r = await cargar(api, admin_csrf, qid, worker["id"], v2_quotation_product_id=jarra)
        assert r.status_code == 200, r.text
        creadas = r.json()["created"]
        assert sorted(t["technique_id"] for t in creadas) == sorted(t["id"] for t in tecnicas)
        for tarea in creadas:
            assert Decimal(tarea["quantity"]) == Decimal(20)
            assert tarea["v2_quotation_product_id"] == jarra
            # Formula de 010D intacta: horas = piezas x jornada / rendimiento.
            capacidad = Decimal(tarea["standard_capacity"])
            assert Decimal(tarea["calculated_hours"]) == (
                Decimal(20) * JORNADA / capacidad
            ).quantize(Decimal(tarea["calculated_hours"]))
            assert Decimal(tarea["hourly_rate"]) == Decimal(15)  # 120 / 8

    async def test_cargar_dos_veces_no_duplica(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, _ = await tornero_con_tres(api, admin_csrf)
        qid = await crear_cotizacion(api, admin_csrf)
        jarra = await linea(api, admin_csrf, qid, "Jarra", 20)
        respuestas = await asyncio.gather(
            *(
                cargar(api, admin_csrf, qid, worker["id"], v2_quotation_product_id=jarra)
                for _ in range(3)
            )
        )
        assert all(r.status_code == 200 for r in respuestas), [r.text for r in respuestas]
        tareas = (await api.get(f"{V2}/{qid}/labor")).json()["items"]
        assert len(tareas) == 3

    async def test_quitar_en_la_cotizacion_no_toca_el_maestro_y_otra_cotizacion_la_vuelve_a_cargar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        qid = await crear_cotizacion(api, admin_csrf)
        creadas = (await cargar(api, admin_csrf, qid, worker["id"])).json()["created"]
        quitada = next(t for t in creadas if t["technique_id"] == tecnicas[2]["id"])
        r = await api.delete(f"{V2}/{qid}/labor/{quitada['id']}", headers=h(admin_csrf))
        assert r.status_code == 204

        assert len((await api.get(f"{V2}/{qid}/labor")).json()["items"]) == 2
        assert len((await ficha(api, worker["id"]))["technique_ids"]) == 3
        tecnica = next(
            t for t in (await api.get(TECHNIQUES)).json()["items"] if t["id"] == tecnicas[2]["id"]
        )
        assert tecnica["active"] is True

        otra = await crear_cotizacion(api, admin_csrf)
        assert len((await cargar(api, admin_csrf, otra, worker["id"])).json()["created"]) == 3

    async def test_todo_el_pedido_nace_en_cero_piezas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, _ = await tornero_con_tres(api, admin_csrf)
        qid = await crear_cotizacion(api, admin_csrf)
        await linea(api, admin_csrf, qid, "Jarra", 20)
        await linea(api, admin_csrf, qid, "Taza", 30)
        creadas = (await cargar(api, admin_csrf, qid, worker["id"])).json()["created"]
        assert all(Decimal(t["quantity"]) == 0 for t in creadas)
        assert all(t["v2_quotation_product_id"] is None for t in creadas)

    async def test_horas_manuales_nacen_en_cero_y_como_personal_adicional(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        adicional = await crear_tecnica(
            api, admin_csrf, "wt-adicional", default_capacity_per_workday="1", manual_hours=True
        )
        assert adicional["manual_hours"] is True
        externo = await crear_trabajador(
            api, admin_csrf, "Apoyo", worker_type="EXTERNAL", technique_ids=[adicional["id"]]
        )
        qid = await crear_cotizacion(api, admin_csrf)
        jarra = await linea(api, admin_csrf, qid, "Jarra", 50)
        tarea = (
            await cargar(api, admin_csrf, qid, externo["id"], v2_quotation_product_id=jarra)
        ).json()["created"][0]
        assert Decimal(tarea["quantity"]) == 0, "50 piezas a 1 por jornada serian 400 horas"
        assert tarea["is_additional_personnel"] is True

    async def test_subconjunto_marcado_y_tecnica_no_habilitada_en_el_subconjunto(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        ajena = await crear_tecnica(api, admin_csrf, "wt-ajena")
        qid = await crear_cotizacion(api, admin_csrf)
        r = await cargar(api, admin_csrf, qid, worker["id"], technique_ids=[tecnicas[0]["id"]])
        assert [t["technique_id"] for t in r.json()["created"]] == [tecnicas[0]["id"]]
        r = await cargar(api, admin_csrf, qid, worker["id"], technique_ids=[ajena["id"]])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "V2_LABOR_TECHNIQUE_NOT_ALLOWED"

    async def test_trabajador_sin_tecnicas_no_inventa_tareas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Sin capacidades")
        qid = await crear_cotizacion(api, admin_csrf)
        r = await cargar(api, admin_csrf, qid, worker["id"])
        assert r.status_code == 200, r.text
        assert r.json()["created"] == []
        assert r.json()["warnings"] == ["V2_LABOR_WORKER_WITHOUT_TECHNIQUES"]
        assert (await api.get(f"{V2}/{qid}/labor")).json()["items"] == []

    async def test_tecnica_inactiva_no_se_carga(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        baja = await api.put(
            f"{TECHNIQUES}/{tecnicas[1]['id']}",
            json={"expected_version": tecnicas[1]["version"], "active": False},
            headers=h(admin_csrf),
        )
        assert baja.status_code == 200, baja.text
        qid = await crear_cotizacion(api, admin_csrf)
        creadas = (await cargar(api, admin_csrf, qid, worker["id"])).json()["created"]
        assert tecnicas[1]["id"] not in [t["technique_id"] for t in creadas]
        assert len(creadas) == 2

    async def test_trabajador_inactivo_no_se_carga(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, _ = await tornero_con_tres(api, admin_csrf)
        await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": worker["version"], "active": False},
            headers=h(admin_csrf),
        )
        qid = await crear_cotizacion(api, admin_csrf)
        r = await cargar(api, admin_csrf, qid, worker["id"])
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "V2_LABOR_RESOURCE_INACTIVE"

    async def test_producto_de_otra_cotizacion_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, _ = await tornero_con_tres(api, admin_csrf)
        una = await crear_cotizacion(api, admin_csrf)
        otra = await crear_cotizacion(api, admin_csrf)
        ajena = await linea(api, admin_csrf, otra, "Ajena", 5)
        r = await cargar(api, admin_csrf, una, worker["id"], v2_quotation_product_id=ajena)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# El backend es la barrera
# ---------------------------------------------------------------------------
class TestBarreraDelBackend:
    async def test_request_manual_con_tecnica_no_habilitada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, _ = await tornero_con_tres(api, admin_csrf)
        ajena = await crear_tecnica(api, admin_csrf, "wt-no-habilitada")
        qid = await crear_cotizacion(api, admin_csrf)
        r = await api.post(
            f"{V2}/{qid}/labor",
            json={"worker_id": worker["id"], "technique_id": ajena["id"], "quantity": "10"},
            headers=h(admin_csrf),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "V2_LABOR_TECHNIQUE_NOT_ALLOWED"

    async def test_cambiar_de_trabajador_a_quien_no_sabe_la_tecnica(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, tecnicas = await tornero_con_tres(api, admin_csrf)
        otro = await crear_trabajador(
            api, admin_csrf, "Solo asas", technique_ids=[tecnicas[1]["id"]]
        )
        qid = await crear_cotizacion(api, admin_csrf)
        tarea = next(
            t
            for t in (await cargar(api, admin_csrf, qid, worker["id"])).json()["created"]
            if t["technique_id"] == tecnicas[0]["id"]
        )
        r = await api.put(
            f"{V2}/{qid}/labor/{tarea['id']}",
            json={"worker_id": otro["id"]},
            headers=h(admin_csrf),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "V2_LABOR_TECHNIQUE_NOT_ALLOWED"

    async def test_relacion_retirada_despues_avisa_en_el_borrador_sin_encallarlo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker, _ = await tornero_con_tres(api, admin_csrf)
        qid = await crear_cotizacion(api, admin_csrf)
        tarea = (await cargar(api, admin_csrf, qid, worker["id"])).json()["created"][0]
        actual = await ficha(api, worker["id"])
        await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": actual["version"], "technique_ids": []},
            headers=h(admin_csrf),
        )
        r = await api.put(
            f"{V2}/{qid}/labor/{tarea['id']}",
            json={"quantity": "30"},
            headers=h(admin_csrf),
        )
        assert r.status_code == 200, r.text
        assert "V2_LABOR_TECHNIQUE_NOT_ENABLED" in r.json()["warnings"]
        assert Decimal(r.json()["quantity"]) == Decimal(30)

    async def test_emitida_sobrevive_al_cambio_del_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        worker, _ = await tornero_con_tres(api, admin_csrf)
        qid = await crear_cotizacion(api, admin_csrf)
        antes = (await cargar(api, admin_csrf, qid, worker["id"])).json()["created"]
        await db_session.execute(
            text(
                "UPDATE v2_quotations SET status = 'CONFIRMED', issued_at = now(),"
                " valid_until = current_date + 20, expires_at = now() + interval '21 days',"
                " commercial_fingerprint = repeat('a', 64) WHERE id = :id"
            ),
            {"id": qid},
        )
        await db_session.commit()
        actual = await ficha(api, worker["id"])
        await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": actual["version"], "technique_ids": [], "daily_rate": "999"},
            headers=h(admin_csrf),
        )
        despues = (await api.get(f"{V2}/{qid}/labor")).json()["items"]
        assert [(t["technique_id"], t["labor_cost"], t["hourly_rate"]) for t in despues] == [
            (t["technique_id"], t["labor_cost"], t["hourly_rate"]) for t in antes
        ]
        r = await cargar(api, admin_csrf, qid, worker["id"])
        assert r.status_code == 409
