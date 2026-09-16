"""Correccion 010H — la pieza manda sus procesos, y los adicionales existen.

El orden que pidio el taller, y que hasta ahora estaba al reves:

    PIEZA -> PROCESOS -> PIEZAS AFECTADAS -> HORAS -> TRABAJADOR -> COSTO

Antes habia que acordarse de que una taza lleva asa y escribirlo a mano. Estas
pruebas comprueban que aparece sola, con sus piezas puestas y sus horas
calculadas, y que las decisiones de una cotizacion —quitar un proceso, anadir
otro, escribir otras piezas— no se las lleva por delante el automatismo.

Los adicionales son la otra mitad: el Excel los suma al Costo de Produccion y al
Costo Real desde el principio, y en V2 no existian.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from tests.db.test_quoter_v2_labor_api import (
    V2,
    crear_cotizacion,
    crear_tecnica,
    crear_trabajador,
)
from tests.db.test_quoter_v2_materials_api import crear_producto
from tests.db.v2_capacidades import habilitar

PROCESOS = "processes"
EXTRAS = "/api/v1/quoter-v2/extras"
JORNADA = Decimal(8)


def h(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def pieza_con_procesos(
    api: httpx.AsyncClient, csrf: str, nombre: str, technique_ids: list[int]
) -> dict[str, Any]:
    """Una pieza de catalogo que declara que procesos necesita."""
    pieza = await crear_producto(api, csrf, nombre, product_type="FINISHED_PRODUCT")
    respuesta = await api.put(
        f"/api/v1/quoter-v2/products/{pieza['id']}/techniques",
        json={"technique_ids": technique_ids},
        headers=h(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text
    return pieza


async def linea_de(
    api: httpx.AsyncClient, csrf: str, qid: int, pieza: dict[str, Any], piezas: int
) -> int:
    respuesta = await api.post(
        f"{V2}/{qid}/products",
        json={"product_id": pieza["id"], "quantity": piezas},
        headers=h(csrf),
    )
    assert respuesta.status_code == 201, respuesta.text
    return int(respuesta.json()["id"])


async def procesos_de(api: httpx.AsyncClient, qid: int) -> list[dict[str, Any]]:
    respuesta = await api.get(f"{V2}/{qid}/{PROCESOS}")
    assert respuesta.status_code == 200, respuesta.text
    return list(respuesta.json()["items"])


class TestLaPiezaTraeSusProcesos:
    async def test_anadir_la_taza_trae_torno_asa_y_acabado_con_sus_piezas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Lo que el usuario pidio: no reconstruir el proceso productivo a mano."""
        torno = await crear_tecnica(api, admin_csrf, "proc-torno", name="Torno")
        asa = await crear_tecnica(
            api, admin_csrf, "proc-asa", name="Armado de asa", default_capacity_per_workday="50"
        )
        acabado = await crear_tecnica(api, admin_csrf, "proc-acabado", name="Acabado")
        taza = await pieza_con_procesos(
            api, admin_csrf, "Taza 250 ml", [torno["id"], asa["id"], acabado["id"]]
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        await linea_de(api, admin_csrf, cotizacion, taza, 20)

        procesos = await procesos_de(api, cotizacion)
        assert [proceso["technique_name"] for proceso in procesos] == [
            "Torno",
            "Armado de asa",
            "Acabado",
        ]
        # Las piezas nacen con la cantidad del producto, no en cero.
        assert {Decimal(proceso["quantity"]) for proceso in procesos} == {Decimal(20)}
        # Y las horas ya estan calculadas, sin trabajador todavia.
        asa_cargada = next(p for p in procesos if p["technique_name"] == "Armado de asa")
        assert Decimal(asa_cargada["calculated_hours"]) == Decimal("3.2")
        assert asa_cargada["worker_id"] is None
        assert asa_cargada["labor_cost"] is None

    async def test_una_pieza_sin_procesos_configurados_lo_dice(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """No es un error: es una pieza que todavia no declara nada."""
        pieza = await pieza_con_procesos(api, admin_csrf, "Pieza muda", [])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        respuesta = await api.post(
            f"{V2}/{cotizacion}/products",
            json={"product_id": pieza["id"], "quantity": 10},
            headers=h(admin_csrf),
        )

        assert respuesta.status_code == 201, respuesta.text
        assert "V2_PROCESS_PRODUCT_WITHOUT_TECHNIQUES" in respuesta.json()["warnings"]
        assert await procesos_de(api, cotizacion) == []

    async def test_una_pieza_a_medida_no_hereda_nada_y_no_falla(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)
        respuesta = await api.post(
            f"{V2}/{cotizacion}/products",
            json={"product_name": "Encargo raro", "quantity": 3},
            headers=h(admin_csrf),
        )
        assert respuesta.status_code == 201, respuesta.text
        assert await procesos_de(api, cotizacion) == []

    async def test_una_tecnica_retirada_no_se_propone(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        viva = await crear_tecnica(api, admin_csrf, "proc-viva", name="Viva")
        muerta = await crear_tecnica(api, admin_csrf, "proc-muerta", name="Retirada")
        pieza = await pieza_con_procesos(
            api, admin_csrf, "Con retirada", [viva["id"], muerta["id"]]
        )
        await api.put(
            f"/api/v1/quoter-v2/techniques/{muerta['id']}",
            json={"expected_version": muerta["version"], "active": False},
            headers=h(admin_csrf),
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        await linea_de(api, admin_csrf, cotizacion, pieza, 5)

        procesos = await procesos_de(api, cotizacion)
        assert [proceso["technique_name"] for proceso in procesos] == ["Viva"]


class TestDecisionesDeEstaCotizacion:
    async def test_quitar_un_proceso_no_toca_el_maestro_ni_vuelve_solo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Quitar el acabado de ESTE pedido no se lo quita a la pieza."""
        torno = await crear_tecnica(api, admin_csrf, "quita-torno", name="Torno")
        acabado = await crear_tecnica(api, admin_csrf, "quita-acabado", name="Acabado")
        pieza = await pieza_con_procesos(api, admin_csrf, "Plato", [torno["id"], acabado["id"]])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await linea_de(api, admin_csrf, cotizacion, pieza, 10)

        procesos = await procesos_de(api, cotizacion)
        sobra = next(p for p in procesos if p["technique_name"] == "Acabado")
        borrado = await api.delete(
            f"{V2}/{cotizacion}/{PROCESOS}/{sobra['id']}", headers=h(admin_csrf)
        )
        assert borrado.status_code == 200, borrado.text
        assert [p["technique_name"] for p in borrado.json()["items"]] == ["Torno"]

        # Cambiar la cantidad regenera cantidades: el acabado NO debe resucitar.
        await api.put(
            f"{V2}/{cotizacion}/products/{linea}",
            json={"quantity": 30},
            headers=h(admin_csrf),
        )
        assert [p["technique_name"] for p in await procesos_de(api, cotizacion)] == ["Torno"]

        # Y el maestro de la pieza sigue pidiendo los dos.
        maestro = (await api.get(f"/api/v1/quoter-v2/products/{pieza['id']}/techniques")).json()
        assert [fila["technique_name"] for fila in maestro["items"] if fila["active"]] == [
            "Torno",
            "Acabado",
        ]

    async def test_anadir_un_proceso_extra_solo_en_esta_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        torno = await crear_tecnica(api, admin_csrf, "extra-torno", name="Torno")
        pulido = await crear_tecnica(api, admin_csrf, "extra-pulido", name="Pulido")
        pieza = await pieza_con_procesos(api, admin_csrf, "Pedido especial", [torno["id"]])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await linea_de(api, admin_csrf, cotizacion, pieza, 8)

        respuesta = await api.post(
            f"{V2}/{cotizacion}/{PROCESOS}",
            json={"v2_quotation_product_id": linea, "technique_id": pulido["id"]},
            headers=h(admin_csrf),
        )

        assert respuesta.status_code == 201, respuesta.text
        assert respuesta.json()["origin"] == "MANUAL"
        # Nace con las piezas de la linea, como los que trajo la pieza.
        assert Decimal(respuesta.json()["quantity"]) == Decimal(8)
        maestro = (await api.get(f"/api/v1/quoter-v2/products/{pieza['id']}/techniques")).json()
        assert [fila["technique_name"] for fila in maestro["items"]] == ["Torno"]

    async def test_la_misma_tecnica_dos_veces_en_la_misma_pieza_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Dos filas de torno sobre la misma pieza serian el mismo trabajo cobrado dos veces."""
        torno = await crear_tecnica(api, admin_csrf, "dup-torno", name="Torno")
        pieza = await pieza_con_procesos(api, admin_csrf, "Pieza dup", [torno["id"]])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await linea_de(api, admin_csrf, cotizacion, pieza, 4)

        respuesta = await api.post(
            f"{V2}/{cotizacion}/{PROCESOS}",
            json={"v2_quotation_product_id": linea, "technique_id": torno["id"]},
            headers=h(admin_csrf),
        )

        assert respuesta.status_code == 409
        assert respuesta.json()["error"]["code"] == "V2_PROCESS_DUPLICATED"

    async def test_piezas_escritas_a_mano_sobreviven_al_cambio_de_cantidad(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El logo va en 5 de las 20 tazas. Subir el pedido a 30 no lo pone en 30."""
        torno = await crear_tecnica(api, admin_csrf, "mano-torno", name="Torno")
        logo = await crear_tecnica(api, admin_csrf, "mano-logo", name="Sello")
        pieza = await pieza_con_procesos(
            api, admin_csrf, "Taza con logo", [torno["id"], logo["id"]]
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await linea_de(api, admin_csrf, cotizacion, pieza, 20)

        sello = next(
            p for p in await procesos_de(api, cotizacion) if p["technique_name"] == "Sello"
        )
        await api.put(
            f"{V2}/{cotizacion}/{PROCESOS}/{sello['id']}/quantity",
            json={"quantity": "5"},
            headers=h(admin_csrf),
        )
        await api.put(
            f"{V2}/{cotizacion}/products/{linea}", json={"quantity": 30}, headers=h(admin_csrf)
        )

        procesos = {p["technique_name"]: p for p in await procesos_de(api, cotizacion)}
        assert Decimal(procesos["Sello"]["quantity"]) == Decimal(5)
        assert procesos["Sello"]["quantity_overridden"] is True
        # El que nadie toco si sigue a la cantidad del pedido.
        assert Decimal(procesos["Torno"]["quantity"]) == Decimal(30)


class TestElTrabajadorVaDespues:
    async def test_asignar_crea_la_tarea_y_aparece_el_costo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El proceso existe primero; el costo aparece cuando alguien lo hace."""
        torno = await crear_tecnica(
            api, admin_csrf, "asig-torno", name="Torno", default_capacity_per_workday="50"
        )
        pieza = await pieza_con_procesos(api, admin_csrf, "Plato asignado", [torno["id"]])
        obrero = await crear_trabajador(api, admin_csrf, "Juan", daily_rate="120")
        await habilitar(api, admin_csrf, obrero["id"], torno["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await linea_de(api, admin_csrf, cotizacion, pieza, 20)

        proceso = (await procesos_de(api, cotizacion))[0]
        assert proceso["labor_cost"] is None

        respuesta = await api.post(
            f"{V2}/{cotizacion}/{PROCESOS}/{proceso['id']}/assign",
            json={"worker_id": obrero["id"]},
            headers=h(admin_csrf),
        )

        assert respuesta.status_code == 200, respuesta.text
        asignado = respuesta.json()["items"][0]
        assert asignado["worker_name"] == "Juan"
        # 20 piezas a 50 por jornada de 8 h son 3,2 h; a S/15 la hora, S/48.
        assert Decimal(asignado["final_hours"]) == Decimal("3.2")
        assert Decimal(asignado["labor_cost"]) == Decimal(48)

    async def test_asignar_a_quien_no_sabe_la_tecnica_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La regla de la correccion anterior sigue viva por este camino nuevo."""
        torno = await crear_tecnica(api, admin_csrf, "barrera-torno", name="Torno")
        otra = await crear_tecnica(api, admin_csrf, "barrera-otra", name="Otra")
        pieza = await pieza_con_procesos(api, admin_csrf, "Plato barrera", [torno["id"]])
        obrero = await crear_trabajador(api, admin_csrf, "Solo otra")
        await habilitar(api, admin_csrf, obrero["id"], otra["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await linea_de(api, admin_csrf, cotizacion, pieza, 6)
        proceso = (await procesos_de(api, cotizacion))[0]

        respuesta = await api.post(
            f"{V2}/{cotizacion}/{PROCESOS}/{proceso['id']}/assign",
            json={"worker_id": obrero["id"]},
            headers=h(admin_csrf),
        )

        assert respuesta.status_code == 422
        assert respuesta.json()["error"]["code"] == "V2_LABOR_TECHNIQUE_NOT_ALLOWED"

    async def test_quitar_al_trabajador_deja_el_proceso_en_pie(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La pieza sigue necesitando el torno aunque Juan ya no lo haga."""
        torno = await crear_tecnica(api, admin_csrf, "libre-torno", name="Torno")
        pieza = await pieza_con_procesos(api, admin_csrf, "Plato libre", [torno["id"]])
        obrero = await crear_trabajador(api, admin_csrf, "Juan libre")
        await habilitar(api, admin_csrf, obrero["id"], torno["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await linea_de(api, admin_csrf, cotizacion, pieza, 10)
        proceso = (await procesos_de(api, cotizacion))[0]
        await api.post(
            f"{V2}/{cotizacion}/{PROCESOS}/{proceso['id']}/assign",
            json={"worker_id": obrero["id"]},
            headers=h(admin_csrf),
        )

        respuesta = await api.delete(
            f"{V2}/{cotizacion}/{PROCESOS}/{proceso['id']}/assign", headers=h(admin_csrf)
        )

        assert respuesta.status_code == 200, respuesta.text
        libre = respuesta.json()["items"][0]
        assert libre["worker_id"] is None
        assert Decimal(libre["calculated_hours"]) == Decimal("1.6")

    async def test_cambiar_la_cantidad_mueve_las_horas_de_la_tarea_asignada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Dejar la tarea con la cantidad vieja daria el costo de otro pedido."""
        torno = await crear_tecnica(api, admin_csrf, "sync-torno", name="Torno")
        pieza = await pieza_con_procesos(api, admin_csrf, "Plato sync", [torno["id"]])
        obrero = await crear_trabajador(api, admin_csrf, "Juan sync")
        await habilitar(api, admin_csrf, obrero["id"], torno["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await linea_de(api, admin_csrf, cotizacion, pieza, 20)
        proceso = (await procesos_de(api, cotizacion))[0]
        await api.post(
            f"{V2}/{cotizacion}/{PROCESOS}/{proceso['id']}/assign",
            json={"worker_id": obrero["id"]},
            headers=h(admin_csrf),
        )

        await api.put(
            f"{V2}/{cotizacion}/products/{linea}", json={"quantity": 50}, headers=h(admin_csrf)
        )

        actualizado = (await procesos_de(api, cotizacion))[0]
        assert Decimal(actualizado["quantity"]) == Decimal(50)
        # 50 piezas a 50 por jornada son 8 h; a S/15, S/120.
        assert Decimal(actualizado["final_hours"]) == Decimal(8)
        assert Decimal(actualizado["labor_cost"]) == Decimal(120)


class TestAdicionales:
    async def test_el_adicional_suma_al_costo_de_produccion_y_al_real(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Como en la hoja «Cotizador V2» del Excel: B25 entra en B26 y en B27."""
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.post(
            f"{V2}/{cotizacion}/products",
            json={"product_name": "Pieza con empaque", "quantity": 4},
            headers=h(admin_csrf),
        )
        antes = (await api.get(f"{V2}/{cotizacion}/pricing")).json()

        concepto = await api.post(
            EXTRAS,
            json={"name": "Empaque especial", "unit": "servicio", "unit_cost": "25"},
            headers=h(admin_csrf),
        )
        assert concepto.status_code == 201, concepto.text
        respuesta = await api.post(
            f"{V2}/{cotizacion}/extras",
            json={"v2_extra_id": concepto.json()["id"], "quantity": "2"},
            headers=h(admin_csrf),
        )

        assert respuesta.status_code == 201, respuesta.text
        assert Decimal(respuesta.json()["extras_cost_total"]) == Decimal(50)
        despues = (await api.get(f"{V2}/{cotizacion}/pricing")).json()
        assert Decimal(despues["production_cost"]) - Decimal(antes["production_cost"]) == Decimal(
            50
        )
        assert Decimal(despues["real_cost"]) - Decimal(antes["real_cost"]) == Decimal(50)

    async def test_el_precio_del_maestro_se_congela_y_puede_sobrescribirse(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        concepto = (
            await api.post(
                EXTRAS,
                json={"name": "Molde especial", "unit_cost": "100"},
                headers=h(admin_csrf),
            )
        ).json()
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.post(
            f"{V2}/{cotizacion}/extras",
            json={"v2_extra_id": concepto["id"], "quantity": "1", "unit_cost": "150"},
            headers=h(admin_csrf),
        )

        # Sube el maestro: la cotizacion no se entera.
        await api.put(
            f"{EXTRAS}/{concepto['id']}",
            json={"expected_version": concepto["version"], "unit_cost": "900"},
            headers=h(admin_csrf),
        )

        fila = (await api.get(f"{V2}/{cotizacion}/extras")).json()["items"][0]
        assert Decimal(fila["unit_cost_snapshot"]) == Decimal(150)
        assert fila["unit_cost_is_override"] is True
        assert Decimal(fila["total_cost"]) == Decimal(150)

    async def test_un_adicional_de_una_pieza_va_al_costo_de_esa_pieza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)
        primera = (
            await api.post(
                f"{V2}/{cotizacion}/products",
                json={"product_name": "Con molde", "quantity": 2},
                headers=h(admin_csrf),
            )
        ).json()["id"]
        await api.post(
            f"{V2}/{cotizacion}/products",
            json={"product_name": "Sin molde", "quantity": 2},
            headers=h(admin_csrf),
        )
        concepto = (
            await api.post(
                EXTRAS, json={"name": "Molde de la pieza", "unit_cost": "60"}, headers=h(admin_csrf)
            )
        ).json()

        await api.post(
            f"{V2}/{cotizacion}/extras",
            json={
                "v2_extra_id": concepto["id"],
                "v2_quotation_product_id": primera,
                "quantity": "1",
            },
            headers=h(admin_csrf),
        )

        lineas = (await api.get(f"{V2}/{cotizacion}/pricing")).json()["lines"]
        con_molde = next(linea for linea in lineas if linea["line_id"] == primera)
        sin_molde = next(linea for linea in lineas if linea["line_id"] != primera)
        assert Decimal(con_molde["direct_cost"]) - Decimal(sin_molde["direct_cost"]) == Decimal(60)

    async def test_borrar_el_adicional_devuelve_el_costo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.post(
            f"{V2}/{cotizacion}/products",
            json={"product_name": "Pieza", "quantity": 1},
            headers=h(admin_csrf),
        )
        concepto = (
            await api.post(
                EXTRAS,
                json={"name": "Transporte especial", "unit_cost": "40"},
                headers=h(admin_csrf),
            )
        ).json()
        creada = await api.post(
            f"{V2}/{cotizacion}/extras",
            json={"v2_extra_id": concepto["id"], "quantity": "3"},
            headers=h(admin_csrf),
        )
        fila = creada.json()["items"][0]

        respuesta = await api.delete(
            f"{V2}/{cotizacion}/extras/{fila['id']}", headers=h(admin_csrf)
        )

        assert respuesta.status_code == 200, respuesta.text
        assert Decimal(respuesta.json()["extras_cost_total"]) == Decimal(0)
        precio = (await api.get(f"{V2}/{cotizacion}/pricing")).json()
        assert Decimal(precio["extras_cost"]) == Decimal(0)


class TestNoRomperLoQueYaHabia:
    async def test_una_tarea_sin_proceso_sigue_pudiendose_crear_y_leer(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El personal adicional apoya al pedido entero: no es proceso de ninguna pieza."""
        tecnica = await crear_tecnica(api, admin_csrf, "viejo-modo", name="Apoyo")
        obrero = await crear_trabajador(api, admin_csrf, "Refuerzo")
        await habilitar(api, admin_csrf, obrero["id"], tecnica["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        respuesta = await api.post(
            f"{V2}/{cotizacion}/labor",
            json={
                "worker_id": obrero["id"],
                "technique_id": tecnica["id"],
                "quantity": "10",
                "is_additional_personnel": True,
            },
            headers=h(admin_csrf),
        )

        assert respuesta.status_code == 201, respuesta.text
        assert respuesta.json()["is_additional_personnel"] is True
        # No aparece como proceso: no lo es.
        assert await procesos_de(api, cotizacion) == []

    async def test_una_cotizacion_emitida_no_admite_procesos_nuevos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        torno = await crear_tecnica(api, admin_csrf, "emitida-torno", name="Torno")
        pieza = await pieza_con_procesos(api, admin_csrf, "Plato emitido", [torno["id"]])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await linea_de(api, admin_csrf, cotizacion, pieza, 5)
        proceso = (await procesos_de(api, cotizacion))[0]
        await api.post(f"{V2}/{cotizacion}/cancel", json={}, headers=h(admin_csrf))

        borrado = await api.delete(
            f"{V2}/{cotizacion}/{PROCESOS}/{proceso['id']}", headers=h(admin_csrf)
        )
        anadido = await api.post(
            f"{V2}/{cotizacion}/{PROCESOS}",
            json={"v2_quotation_product_id": linea, "technique_id": torno["id"]},
            headers=h(admin_csrf),
        )

        assert borrado.status_code == 409
        assert anadido.status_code == 409
