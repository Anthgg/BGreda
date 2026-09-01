"""Fase 009G.1 — los dias de la cotizacion mueven los costos operativos.

El defecto: cambiar el ajuste de dias no cambiaba el precio. Los dias se
calculaban bien y se guardaban bien, pero el motor comercial sumaba los
maestros de otros gastos planos, sin mirar `calculation_type`, asi que anadir
una semana al pedido no costaba nada.

Aqui se fija la regla completa: alquiler y servicios por dia, administrativo
fijo, los dias de la cotizacion son el MAXIMO de sus lineas, y el costo de
quema no se entera de nada de esto.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head

OTHER_COSTS = "/api/v1/other-costs"

#: Los maestros reales del taller, con el tipo que declara cada uno.
ALQUILER_DIARIO = Decimal(110)
SERVICIOS_DIARIO = Decimal(10)
ADMINISTRATIVO = Decimal(200)
#: Lo que cuesta cada dia que se anade al pedido.
POR_DIA = ALQUILER_DIARIO + SERVICIOS_DIARIO


async def _sembrar_maestros(api: httpx.AsyncClient, csrf: str) -> None:
    """Crea los tres maestros con su semantica real, no todos como fijos."""
    maestros = (
        ("Alquiler del espacio x dia", ALQUILER_DIARIO, "PER_DAY"),
        ("Costo de servicios x dia", SERVICIOS_DIARIO, "PER_DAY"),
        ("Costo administrativo", ADMINISTRATIVO, "FIXED"),
    )
    for nombre, precio, tipo in maestros:
        respuesta = await api.post(
            OTHER_COSTS,
            json={"name": nombre, "unit_price": str(precio), "calculation_type": tipo},
            headers=head(csrf),
        )
        assert respuesta.status_code == 201, respuesta.text


def _con_ajuste(payload: dict[str, Any], ajuste: int) -> dict[str, Any]:
    return {
        **payload,
        "items": [{**item, "days_adjustment": ajuste} for item in payload["items"]],
    }


async def _previsualizar(
    api: httpx.AsyncClient, csrf: str, payload: dict[str, Any]
) -> dict[str, Any]:
    respuesta = await api.post(f"{BUILDER}/preview", json=payload, headers=head(csrf))
    assert respuesta.status_code == 200, respuesta.text
    return dict(respuesta.json())


def _dias(body: dict[str, Any]) -> int:
    return max(int(item["total_days"]) for item in body["items"])


def _esperado(dias: int) -> Decimal:
    return POR_DIA * Decimal(dias) + ADMINISTRATIVO


def _repartido(body: dict[str, Any]) -> Decimal:
    """El costo operativo tal y como queda repartido entre las lineas.

    Sirve tanto para una previsualizacion como para un borrador reabierto: el
    reparto se guarda en el plan de cada linea, mientras que el total de
    cabecera solo existe mientras se calcula.
    """
    return sum((Decimal(item["fixed_cost_allocation"]) for item in body["items"]), Decimal(0))


# ---------------------------------------------------------------------------
# La formula
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_costo_operativo_es_por_dia_mas_el_fijo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """RENTAL_COST_USES_TOTAL_DAYS y SERVICE_COST_USES_TOTAL_DAYS."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    body = await _previsualizar(api, admin_csrf, payload)
    dias = _dias(body)
    assert dias > 0

    assert Decimal(body["total_fixed_cost"]) == _esperado(dias)
    # Antes de 009G.1 esto valia 320 pasaran los dias que pasaran.
    assert Decimal(body["total_fixed_cost"]) > Decimal(320)


@pytest.mark.asyncio
async def test_el_administrativo_no_depende_de_los_dias(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ADMIN_COST_REMAINS_FIXED.

    Se compara la cotizacion con y sin ajuste: la diferencia tiene que ser
    exactamente los dias por su tarifa. Si el administrativo se hubiera colado
    en la multiplicacion, la diferencia traeria 200 de mas por dia.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    base = await _previsualizar(api, admin_csrf, _con_ajuste(payload, 0))
    mas_uno = await _previsualizar(api, admin_csrf, _con_ajuste(payload, 1))

    diferencia = Decimal(mas_uno["total_fixed_cost"]) - Decimal(base["total_fixed_cost"])
    assert diferencia == POR_DIA, "un dia mas cuesta 120: 110 de alquiler y 10 de servicios"


@pytest.mark.asyncio
async def test_un_maestro_por_pieza_no_entra_en_el_costo_operativo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PER_PIECE_EXCLUDED_FROM_009G1.

    PER_PIECE queda fuera de esta fase: su unico maestro esta desactivado y su
    formula no esta decidida. Se comprueba que no se cuela ni como fijo ni como
    diario, que es lo que pasaria si el motor lo tratara por descarte.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)
    sin_el = await _previsualizar(api, admin_csrf, payload)

    creado = await api.post(
        OTHER_COSTS,
        json={"name": "Factor por pieza", "unit_price": "3", "calculation_type": "PER_PIECE"},
        headers=head(admin_csrf),
    )
    assert creado.status_code == 201, creado.text

    con_el = await _previsualizar(api, admin_csrf, payload)
    assert Decimal(con_el["total_fixed_cost"]) == Decimal(sin_el["total_fixed_cost"])


# ---------------------------------------------------------------------------
# Los dias de la cotizacion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_los_dias_de_la_cotizacion_son_el_maximo_no_la_suma(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MULTIPRODUCT_TOTAL_DAYS_USES_MAX.

    Los dias de dos productos transcurren en el mismo periodo. Sumarlos
    cobraria dos veces un alquiler que solo se paga una vez.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    # Se alarga SOLO la primera linea, para que las dos difieran de verdad.
    items = [dict(item) for item in payload["items"]]
    items[0] = {**items[0], "waiting_days": 5}
    body = await _previsualizar(api, admin_csrf, {**payload, "items": items})

    dias_por_linea = [int(item["total_days"]) for item in body["items"]]
    assert len(dias_por_linea) == 2
    assert dias_por_linea[0] != dias_por_linea[1], "las lineas deben durar distinto"

    assert Decimal(body["total_fixed_cost"]) == _esperado(max(dias_por_linea))
    assert Decimal(body["total_fixed_cost"]) != _esperado(sum(dias_por_linea))


@pytest.mark.asyncio
async def test_los_dias_adicionales_mueven_el_costo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ADDITIONAL_DAYS_CHANGES_TOTAL_DAYS."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    sin_espera = await _previsualizar(api, admin_csrf, payload)
    items = [{**item, "waiting_days": 3} for item in payload["items"]]
    con_espera = await _previsualizar(api, admin_csrf, {**payload, "items": items})

    assert _dias(con_espera) == _dias(sin_espera) + 3
    diferencia = Decimal(con_espera["total_fixed_cost"]) - Decimal(sin_espera["total_fixed_cost"])
    assert diferencia == POR_DIA * 3


@pytest.mark.parametrize("ajuste", [0, 1, 2, -1])
@pytest.mark.asyncio
async def test_el_ajuste_de_dias_mueve_el_costo_operativo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession, ajuste: int
) -> None:
    """DAY_ADJUSTMENT_CHANGES_OPERATIONAL_COST, incluido el ajuste negativo."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    base = await _previsualizar(api, admin_csrf, _con_ajuste(payload, 0))
    caso = await _previsualizar(api, admin_csrf, _con_ajuste(payload, ajuste))

    assert _dias(caso) == _dias(base) + ajuste
    esperado = Decimal(base["total_fixed_cost"]) + POR_DIA * ajuste
    assert Decimal(caso["total_fixed_cost"]) == esperado


@pytest.mark.asyncio
async def test_el_ajuste_de_dias_no_toca_el_costo_de_quema(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DAY_ADJUSTMENT_DOES_NOT_CHANGE_FIRING_COST.

    La quema se cobra por hornada y ocupacion, no por calendario. Que el
    pedido dure dos dias mas no enciende el horno otra vez.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    quemas = []
    for ajuste in (0, 1, 2):
        body = await _previsualizar(api, admin_csrf, _con_ajuste(payload, ajuste))
        quemas.append([Decimal(item["firing_cost"]) for item in body["items"]])

    assert quemas[0] == quemas[1] == quemas[2]


@pytest.mark.asyncio
async def test_el_reparto_reconcilia_con_el_costo_operativo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """OPERATIONAL_COST_ALLOCATION_RECONCILES.

    El residuo de la cuantizacion va a la ultima linea; la suma tiene que dar
    el total exacto, sin un centimo perdido por el camino.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    body = await _previsualizar(api, admin_csrf, _con_ajuste(payload, 2))
    repartido = sum(Decimal(item["fixed_cost_allocation"]) for item in body["items"])
    assert repartido == Decimal(body["total_fixed_cost"])


# ---------------------------------------------------------------------------
# Guardar, reabrir y volver
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_ajuste_sobrevive_a_guardar_y_reabrir_dos_veces(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DAY_ADJUSTMENT_SAVE_REOPEN y DAY_ADJUSTMENT_SECOND_SAVE.

    Se comprueba sobre el REPARTO por linea, no sobre `total_fixed_cost`. Ese
    campo de cabecera es un eco de la previsualizacion y vuelve en cero al
    reabrir: no se guarda, se recalcula. Lo que si persiste —y lo que sostiene
    el precio— es `fixed_cost_allocation` de cada linea, cuya suma es el total
    operativo exacto.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    creada = await api.post(BUILDER, json=_con_ajuste(payload, 2), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    guardada = creada.json()
    dias = _dias(guardada)
    costo = _repartido(guardada)
    assert costo == _esperado(dias)

    for _ in range(2):
        reabierta = await api.get(f"{BUILDER}/{guardada['id']}")
        assert reabierta.status_code == 200
        vuelta = reabierta.json()
        assert all(item["days_adjustment"] == 2 for item in vuelta["items"])
        assert _dias(vuelta) == dias
        assert _repartido(vuelta) == costo

        regrabada = await api.put(
            f"{BUILDER}/{guardada['id']}",
            json={**_con_ajuste(payload, 2), "expected_updated_at": vuelta["updated_at"]},
            headers=head(admin_csrf),
        )
        assert regrabada.status_code == 200, regrabada.text
        guardada = regrabada.json()


@pytest.mark.asyncio
async def test_ir_y_volver_devuelve_el_costo_exacto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DAY_ADJUSTMENT_ROUNDTRIP: 0 → +1 → +2 → 0 vuelve al importe inicial."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    recorrido = []
    for ajuste in (0, 1, 2, 0):
        body = await _previsualizar(api, admin_csrf, _con_ajuste(payload, ajuste))
        recorrido.append((_dias(body), Decimal(body["total_fixed_cost"])))

    assert recorrido[0] == recorrido[3], "volver a cero tiene que devolver el importe exacto"
    assert recorrido[1][0] == recorrido[0][0] + 1
    assert recorrido[2][0] == recorrido[0][0] + 2
    assert recorrido[1][1] - recorrido[0][1] == POR_DIA
    assert recorrido[2][1] - recorrido[0][1] == POR_DIA * 2


@pytest.mark.asyncio
async def test_cambiar_el_ajuste_no_mueve_el_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DAY_ADJUSTMENT_NO_INVENTORY_MUTATION.

    Cotizar es estimar. Alargar un pedido en el Cotizador no puede descontar
    material ni registrar una quema que nadie ha encendido.
    """

    async def _inventario() -> tuple[int, int, int]:
        return (
            int(await db_session.scalar(text("SELECT count(*) FROM stock_movements")) or 0),
            int(await db_session.scalar(text("SELECT count(*) FROM stock_balances")) or 0),
            int(await db_session.scalar(text("SELECT count(*) FROM recipe_preparations")) or 0),
        )

    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)
    antes = await _inventario()

    for ajuste in (0, 1, 2, -1, 0):
        await _previsualizar(api, admin_csrf, _con_ajuste(payload, ajuste))

    await db_session.commit()
    assert await _inventario() == antes


@pytest.mark.asyncio
async def test_una_confirmada_no_se_mueve_si_cambian_las_tarifas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_COST_SNAPSHOT_IMMUTABLE.

    Subir el alquiler manana no puede reescribir un precio ya acordado.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await _sembrar_maestros(api, admin_csrf)

    creada = await api.post(BUILDER, json=_con_ajuste(payload, 1), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    borrador = creada.json()
    confirmada = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    congelada = confirmada.json()

    listado = await api.get(f"{OTHER_COSTS}?limit=50")
    assert listado.status_code == 200, listado.text
    alquiler = next(row for row in listado.json()["items"] if row["calculation_type"] == "PER_DAY")
    subida = await api.put(
        f"{OTHER_COSTS}/{alquiler['id']}",
        json={
            "name": alquiler["name"],
            "unit_price": "500",
            "calculation_type": "PER_DAY",
            "active": True,
        },
        headers=head(admin_csrf),
    )
    assert subida.status_code == 200, subida.text

    despues = await api.get(f"{BUILDER}/{borrador['id']}")
    assert despues.status_code == 200
    assert despues.json()["total_with_tax"] == congelada["total_with_tax"]
