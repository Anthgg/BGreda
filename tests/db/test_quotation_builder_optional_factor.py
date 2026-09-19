"""Fase 009K.3 — el factor de produccion deja de aplicarse por omision.

Hasta aqui el factor multiplicaba SIEMPRE el costo tecnico y no habia forma de
no aplicarlo: `production_factor = 0` lo rechazan el motor de precios, el CHECK
de configuracion y el esquema de entrada. Apagarlo con un cero era, literal,
imposible. Se apaga con una bandera, y entonces el multiplicador efectivo es
UNO —el neutro de una multiplicacion—, no cero.

Lo que estas pruebas vigilan, por orden de importancia:

1. que el factor NAZCA apagado, que es el cambio de negocio;
2. que encendido pueda tomar el valor manual de la cotizacion o, si falta,
   el valor de Configuracion;
3. que apagado el precio sea exactamente el de un factor uno, ni redondeado
   distinto ni con los costos fijos repartidos de otra manera;
4. que una cotizacion CONFIRMADA no cambie de importe porque alguien mueva la
   configuracion despues —el contrato de 009E, que esta fase no toca—;
5. que una cotizacion ANTERIOR a esta fase siga valiendo lo que valia. Esa es
   la unica que puede romperse en silencio: su bandera es NULL, y leer ese
   NULL como «apagado» le quitaria el factor tres a documentos firmados.

No existe ninguna regla automatica que elija 1, 2 o 3 por tramos: la auditoria
de esta fase lo dejo por escrito. Que aqui se prueben los valores 3 y 2 es
porque son lo que dice Configuracion, no un tramo.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quotations import Quotation
from tests.db.test_quotation_builder_api import (
    BUILDER,
    _complete_payload,
    cambiar_configuracion,
    head,
)

UNO = Decimal(1)
TRES = Decimal(3)


async def _preview(api: httpx.AsyncClient, csrf: str, payload: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(f"{BUILDER}/preview", json=payload, headers=head(csrf))
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


async def _crear(api: httpx.AsyncClient, csrf: str, payload: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(BUILDER, json=payload, headers=head(csrf))
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()


async def _leer(api: httpx.AsyncClient, csrf: str, quotation_id: int) -> dict[str, Any]:
    respuesta = await api.get(f"{BUILDER}/{quotation_id}", headers=head(csrf))
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


async def _confirmar(api: httpx.AsyncClient, csrf: str, borrador: dict[str, Any]) -> dict[str, Any]:
    respuesta = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# ---------------------------------------------------------------------------
# BF01-BF06: apagado por omision, encendido por decision
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_factor_nace_apagado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF01. FACTOR_COMMERCIAL_DEFAULT: DISABLED.

    Es el cambio de negocio de la fase. Antes esta misma peticion devolvia 3.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    body = await _preview(api, admin_csrf, payload)

    assert body["production_factor_enabled"] is False
    assert Decimal(body["production_factor"]) == UNO


@pytest.mark.asyncio
async def test_apagado_el_multiplicador_efectivo_es_uno(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF02. FACTOR_DISABLED_EFFECTIVE_MULTIPLIER: 1.

    Uno y no cero: cero haria que `price_line` respondiera 422, y ademas
    dejaria la base factorada en nada, con lo que los costos fijos no tendrian
    con que repartirse.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    body = await _preview(api, admin_csrf, payload)

    for item in body["items"]:
        assert Decimal(item["production_factor"]) == UNO
        assert Decimal(item["factored_cost"]) == Decimal(item["technical_cost"])


@pytest.mark.asyncio
async def test_encendido_toma_el_factor_de_configuracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF03 + BF04. FACTOR_ENABLED_SOURCE: CommercialSettings."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    body = await _preview(api, admin_csrf, {**payload, "production_factor_enabled": True})

    assert body["production_factor_enabled"] is True
    assert Decimal(body["production_factor"]) == TRES
    for item in body["items"]:
        assert Decimal(item["factored_cost"]) == Decimal(item["technical_cost"]) * TRES


@pytest.mark.asyncio
async def test_encendido_permite_factor_manual_por_cotizacion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """009K.4.2. El valor manual de la cotizacion gana al default."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    body = await _preview(
        api,
        admin_csrf,
        {**payload, "production_factor_enabled": True, "production_factor": "1.5"},
    )

    assert body["production_factor_enabled"] is True
    assert Decimal(body["production_factor"]) == Decimal("1.5")
    for item in body["items"]:
        assert Decimal(item["factored_cost"]) == Decimal(item["technical_cost"]) * Decimal("1.5")


@pytest.mark.asyncio
async def test_cambiar_el_factor_configurado_cambia_el_que_se_aplica(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF05. Con Configuracion en 2, encendido aplica 2.

    Esto NO es una regla de tramos que elija entre 1, 2 y 3. Es el unico
    valor configurado, leido de donde vive.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    await cambiar_configuracion(api, admin_csrf, production_factor_default="2")

    body = await _preview(api, admin_csrf, {**payload, "production_factor_enabled": True})

    assert Decimal(body["production_factor"]) == Decimal(2)


@pytest.mark.asyncio
async def test_apagado_ignora_el_factor_configurado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF06. Con Configuracion en 3 y la bandera apagada, se aplica 1."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    body = await _preview(api, admin_csrf, {**payload, "production_factor_enabled": False})

    assert Decimal(body["production_factor"]) == UNO
    assert (await api.get("/api/v1/settings/commercial")).json()[
        "production_factor_default"
    ] == "3.000000"


@pytest.mark.asyncio
async def test_encender_y_apagar_no_acumula_factor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF07. FACTOR_DOUBLE_APPLICATION: NO.

    Se hace el viaje entero —apagado, encendido, apagado— sobre el MISMO
    borrador guardado. Si el factor se aplicara sobre un costo ya factorado,
    la segunda vuelta daria nueve en vez de tres, y la tercera no volveria al
    punto de partida.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    apagado = await _crear(api, admin_csrf, payload)
    total_inicial = Decimal(apagado["quotation_gross_total"])

    encendido = await api.put(
        f"{BUILDER}/{apagado['id']}",
        json={
            **payload,
            "production_factor_enabled": True,
            "expected_updated_at": apagado["updated_at"],
        },
        headers=head(admin_csrf),
    )
    assert encendido.status_code == 200, encendido.text
    assert Decimal(encendido.json()["production_factor"]) == TRES

    de_vuelta = await api.put(
        f"{BUILDER}/{apagado['id']}",
        json={
            **payload,
            "production_factor_enabled": False,
            "expected_updated_at": encendido.json()["updated_at"],
        },
        headers=head(admin_csrf),
    )
    assert de_vuelta.status_code == 200, de_vuelta.text
    assert Decimal(de_vuelta.json()["production_factor"]) == UNO
    assert Decimal(de_vuelta.json()["quotation_gross_total"]) == total_inicial


# ---------------------------------------------------------------------------
# BF08-BF09: el borrador recuerda la decision
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("encendido", [False, True])
async def test_el_borrador_recuerda_si_el_factor_estaba_puesto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession, encendido: bool
) -> None:
    """BF08 + BF09. DRAFT_FACTOR_STATE_PERSISTED.

    Reabrir tiene que devolver la DECISION, no deducirla del importe: un
    factor guardado de 1 no distingue «no lo apliqué» de «apliqué uno».
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    creada = await _crear(api, admin_csrf, {**payload, "production_factor_enabled": encendido})
    reabierta = await _leer(api, admin_csrf, creada["id"])

    assert reabierta["production_factor_enabled"] is encendido
    assert Decimal(reabierta["production_factor"]) == (TRES if encendido else UNO)


# ---------------------------------------------------------------------------
# BF10-BF12: confirmar congela
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("encendido", [False, True])
async def test_confirmar_congela_la_decision_del_factor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession, encendido: bool
) -> None:
    """BF10 + BF11. CONFIRMED_FACTOR_SNAPSHOT_IMMUTABLE."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, {**payload, "production_factor_enabled": encendido})

    confirmada = await _confirmar(api, admin_csrf, borrador)

    assert confirmada["status"] == "CONFIRMED"
    assert confirmada["production_factor_enabled"] is encendido
    assert Decimal(confirmada["production_factor"]) == (TRES if encendido else UNO)


@pytest.mark.asyncio
async def test_cambiar_el_factor_despues_no_mueve_una_confirmada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF12. Se confirma con 3, Configuracion pasa a 2, el documento sigue en 3.

    Es el contrato de 009E y esta fase no lo toca. Se comprueba con el total,
    no solo con el factor: un numero congelado que no sostuviera el importe
    seria decorativo.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, {**payload, "production_factor_enabled": True})
    confirmada = await _confirmar(api, admin_csrf, borrador)
    total = Decimal(confirmada["quotation_gross_total"])

    await cambiar_configuracion(api, admin_csrf, production_factor_default="2")

    releida = await _leer(api, admin_csrf, confirmada["id"])
    assert Decimal(releida["production_factor"]) == TRES
    assert releida["production_factor_enabled"] is True
    assert Decimal(releida["quotation_gross_total"]) == total


# ---------------------------------------------------------------------------
# BF13-BF15: lo que no debe moverse
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_cotizacion_anterior_a_la_fase_sigue_con_su_factor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF13. HISTORICAL_CONFIRMED_QUOTATIONS_REWRITTEN: NO.

    Se fabrica el estado exacto de antes de 0026: una confirmada que se
    calculo con factor tres y cuya bandera esta en NULL, porque la columna no
    existia cuando se emitio. Leer ese NULL como «apagado» le quitaria el
    factor a un documento firmado; la lectura correcta es mirar el factor que
    la propia cotizacion guardo.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, {**payload, "production_factor_enabled": True})
    confirmada = await _confirmar(api, admin_csrf, borrador)
    total = Decimal(confirmada["quotation_gross_total"])

    # El unico modo de tener una fila anterior a la fase es no tener bandera.
    await db_session.execute(
        update(Quotation)
        .where(Quotation.id == confirmada["id"])
        .values(production_factor_enabled=None, kiln_mode=None)
    )
    await db_session.commit()

    historica = await _leer(api, admin_csrf, confirmada["id"])
    assert historica["production_factor_enabled"] is True, "se lee del factor guardado"
    assert Decimal(historica["production_factor"]) == TRES
    assert Decimal(historica["quotation_gross_total"]) == total


@pytest.mark.asyncio
async def test_un_borrador_anterior_a_la_fase_conserva_su_factor_al_confirmar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF13 bis. EXISTING_DRAFT_FACTOR_PRESERVED.

    Confirmar recalcula para comprobar que nada cambio por debajo. Si en ese
    recalculo la bandera NULL se leyera como «apagado», el documento se
    congelaria con un importe distinto del que el usuario vio — y justo en el
    acto de firmarlo.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    borrador = await _crear(api, admin_csrf, {**payload, "production_factor_enabled": True})
    total_borrador = Decimal(borrador["quotation_gross_total"])

    await db_session.execute(
        update(Quotation)
        .where(Quotation.id == borrador["id"])
        .values(production_factor_enabled=None, kiln_mode=None)
    )
    await db_session.commit()

    reabierto = await _leer(api, admin_csrf, borrador["id"])
    confirmada = await _confirmar(api, admin_csrf, reabierto)

    assert Decimal(confirmada["production_factor"]) == TRES
    assert Decimal(confirmada["quotation_gross_total"]) == total_borrador


@pytest.mark.asyncio
async def test_el_factor_comercial_legado_no_se_toca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BF14. COMMERCIAL_FACTOR_LEGACY_CHANGED: NO.

    `quotations.commercial_factor` es OTRO campo —sale de
    `default_quotation_factor` y pertenece al flujo antiguo— y esta fase no lo
    reutiliza como bandera ni lo pone a cero. Su CHECK `> 0` sigue vigente.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    creada = await _crear(api, admin_csrf, payload)

    fila = (
        await db_session.execute(
            text(
                "SELECT commercial_factor, commercial_factor_default_snapshot,"
                " production_factor_enabled, kiln_mode"
                " FROM quotations WHERE id = :qid"
            ),
            {"qid": creada["id"]},
        )
    ).one()
    assert fila[0] > 0, "el factor legado sigue siendo positivo"
    assert fila[0] == fila[1]
    assert fila[2] is False, "la bandera nueva vive en su propia columna"
    assert fila[3] == "TOGETHER"


@pytest.mark.asyncio
async def test_el_factor_no_se_puede_apagar_con_un_cero(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El cero sigue prohibido, que es la razon de que exista la bandera.

    Si algun dia alguien decide «simplificar» permitiendo `production_factor:
    0`, esta prueba lo dira: no es un detalle de validacion, es que un cero
    multiplicando el costo tecnico deja la base factorada en nada y con ella
    el reparto de costos fijos.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    respuesta = await api.post(
        f"{BUILDER}/preview",
        json={**payload, "production_factor": "0"},
        headers=head(admin_csrf),
    )

    assert respuesta.status_code == 422, respuesta.text
