"""Fase 010P W1 — las reglas nuevas del motor, recorridas por la API real.

Las pruebas unitarias (`tests/unit/test_quoter_v2_rules_010p.py`) fijan la
aritmetica. Estas comprueban que el backend la aplica de punta a punta: lo que
se congela al crear, lo que se recalcula al editar, lo que se sugiere sin
tocar un total y lo que aceptar «por mayor» cambia (y lo que NO cambia).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quoter_v2_lifecycle_api import cliente, cotizacion_completa, h, preview
from tests.db.v2_capacidades import habilitar
from tests.db.v2_escenario_base import (
    KILNS,
    TECHNIQUES,
    V2,
    V2_SETTINGS,
    WORKERS,
    preparar_configuracion,
    preparar_maestros,
)


def D(valor: Any) -> Decimal:
    return Decimal(str(valor))


async def _trabajador(
    api: httpx.AsyncClient,
    csrf: str,
    nombre: str,
    tipo: str,
    jornal: str,
    jornada: str | None = None,
) -> int:
    payload: dict[str, Any] = {"name": nombre, "worker_type": tipo, "daily_rate": jornal}
    if jornada is not None:
        payload["workday_hours"] = jornada
    r = await api.post(WORKERS, json=payload, headers=h(csrf))
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def _tecnica(api: httpx.AsyncClient, csrf: str, code: str) -> int:
    r = await api.post(
        TECHNIQUES,
        json={"code": code, "name": f"Tecnica {code}", "default_capacity_per_workday": "50"},
        headers=h(csrf),
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def _ajustes(api: httpx.AsyncClient, csrf: str, **campos: Any) -> dict[str, Any]:
    actual = (await api.get(V2_SETTINGS)).json()["settings"]
    r = await api.put(
        V2_SETTINGS, json={"expected_version": actual["version"], **campos}, headers=h(csrf)
    )
    assert r.status_code == 200, r.text
    return dict(r.json()["settings"])


async def _cotizacion(
    api: httpx.AsyncClient, csrf: str, customer_id: int, tipo: str = "RETAIL"
) -> int:
    r = await api.post(
        V2,
        json={"name": "Pedido 010P", "customer_id": customer_id, "production_type": tipo},
        headers=h(csrf),
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def _linea(
    api: httpx.AsyncClient,
    csrf: str,
    qid: int,
    pasta_id: int,
    cantidad: int,
    minutos: str | None,
    moldes: int = 1,
    nombre: str = "Plato",
) -> int:
    payload: dict[str, Any] = {
        "product_name": nombre,
        "quantity": cantidad,
        "length_cm": "20",
        "width_cm": "20",
        "height_cm": "4",
        "body_material_id": pasta_id,
        "body_unit_weight": "450",
        "mold_count": moldes,
    }
    if minutos is not None:
        payload["production_time_per_unit_minutes"] = minutos
    r = await api.post(f"{V2}/{qid}/products", json=payload, headers=h(csrf))
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def _proceso(
    api: httpx.AsyncClient, csrf: str, qid: int, line_id: int, technique_id: int
) -> dict[str, Any]:
    r = await api.post(
        f"{V2}/{qid}/processes",
        json={"v2_quotation_product_id": line_id, "technique_id": technique_id},
        headers=h(csrf),
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _asignar(
    api: httpx.AsyncClient, csrf: str, qid: int, process_id: int, worker_id: int
) -> dict[str, Any]:
    r = await api.post(
        f"{V2}/{qid}/processes/{process_id}/assign", json={"worker_id": worker_id}, headers=h(csrf)
    )
    assert r.status_code == 200, r.text
    return next(p for p in r.json()["items"] if p["id"] == process_id)


async def _procesos(api: httpx.AsyncClient, qid: int) -> dict[int, dict[str, Any]]:
    r = await api.get(f"{V2}/{qid}/processes")
    assert r.status_code == 200, r.text
    return {int(p["id"]): p for p in r.json()["items"]}


async def _precio(api: httpx.AsyncClient, qid: int) -> dict[str, Any]:
    r = await api.get(f"{V2}/{qid}/pricing")
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _base(api: httpx.AsyncClient, csrf: str) -> dict[str, int]:
    kiln_id = await preparar_configuracion(api, csrf)
    pasta_id, interno_id, technique_id = await preparar_maestros(api, csrf)
    return {
        "kiln_id": kiln_id,
        "pasta_id": pasta_id,
        "interno_id": interno_id,
        "technique_id": technique_id,
        "customer_id": await cliente(api, csrf),
    }


# ---------------------------------------------------------------------------
# Horno: las tarifas se congelan al CREAR
# ---------------------------------------------------------------------------
async def test_tarifas_del_horno_se_congelan_al_crear(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    base = await _base(api, admin_csrf)
    qid = await _cotizacion(api, admin_csrf, base["customer_id"])

    sql = text(
        "SELECT gas_cost_low_snapshot, gas_cost_high_snapshot,"
        " commercial_rate_low_snapshot, commercial_rate_high_snapshot"
        " FROM v2_quotations WHERE id = :id"
    )
    congeladas = (await db_session.execute(sql, {"id": qid})).one()
    assert tuple(map(D, congeladas)) == (D(35), D(70), D(200), D(250))

    # La casa cambia sus tarifas despues: lo congelado no se mueve.
    for tipo, gas, tarifa in (("LOW", "99", "999"), ("HIGH", "98", "998")):
        r = await api.put(
            f"{V2_SETTINGS}/kiln-rates/{base['kiln_id']}/{tipo}",
            json={"gas_cost": gas, "external_rate": tarifa},
            headers=h(admin_csrf),
        )
        assert r.status_code == 200, r.text
    assert (await api.get(f"{V2}/{qid}/firing")).status_code == 200
    await db_session.rollback()
    despues = (await db_session.execute(sql, {"id": qid})).one()
    assert tuple(map(D, despues)) == (D(35), D(70), D(200), D(250))

    # Una cotizacion nueva nace con las tarifas nuevas.
    nueva = await _cotizacion(api, admin_csrf, base["customer_id"])
    await db_session.rollback()
    otra = (await db_session.execute(sql, {"id": nueva})).one()
    assert tuple(map(D, otra)) == (D(99), D(98), D(999), D(998))


# ---------------------------------------------------------------------------
# Tiempo, espacio por hora y tiempo pasivo
# ---------------------------------------------------------------------------
async def test_espacio_por_hora_activa_acuerdo_y_pasivo_solo_sugerido(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    base = await _base(api, admin_csrf)
    qid = await _cotizacion(api, admin_csrf, base["customer_id"])
    # 10 piezas x 30 min con 1 molde = 300 min; 20 x 12 min con 2 moldes =
    # 10 ciclos = 120 min. En paralelo: el pedido dura 300 min = 5 h.
    await _linea(api, admin_csrf, qid, base["pasta_id"], 10, "30")
    await _linea(api, admin_csrf, qid, base["pasta_id"], 20, "12", moldes=2, nombre="Taza")

    precio = await _precio(api, qid)
    assert precio["pricing_rules_version"] == 2
    assert D(precio["active_production_minutes"]) == D(300)
    assert D(precio["active_production_hours"]) == D(5)
    # 140 por dia / 8 h = 17,5 por hora congelado al crear.
    assert D(precio["space_cost_per_hour_snapshot"]) == D("17.5")
    assert D(precio["space_cost"]) == D("87.5")
    assert D(precio["passive_space_suggestion"]) == 0

    lineas = (await api.get(f"{V2}/{qid}/products")).json()["items"]
    assert sorted(D(fila["line_active_minutes"]) for fila in lineas) == [D(120), D(300)]
    assert sorted(fila["cycles"] for fila in lineas) == [10, 10]
    assert sum(D(fila["space_cost"]) for fila in precio["lines"]) == D(precio["space_cost"])

    # Acuerdo por hora de ESTA cotizacion y tiempo pasivo.
    r = await api.put(
        f"{V2}/{qid}/pricing",
        json={"space_cost_per_hour_override": "20", "passive_time_hours": "4"},
        headers=h(admin_csrf),
    )
    assert r.status_code == 200, r.text
    acordado = r.json()
    assert D(acordado["effective_space_cost_per_hour"]) == D(20)
    assert D(acordado["space_cost"]) == D(100)
    # El pasivo solo se sugiere: 4 h x 20 = 80 que NO entran en ningun costo.
    assert D(acordado["passive_space_suggestion"]) == D(80)
    assert "V2_PRICING_PASSIVE_TIME_AVAILABLE" in acordado["warnings"]
    assert D(acordado["production_cost"]) - D(precio["production_cost"]) == D("12.5")

    # Nulo retira el acuerdo: vuelve lo congelado.
    r = await api.put(
        f"{V2}/{qid}/pricing",
        json={"space_cost_per_hour_override": None},
        headers=h(admin_csrf),
    )
    assert r.status_code == 200, r.text
    assert D(r.json()["effective_space_cost_per_hour"]) == D("17.5")
    assert D(r.json()["space_cost"]) == D("87.5")

    negativo = await api.put(
        f"{V2}/{qid}/pricing",
        json={"space_cost_per_hour_override": "-1"},
        headers=h(admin_csrf),
    )
    assert negativo.status_code == 422


# ---------------------------------------------------------------------------
# Personal: interno cero, externo comercial y real, brecha y reparto
# ---------------------------------------------------------------------------
async def test_externos_comercial_real_brecha_y_snapshot_de_tarifa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    base = await _base(api, admin_csrf)
    tec = base["technique_id"]
    externo_a = await _trabajador(api, admin_csrf, "Externo A", "EXTERNAL", "150", "8")
    externo_b = await _trabajador(api, admin_csrf, "Externo B", "EXTERNAL", "240", "6")
    for trabajador in (base["interno_id"], externo_a, externo_b):
        await habilitar(api, admin_csrf, trabajador, tec)

    qid = await _cotizacion(api, admin_csrf, base["customer_id"])
    plato = await _linea(api, admin_csrf, qid, base["pasta_id"], 10, "30")
    taza = await _linea(api, admin_csrf, qid, base["pasta_id"], 20, "12", moldes=2, nombre="Taza")
    fuente = await _linea(api, admin_csrf, qid, base["pasta_id"], 5, "10", nombre="Fuente")

    p_plato = await _proceso(api, admin_csrf, qid, plato, tec)
    p_taza = await _proceso(api, admin_csrf, qid, taza, tec)
    p_fuente = await _proceso(api, admin_csrf, qid, fuente, tec)
    await _asignar(api, admin_csrf, qid, p_plato["id"], externo_a)
    await _asignar(api, admin_csrf, qid, p_taza["id"], externo_b)
    await _asignar(api, admin_csrf, qid, p_fuente["id"], base["interno_id"])

    precio = await _precio(api, qid)
    # 5 h activas. A: 5 x 150/8 = 93,75 al cliente; 1 jornal = 150 real.
    #              B: 5 x 240/6 = 200 al cliente; 1 jornal = 240 real.
    assert D(precio["commercial_external_labor_cost"]) == D("293.75")
    assert D(precio["real_external_labor_cost"]) == D(390)
    assert D(precio["labor_cost_gap"]) == D("96.25")
    assert D(precio["labor_cost"]) == D("293.75")
    por_externo = {fila["worker_id"]: fila for fila in precio["external_workers"]}
    assert set(por_externo) == {externo_a, externo_b}
    assert por_externo[externo_a]["days_paid"] == 1
    assert D(por_externo[externo_a]["hourly_equivalent"]) == D("18.75")
    assert D(por_externo[externo_a]["daily_rate"]) == D(150)
    assert por_externo[externo_a]["name"] == "Externo A"

    # El interno no cuesta y ninguna tarea lleva costo propio.
    tareas = (await api.get(f"{V2}/{qid}/labor")).json()["items"]
    assert len(tareas) == 3
    assert all(D(tarea["labor_cost"]) == 0 for tarea in tareas)

    # Reparto por minutos: la suma de las lineas es EXACTAMENTE el total.
    for campo, total in (
        ("external_commercial_cost", "commercial_external_labor_cost"),
        ("external_real_cost", "real_external_labor_cost"),
    ):
        assert sum(D(fila[campo]) for fila in precio["lines"]) == D(precio[total])
    # La brecha va al costo real, no al de produccion (ni a la quema).
    diferencia_quema = D(precio["gas_cost"]) - D(precio["firing_commercial_cost"])
    assert D(precio["real_cost"]) - D(precio["production_cost"]) == (
        diferencia_quema + D(precio["labor_cost_gap"])
    )

    # Cambiar el jornal del maestro despues NO mueve lo congelado.
    ficha = next(w for w in (await api.get(WORKERS)).json()["items"] if w["id"] == externo_a)
    r = await api.put(
        f"{WORKERS}/{externo_a}",
        json={"expected_version": ficha["version"], "daily_rate": "300"},
        headers=h(admin_csrf),
    )
    assert r.status_code == 200, r.text
    # Y una segunda tarea de la misma persona reutiliza su foto, no la nueva.
    p_extra = await _proceso(api, admin_csrf, qid, fuente, await _tecnica(api, admin_csrf, "T2"))
    await habilitar(api, admin_csrf, externo_a, p_extra["technique_id"])
    await _asignar(api, admin_csrf, qid, p_extra["id"], externo_a)

    despues = await _precio(api, qid)
    assert D(despues["commercial_external_labor_cost"]) == D("293.75")
    assert D(despues["real_external_labor_cost"]) == D(390)
    fotos = (
        await db_session.execute(
            text(
                "SELECT worker_id, daily_rate_snapshot, workday_hours_snapshot"
                " FROM v2_quotation_workers WHERE v2_quotation_id = :id ORDER BY worker_id"
            ),
            {"id": qid},
        )
    ).all()
    por_trabajador = {int(w): (D(d), None if j is None else D(j)) for w, d, j in fotos}
    # Una foto por persona DISTINTA, creada en su primera asignacion.
    assert len(fotos) == 3
    assert por_trabajador[externo_a] == (D(150), D(8))
    assert por_trabajador[externo_b] == (D(240), D(6))
    tareas_a = (
        await db_session.execute(
            text(
                "SELECT worker_type_snapshot, daily_rate_snapshot, workday_hours_snapshot,"
                " hourly_rate_snapshot FROM v2_quotation_labor"
                " WHERE v2_quotation_id = :id AND worker_id = :worker"
                " ORDER BY id"
            ),
            {"id": qid, "worker": externo_a},
        )
    ).all()
    assert len(tareas_a) == 2
    assert all(
        (tipo, D(jornal), D(jornada), D(por_hora)) == ("EXTERNAL", D(150), D(8), D("18.75"))
        for tipo, jornal, jornada, por_hora in tareas_a
    )


# ---------------------------------------------------------------------------
# Administracion por tipo de pedido
# ---------------------------------------------------------------------------
async def test_administracion_cero_en_por_menor_y_se_reaplica_al_cambiar_tipo(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    base = await _base(api, admin_csrf)
    qid = await _cotizacion(api, admin_csrf, base["customer_id"], "RETAIL")
    await _linea(api, admin_csrf, qid, base["pasta_id"], 10, "30")
    assert D((await _precio(api, qid))["administration_cost"]) == 0

    r = await api.put(f"{V2}/{qid}", json={"production_type": "WHOLESALE"}, headers=h(admin_csrf))
    assert r.status_code == 200, r.text
    assert D((await _precio(api, qid))["administration_cost"]) == D(200)

    r = await api.put(f"{V2}/{qid}", json={"production_type": "RETAIL"}, headers=h(admin_csrf))
    assert r.status_code == 200, r.text
    assert D((await _precio(api, qid))["administration_cost"]) == 0

    por_mayor = await _cotizacion(api, admin_csrf, base["customer_id"], "WHOLESALE")
    await _linea(api, admin_csrf, por_mayor, base["pasta_id"], 10, "30")
    assert D((await _precio(api, por_mayor))["administration_cost"]) == D(200)


# ---------------------------------------------------------------------------
# Umbral por mayor: sugerir, rechazar, aceptar
# ---------------------------------------------------------------------------
async def test_umbral_sugiere_y_rechazar_solo_lo_anota(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    base = await _base(api, admin_csrf)
    await _ajustes(api, admin_csrf, wholesale_quantity_threshold=50)
    qid = await _cotizacion(api, admin_csrf, base["customer_id"])
    await _linea(api, admin_csrf, qid, base["pasta_id"], 30, "5")
    precio = await _precio(api, qid)
    assert precio["wholesale_threshold"] == 50
    assert precio["total_units"] == 30
    assert precio["wholesale_suggested"] is False

    # El umbral es del pedido ENTERO: 30 + 30 = 60 > 50.
    await _linea(api, admin_csrf, qid, base["pasta_id"], 30, "5", nombre="Taza")
    precio = await _precio(api, qid)
    assert precio["wholesale_suggested"] is True
    assert "V2_WHOLESALE_THRESHOLD_EXCEEDED" in precio["warnings"]

    # Cambiar el umbral de la casa despues no toca el congelado.
    await _ajustes(api, admin_csrf, wholesale_quantity_threshold=500)
    assert (await _precio(api, qid))["wholesale_threshold"] == 50

    r = await api.post(f"{V2}/{qid}/decline-wholesale-suggestion", headers=h(admin_csrf))
    assert r.status_code == 200, r.text
    assert r.json()["wholesale_suggestion_declined_at"] is not None
    assert r.json()["production_type"] == "RETAIL"
    precio = await _precio(api, qid)
    assert precio["wholesale_suggested"] is False
    assert precio["wholesale_suggestion_declined"] is True
    assert D(precio["administration_cost"]) == 0


async def test_aceptar_por_mayor_aplica_defaults_y_respeta_lo_manual(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    base = await _base(api, admin_csrf)
    tec, tec_rara = base["technique_id"], await _tecnica(api, admin_csrf, "RARA")
    interno = base["interno_id"]
    otro_interno = await _trabajador(api, admin_csrf, "Interno manual", "INTERNAL", "100")
    externo = await _trabajador(api, admin_csrf, "Externo por mayor", "EXTERNAL", "160", "8")
    await habilitar(api, admin_csrf, interno, tec, tec_rara)
    await habilitar(api, admin_csrf, otro_interno, tec)
    await habilitar(api, admin_csrf, externo, tec)  # NO sabe la tecnica rara

    horno = await api.post(
        KILNS,
        json={"name": "Horno grande 010P", "capacity_volume_cm3": "90000"},
        headers=h(admin_csrf),
    )
    assert horno.status_code == 201, horno.text
    horno_mayor = int(horno.json()["id"])
    for tipo, gas, tarifa in (("LOW", "40", "80"), ("HIGH", "120", "160")):
        r = await api.put(
            f"{V2_SETTINGS}/kiln-rates/{horno_mayor}/{tipo}",
            json={"gas_cost": gas, "external_rate": tarifa},
            headers=h(admin_csrf),
        )
        assert r.status_code == 200, r.text
    await _ajustes(
        api,
        admin_csrf,
        wholesale_kiln_id=horno_mayor,
        retail_default_worker_id=interno,
        wholesale_default_worker_id=externo,
    )

    qid = await _cotizacion(api, admin_csrf, base["customer_id"])
    lineas = [
        await _linea(api, admin_csrf, qid, base["pasta_id"], 10, "30", nombre=f"P{i}")
        for i in range(3)
    ]
    por_defecto = await _proceso(api, admin_csrf, qid, lineas[0], tec)
    manual = await _proceso(api, admin_csrf, qid, lineas[1], tec)
    rara = await _proceso(api, admin_csrf, qid, lineas[2], tec_rara)
    # El sistema asigno al interno por defecto de por menor.
    for proceso in (por_defecto, manual, rara):
        assert proceso["worker_id"] == interno
        assert proceso["assignment_origin"] == "DEFAULT"
    # Una persona cambia uno a mano: pasa a MANUAL.
    elegido = await _asignar(api, admin_csrf, qid, manual["id"], otro_interno)
    assert elegido["assignment_origin"] == "MANUAL"

    r = await api.post(f"{V2}/{qid}/apply-wholesale-defaults", headers=h(admin_csrf))
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["quotation"]["production_type"] == "WHOLESALE"
    assert "V2_WHOLESALE_MANUAL_WORKERS_KEPT" in cuerpo["warnings"]
    assert "V2_WHOLESALE_DEFAULT_WORKER_LACKS_TECHNIQUE" in cuerpo["warnings"]

    procesos = await _procesos(api, qid)
    assert procesos[por_defecto["id"]]["worker_id"] == externo
    assert procesos[por_defecto["id"]]["assignment_origin"] == "DEFAULT"
    assert procesos[manual["id"]]["worker_id"] == otro_interno
    assert procesos[manual["id"]]["assignment_origin"] == "MANUAL"
    assert procesos[rara["id"]]["worker_id"] == interno

    precio = await _precio(api, qid)
    assert D(precio["administration_cost"]) == D(200)
    assert [w["worker_id"] for w in precio["external_workers"]] == [externo]
    fila = (
        await db_session.execute(
            text(
                "SELECT kiln_id, gas_cost_low_snapshot, commercial_rate_high_snapshot"
                " FROM v2_quotations WHERE id = :id"
            ),
            {"id": qid},
        )
    ).one()
    assert (int(fila[0]), D(fila[1]), D(fila[2])) == (horno_mayor, D(40), D(160))


async def test_aceptar_sin_externo_por_defecto_cambia_el_tipo_y_avisa(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    base = await _base(api, admin_csrf)
    await habilitar(api, admin_csrf, base["interno_id"], base["technique_id"])
    qid = await _cotizacion(api, admin_csrf, base["customer_id"])
    linea = await _linea(api, admin_csrf, qid, base["pasta_id"], 10, "30")
    proceso = await _proceso(api, admin_csrf, qid, linea, base["technique_id"])
    # Sin trabajador por defecto de por menor, nadie queda asignado solo.
    assert proceso["worker_id"] is None
    await _asignar(api, admin_csrf, qid, proceso["id"], base["interno_id"])

    r = await api.post(f"{V2}/{qid}/apply-wholesale-defaults", headers=h(admin_csrf))
    assert r.status_code == 200, r.text
    assert r.json()["quotation"]["production_type"] == "WHOLESALE"
    assert "V2_WHOLESALE_EXTERNAL_WORKER_MISSING" in r.json()["warnings"]
    assert (await _procesos(api, qid))[proceso["id"]]["worker_id"] == base["interno_id"]


async def test_trabajador_por_defecto_inactivo_no_se_aplica(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    base = await _base(api, admin_csrf)
    interno = base["interno_id"]
    await habilitar(api, admin_csrf, interno, base["technique_id"])
    await _ajustes(api, admin_csrf, retail_default_worker_id=interno)
    ficha = next(w for w in (await api.get(WORKERS)).json()["items"] if w["id"] == interno)
    r = await api.put(
        f"{WORKERS}/{interno}",
        json={"expected_version": ficha["version"], "active": False},
        headers=h(admin_csrf),
    )
    assert r.status_code == 200, r.text
    # La referencia se conserva en la configuracion...
    assert (await api.get(V2_SETTINGS)).json()["settings"]["retail_default_worker_id"] == interno

    qid = await _cotizacion(api, admin_csrf, base["customer_id"])
    linea = await _linea(api, admin_csrf, qid, base["pasta_id"], 10, "30")
    proceso = await _proceso(api, admin_csrf, qid, linea, base["technique_id"])
    # ...pero no se aplica.
    assert proceso["worker_id"] is None


async def test_ajustes_rechazan_un_trabajador_por_defecto_del_tipo_equivocado(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    base = await _base(api, admin_csrf)
    actual = (await api.get(V2_SETTINGS)).json()["settings"]
    r = await api.put(
        V2_SETTINGS,
        json={
            "expected_version": actual["version"],
            "wholesale_default_worker_id": base["interno_id"],
        },
        headers=h(admin_csrf),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "V2_DEFAULT_WORKER_INVALID"


async def test_un_trabajador_por_defecto_cuyo_tipo_cambio_no_se_aplica(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    base = await _base(api, admin_csrf)
    interno = base["interno_id"]
    await habilitar(api, admin_csrf, interno, base["technique_id"])
    await _ajustes(api, admin_csrf, retail_default_worker_id=interno)

    ficha = next(w for w in (await api.get(WORKERS)).json()["items"] if w["id"] == interno)
    cambio = await api.put(
        f"{WORKERS}/{interno}",
        json={"expected_version": ficha["version"], "worker_type": "EXTERNAL"},
        headers=h(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    qid = await _cotizacion(api, admin_csrf, base["customer_id"])
    linea = await _linea(api, admin_csrf, qid, base["pasta_id"], 10, "30")
    proceso = await _proceso(api, admin_csrf, qid, linea, base["technique_id"])
    assert proceso["worker_id"] is None
    tareas = (await api.get(f"{V2}/{qid}/labor")).json()["items"]
    assert tareas == []


# ---------------------------------------------------------------------------
# Emision: sin tiempo por pieza no se emite; los dias ya no bloquean
# ---------------------------------------------------------------------------
async def test_linea_sin_tiempo_bloquea_la_emision_y_los_dias_no(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    datos = await cotizacion_completa(api, admin_csrf)
    qid = datos["id"]
    resumen = await preview(api, qid)
    assert resumen["can_confirm"], resumen["blockers"]

    plan = await api.put(
        f"{V2}/{qid}/planning", json={"effective_work_days": None}, headers=h(admin_csrf)
    )
    if plan.status_code == 200:
        codigos = {b["code"] for b in (await preview(api, qid))["blockers"]}
        assert "V2_CONFIRM_WORK_DAYS_REQUIRED" not in codigos

    r = await api.put(
        f"{V2}/{qid}/products/{datos['lines'][0]}",
        json={"production_time_per_unit_minutes": None},
        headers=h(admin_csrf),
    )
    assert r.status_code == 200, r.text
    resumen = await preview(api, qid)
    assert not resumen["can_confirm"]
    assert "V2_CONFIRM_LINE_TIME_REQUIRED" in {b["code"] for b in resumen["blockers"]}

    malo = await api.put(
        f"{V2}/{qid}/products/{datos['lines'][0]}",
        json={"mold_count": 0},
        headers=h(admin_csrf),
    )
    assert malo.status_code == 422
