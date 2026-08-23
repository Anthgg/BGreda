"""Pruebas de integracion de hornos, tarifas y hojas de quema contra PostgreSQL.

El catalogo oficial lo siembra la migracion 0007, pero estas pruebas crean sus
propios hornos: asi comprueban tambien el alta y no dependen de que el seed siga
teniendo los mismos identificadores.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditEvent
from app.models.inventory import StockMovement
from app.models.sequence import DocumentSequenceIssue
from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

KILNS = "/api/v1/kilns"
FIRINGS = "/api/v1/firings"

#: Tramos del documento funcional, para sembrarlos en los hornos de prueba.
FACTORES_CHICO = [
    (1, 10, "2.0"),
    (11, 20, "1.9"),
    (21, 30, "1.8"),
    (31, 40, "1.7"),
    (41, 50, "1.6"),
    (51, 60, "1.4"),
    (61, 70, "1.3"),
    (71, 80, "1.2"),
    (81, 90, "1.1"),
    (91, 100, "1.0"),
]
FACTORES_GRANDE = [
    (1, 10, "3.0"),
    (11, 20, "2.8"),
    (21, 30, "2.6"),
    (31, 40, "2.3"),
    (41, 50, "2.1"),
    (51, 60, "1.9"),
    (61, 70, "1.7"),
    (71, 80, "1.4"),
    (81, 90, "1.2"),
    (91, 100, "1.0"),
]


def head(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


async def crear_horno(
    api: httpx.AsyncClient,
    csrf: str,
    session: AsyncSession,
    *,
    nombre: str,
    capacidad: str,
    baja: str,
    alta: str,
    factores: list[tuple[int, int, str]],
) -> dict[str, Any]:
    """Alta de horno con sus dos tarifas y su tabla de factores."""
    from app.models.firings import KilnOccupancyFactor

    respuesta = await api.post(
        KILNS,
        json={"name": nombre, "capacity_volume_cm3": capacidad},
        headers=head(csrf),
    )
    assert respuesta.status_code == 201, respuesta.text
    kiln = respuesta.json()

    for tipo, importe in (("LOW", baja), ("HIGH", alta)):
        creada = await api.post(
            f"{KILNS}/{kiln['id']}/rates",
            json={"firing_type": tipo, "rate": importe},
            headers=head(csrf),
        )
        assert creada.status_code == 201, creada.text

    for minimo, maximo, factor in factores:
        session.add(
            KilnOccupancyFactor(
                kiln_id=kiln["id"],
                min_percentage=minimo,
                max_percentage=maximo,
                factor=Decimal(factor),
            )
        )
    await session.commit()
    return kiln


async def hornos_de_referencia(
    api: httpx.AsyncClient, csrf: str, session: AsyncSession
) -> tuple[int, int]:
    chico = await crear_horno(
        api,
        csrf,
        session,
        nombre="Horno chico QA",
        capacidad="17000",
        baja="90.00",
        alta="180.00",
        factores=FACTORES_CHICO,
    )
    grande = await crear_horno(
        api,
        csrf,
        session,
        nombre="Horno grande QA",
        capacidad="200000",
        baja="1000.00",
        alta="2000.00",
        factores=FACTORES_GRANDE,
    )
    return chico["id"], grande["id"]


def hoja_de_referencia(chico: int, grande: int) -> dict[str, Any]:
    """El cuerpo que reproduce la hoja «Costo de quema» del documento."""
    return {
        "firing_date": "2026-07-24",
        "sessions": [
            {"kiln_id": chico, "firing_type": "LOW", "sort_order": 0},
            {"kiln_id": chico, "firing_type": "HIGH", "sort_order": 1},
            {"kiln_id": grande, "firing_type": "HIGH", "sort_order": 2},
        ],
        "lines": [
            {
                "description": "Plato palta",
                "quantity": 20,
                "length_cm": "18",
                "width_cm": "12",
                "height_cm": "3",
                "low_kiln_id": chico,
                "high_kiln_id": grande,
                "factor_kiln_id": chico,
                "sort_order": 0,
            },
            {
                "description": "Tasa Buho",
                "quantity": 50,
                "length_cm": "1",
                "width_cm": "15",
                "height_cm": "3",
                "low_kiln_id": chico,
                "high_kiln_id": chico,
                "factor_kiln_id": grande,
                "sort_order": 1,
            },
            {
                "description": "Platos hondos chicos",
                "quantity": 12,
                "length_cm": "15",
                "width_cm": "12",
                "height_cm": "5",
                "low_kiln_id": chico,
                "high_kiln_id": chico,
                "factor_kiln_id": grande,
                "sort_order": 2,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Maestro de hornos
# ---------------------------------------------------------------------------
async def test_crear_horno_genera_codigo_en_el_backend(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    respuesta = await api.post(
        KILNS,
        json={"name": "Horno de ensayo", "capacity_volume_cm3": "5000"},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 201
    creado = respuesta.json()
    assert creado["code"].startswith("KILN-")
    assert Decimal(creado["capacity_volume_cm3"]) == Decimal(5000)


async def test_el_cliente_no_puede_imponer_el_codigo_del_horno(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    respuesta = await api.post(
        KILNS,
        json={"name": "Horno", "capacity_volume_cm3": "5000", "code": "MIO-999"},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422


async def test_editar_horno_cambia_capacidad_y_estado(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    kiln = (
        await api.post(
            KILNS,
            json={"name": "Horno editable", "capacity_volume_cm3": "5000"},
            headers=head(admin_csrf),
        )
    ).json()

    respuesta = await api.put(
        f"{KILNS}/{kiln['id']}",
        json={"capacity_volume_cm3": "6000", "active": False},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 200
    actualizado = respuesta.json()
    assert Decimal(actualizado["capacity_volume_cm3"]) == Decimal(6000)
    assert actualizado["active"] is False
    assert actualizado["code"] == kiln["code"]


async def test_capacidad_no_positiva_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    respuesta = await api.post(
        KILNS,
        json={"name": "Horno imposible", "capacity_volume_cm3": "0"},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422


async def test_listado_de_hornos_filtra_en_el_servidor(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    await api.post(
        KILNS,
        json={"name": "Horno alfa", "capacity_volume_cm3": "1000"},
        headers=head(admin_csrf),
    )
    respuesta = await api.get(KILNS, params={"search": "alfa"})
    assert respuesta.status_code == 200
    pagina = respuesta.json()
    assert pagina["total"] == 1
    assert pagina["items"][0]["name"] == "Horno alfa"


# ---------------------------------------------------------------------------
# Tarifas e historial
# ---------------------------------------------------------------------------
async def test_tarifas_baja_y_alta_quedan_vigentes(api: httpx.AsyncClient, admin_csrf: str) -> None:
    kiln = (
        await api.post(
            KILNS,
            json={"name": "Horno tarifado", "capacity_volume_cm3": "1000"},
            headers=head(admin_csrf),
        )
    ).json()

    for tipo, importe in (("LOW", "90.00"), ("HIGH", "180.00")):
        await api.post(
            f"{KILNS}/{kiln['id']}/rates",
            json={"firing_type": tipo, "rate": importe},
            headers=head(admin_csrf),
        )

    detalle = (await api.get(f"{KILNS}/{kiln['id']}")).json()
    assert Decimal(detalle["current_low_rate"]) == Decimal("90")
    assert Decimal(detalle["current_high_rate"]) == Decimal("180")


async def test_cambiar_una_tarifa_cierra_la_anterior_sin_borrarla(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """El historial se conserva: 1000 -> 1100 deja dos filas, no una."""
    kiln = (
        await api.post(
            KILNS,
            json={"name": "Horno historico", "capacity_volume_cm3": "1000"},
            headers=head(admin_csrf),
        )
    ).json()

    await api.post(
        f"{KILNS}/{kiln['id']}/rates",
        json={"firing_type": "LOW", "rate": "1000.00", "valid_from": "2026-01-01"},
        headers=head(admin_csrf),
    )
    await api.post(
        f"{KILNS}/{kiln['id']}/rates",
        json={"firing_type": "LOW", "rate": "1100.00", "valid_from": "2026-06-01"},
        headers=head(admin_csrf),
    )

    historial = (await api.get(f"{KILNS}/{kiln['id']}/rates")).json()
    bajas = [r for r in historial if r["firing_type"] == "LOW"]
    assert len(bajas) == 2

    vigentes = [r for r in bajas if r["valid_to"] is None]
    assert len(vigentes) == 1
    assert Decimal(vigentes[0]["rate"]) == Decimal("1100")

    cerrada = next(r for r in bajas if r["valid_to"] is not None)
    assert Decimal(cerrada["rate"]) == Decimal("1000")
    assert cerrada["valid_to"] == "2026-06-01"


# ---------------------------------------------------------------------------
# Simulador
# ---------------------------------------------------------------------------
async def test_el_simulador_reproduce_el_caso_de_referencia(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """§18: 12960 cm3 de 26010, 1041.38 de base y 1249.66 tras el factor 1.20."""
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)

    respuesta = await api.post(
        f"{FIRINGS}/calculate",
        json=hoja_de_referencia(chico, grande),
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 200, respuesta.text
    resultado = respuesta.json()

    assert Decimal(resultado["total_volume_cm3"]) == Decimal(26010)

    palta = resultado["lines"][0]
    assert Decimal(palta["total_volume_cm3"]) == Decimal(12960)
    assert Decimal(palta["base_cost"]).quantize(Decimal("0.01")) == Decimal("1041.38")
    assert palta["occupancy_bracket"] == 80
    assert Decimal(palta["occupancy_factor"]) == Decimal("1.2")
    assert Decimal(palta["allocated_cost"]).quantize(Decimal("0.01")) == Decimal("1249.66")


async def test_el_simulador_no_toca_inventario_ni_gasta_correlativo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """§19 y §25: simular es de solo lectura."""
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)

    async def contar(modelo: Any) -> int:
        return int(
            (await db_session.execute(select(func.count()).select_from(modelo))).scalar_one()
        )

    movimientos_antes = await contar(StockMovement)
    correlativos_antes = await contar(DocumentSequenceIssue)

    for _ in range(3):
        respuesta = await api.post(
            f"{FIRINGS}/calculate",
            json=hoja_de_referencia(chico, grande),
            headers=head(admin_csrf),
        )
        assert respuesta.status_code == 200

    assert await contar(StockMovement) == movimientos_antes
    assert await contar(DocumentSequenceIssue) == correlativos_antes


async def test_el_simulador_marca_la_capacidad_excedida_sin_fallar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, _grande = await hornos_de_referencia(api, admin_csrf, db_session)

    respuesta = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Pieza enorme",
                    "quantity": 1,
                    "length_cm": "100",
                    "width_cm": "100",
                    "height_cm": "100",
                    "low_kiln_id": chico,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 200
    resultado = respuesta.json()
    assert resultado["capacity_exceeded"] is True
    assert resultado["lines"][0]["capacity_exceeded"] is True


async def test_el_simulador_esta_disponible_para_operator(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)

    # Se cambia de sesion despues de preparar los datos: las fixtures de rol
    # comparten cliente, y autenticarse de nuevo invalida el token anterior.
    operator_csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
    respuesta = await api.post(
        f"{FIRINGS}/calculate",
        json=hoja_de_referencia(chico, grande),
        headers=head(operator_csrf),
    )
    assert respuesta.status_code == 200


# ---------------------------------------------------------------------------
# Ciclo de vida de la hoja
# ---------------------------------------------------------------------------
async def test_crear_borrador_emite_correlativo_y_calcula(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)

    respuesta = await api.post(
        FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf)
    )
    assert respuesta.status_code == 201, respuesta.text
    hoja = respuesta.json()

    assert hoja["status"] == "DRAFT"
    assert hoja["code"]
    assert Decimal(hoja["total_volume_cm3"]) == Decimal(26010)
    assert len(hoja["sessions"]) == 3
    assert len(hoja["lines"]) == 3
    assert Decimal(hoja["lines"][0]["allocated_cost"]).quantize(Decimal("0.01")) == Decimal(
        "1249.66"
    )


async def test_editar_borrador_recalcula_los_costos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()

    cuerpo = hoja_de_referencia(chico, grande)
    cuerpo["lines"] = cuerpo["lines"][:1]  # type: ignore[index]

    respuesta = await api.put(f"{FIRINGS}/{hoja['id']}", json=cuerpo, headers=head(admin_csrf))
    assert respuesta.status_code == 200
    actualizada = respuesta.json()

    assert len(actualizada["lines"]) == 1
    assert Decimal(actualizada["total_volume_cm3"]) == Decimal(12960)
    # Al quedarse sola, la pieza absorbe las tarifas completas: 90 + 2000.
    assert Decimal(actualizada["subtotal"]) == Decimal(2090)


async def test_confirmar_congela_los_snapshots_de_tarifa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """§7 y §37: cambiar la tarifa manana no reescribe una quema de ayer."""
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()

    confirmada = (
        await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))
    ).json()
    assert confirmada["status"] == "CONFIRMED"
    assert confirmada["confirmed_at"] is not None
    coste_original = Decimal(confirmada["total_cost"])

    # La tarifa del horno grande sube de 2000 a 4000.
    await api.post(
        f"{KILNS}/{grande}/rates",
        json={"firing_type": "HIGH", "rate": "4000.00"},
        headers=head(admin_csrf),
    )

    releida = (await api.get(f"{FIRINGS}/{hoja['id']}")).json()
    assert Decimal(releida["total_cost"]) == coste_original
    alta_grande = next(
        s for s in releida["sessions"] if s["kiln_id"] == grande and s["firing_type"] == "HIGH"
    )
    assert Decimal(alta_grande["rate_snapshot"]) == Decimal(2000)


async def test_una_hoja_confirmada_no_se_puede_editar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()
    await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))

    respuesta = await api.put(
        f"{FIRINGS}/{hoja['id']}",
        json=hoja_de_referencia(chico, grande),
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "FIRING_NOT_EDITABLE"


async def test_una_hoja_confirmada_no_se_puede_reconfirmar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()
    await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))

    respuesta = await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))
    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "FIRING_NOT_CONFIRMABLE"


async def test_confirmar_bloquea_si_una_pieza_supera_la_capacidad(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """§16: por encima del 100 % de ocupacion la hoja no se puede confirmar."""
    chico, _grande = await hornos_de_referencia(api, admin_csrf, db_session)
    cuerpo = {
        "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
        "lines": [
            {
                "description": "Pieza enorme",
                "quantity": 1,
                "length_cm": "100",
                "width_cm": "100",
                "height_cm": "100",
                "low_kiln_id": chico,
            }
        ],
    }
    hoja = (await api.post(FIRINGS, json=cuerpo, headers=head(admin_csrf))).json()

    respuesta = await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))
    assert respuesta.status_code == 409
    assert respuesta.json()["error"]["code"] == "KILN_CAPACITY_EXCEEDED"

    # Y sigue en borrador: no se confirmo a medias.
    assert (await api.get(f"{FIRINGS}/{hoja['id']}")).json()["status"] == "DRAFT"


async def test_anular_no_borra_la_hoja(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()
    await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))

    anulada = (await api.post(f"{FIRINGS}/{hoja['id']}/cancel", headers=head(admin_csrf))).json()
    assert anulada["status"] == "CANCELLED"
    assert anulada["cancelled_at"] is not None

    # La fila sigue ahi y conserva su costo.
    releida = (await api.get(f"{FIRINGS}/{hoja['id']}")).json()
    assert releida["code"] == hoja["code"]
    assert Decimal(releida["total_cost"]) > Decimal(0)


async def test_confirmar_una_quema_no_consume_materiales(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """§25: el costo del horno no se mezcla con la produccion de esmaltes."""
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    movimientos_antes = int(
        (await db_session.execute(select(func.count()).select_from(StockMovement))).scalar_one()
    )

    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()
    await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))

    movimientos_despues = int(
        (await db_session.execute(select(func.count()).select_from(StockMovement))).scalar_one()
    )
    assert movimientos_despues == movimientos_antes


# ---------------------------------------------------------------------------
# Listado
# ---------------------------------------------------------------------------
async def test_listado_filtra_por_estado_y_horno_en_el_servidor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    primera = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()
    await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    await api.post(f"{FIRINGS}/{primera['id']}/confirm", headers=head(admin_csrf))

    confirmadas = (await api.get(FIRINGS, params={"status": "CONFIRMED"})).json()
    assert confirmadas["total"] == 1
    assert confirmadas["items"][0]["code"] == primera["code"]

    borradores = (await api.get(FIRINGS, params={"status": "DRAFT"})).json()
    assert borradores["total"] == 1

    por_horno = (await api.get(FIRINGS, params={"kiln_id": grande})).json()
    assert por_horno["total"] == 2

    por_codigo = (await api.get(FIRINGS, params={"search": primera["code"]})).json()
    assert por_codigo["total"] == 1


async def test_listado_pagina_en_el_servidor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    for _ in range(3):
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))

    pagina = (await api.get(FIRINGS, params={"limit": 2, "offset": 0})).json()
    assert pagina["total"] == 3
    assert len(pagina["items"]) == 2

    siguiente = (await api.get(FIRINGS, params={"limit": 2, "offset": 2})).json()
    assert len(siguiente["items"]) == 1


# ---------------------------------------------------------------------------
# Permisos y CSRF
# ---------------------------------------------------------------------------
async def test_operator_no_puede_crear_hornos(api: httpx.AsyncClient, operator_csrf: str) -> None:
    respuesta = await api.post(
        KILNS,
        json={"name": "Horno prohibido", "capacity_volume_cm3": "1000"},
        headers=head(operator_csrf),
    )
    assert respuesta.status_code == 403


async def test_operator_no_puede_crear_ni_confirmar_quemas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()

    operator_csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

    creacion = await api.post(
        FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(operator_csrf)
    )
    assert creacion.status_code == 403

    confirmacion = await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(operator_csrf))
    assert confirmacion.status_code == 403


async def test_operator_si_puede_consultar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()
    await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

    assert (await api.get(KILNS)).status_code == 200
    assert (await api.get(FIRINGS)).status_code == 200
    assert (await api.get(f"{FIRINGS}/{hoja['id']}")).status_code == 200


async def test_sin_csrf_no_se_muta(api: httpx.AsyncClient) -> None:
    respuesta = await api.post(KILNS, json={"name": "Sin csrf", "capacity_volume_cm3": "1000"})
    assert respuesta.status_code == 403


# ---------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------
async def test_se_auditan_alta_de_horno_tarifa_y_ciclo_de_la_hoja(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    hoja = (
        await api.post(FIRINGS, json=hoja_de_referencia(chico, grande), headers=head(admin_csrf))
    ).json()
    await api.post(f"{FIRINGS}/{hoja['id']}/confirm", headers=head(admin_csrf))
    await api.post(f"{FIRINGS}/{hoja['id']}/cancel", headers=head(admin_csrf))

    eventos = (await db_session.execute(select(AuditEvent.entity_type, AuditEvent.entity_id))).all()
    tipos = {fila[0] for fila in eventos}
    assert {"kiln", "kiln_rate", "firing"} <= tipos

    de_la_hoja = [f for f in eventos if f[0] == "firing" and f[1] == str(hoja["id"])]
    # Alta, confirmacion y anulacion.
    assert len(de_la_hoja) >= 3


# ---------------------------------------------------------------------------
# Validaciones de entrada
# ---------------------------------------------------------------------------
async def test_una_pieza_sin_horno_asignado_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, _grande = await hornos_de_referencia(api, admin_csrf, db_session)
    respuesta = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Pieza suelta",
                    "quantity": 1,
                    "length_cm": "1",
                    "width_cm": "1",
                    "height_cm": "1",
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422


async def test_un_horno_desactivado_no_puede_usarse(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    await api.put(f"{KILNS}/{chico}", json={"active": False}, headers=head(admin_csrf))

    respuesta = await api.post(
        f"{FIRINGS}/calculate",
        json=hoja_de_referencia(chico, grande),
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "KILN_INACTIVE"


async def test_un_horno_sin_tarifa_no_puede_quemarse(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    kiln = (
        await api.post(
            KILNS,
            json={"name": "Horno sin tarifa", "capacity_volume_cm3": "1000"},
            headers=head(admin_csrf),
        )
    ).json()

    respuesta = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "sessions": [{"kiln_id": kiln["id"], "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Pieza",
                    "quantity": 1,
                    "length_cm": "1",
                    "width_cm": "1",
                    "height_cm": "1",
                    "low_kiln_id": kiln["id"],
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "KILN_RATE_NOT_CONFIGURED"


async def test_dimensiones_no_positivas_se_rechazan(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, _grande = await hornos_de_referencia(api, admin_csrf, db_session)
    respuesta = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Pieza plana",
                    "quantity": 1,
                    "length_cm": "10",
                    "width_cm": "10",
                    "height_cm": "0",
                    "low_kiln_id": chico,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422


async def test_limpiar_notas_horno_con_null(api: httpx.AsyncClient, admin_csrf: str) -> None:
    kiln = (
        await api.post(
            KILNS,
            json={
                "name": "Horno Con Notas",
                "capacity_volume_cm3": "1000",
                "notes": "Notas a borrar",
            },
            headers=head(admin_csrf),
        )
    ).json()
    assert kiln["notes"] == "Notas a borrar"

    actualizado = (
        await api.put(
            f"{KILNS}/{kiln['id']}",
            json={"notes": None},
            headers=head(admin_csrf),
        )
    ).json()
    assert actualizado["notes"] is None

    obtenido = (await api.get(f"{KILNS}/{kiln['id']}")).json()
    assert obtenido["notes"] is None


async def test_horno_nuevo_sin_factores_bloquea_y_permite_configurarlos(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    # 1. Crear horno sin factores
    kiln = (
        await api.post(
            KILNS,
            json={"name": "Horno Recién Creado", "capacity_volume_cm3": "15000"},
            headers=head(admin_csrf),
        )
    ).json()
    assert kiln["occupancy_factors"] == []

    # 2. Agregar tarifa
    rate_resp = await api.post(
        f"{KILNS}/{kiln['id']}/rates",
        json={"firing_type": "LOW", "rate": "120.00"},
        headers=head(admin_csrf),
    )
    assert rate_resp.status_code == 201

    # 3. Intentar calcular sin factores -> 422
    calc_fail = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "sessions": [{"kiln_id": kiln["id"], "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Taza",
                    "quantity": 1,
                    "length_cm": "10",
                    "width_cm": "10",
                    "height_cm": "10",
                    "low_kiln_id": kiln["id"],
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert calc_fail.status_code == 422
    assert calc_fail.json()["error"]["code"] == "OCCUPANCY_FACTOR_NOT_CONFIGURED"

    # 4. Configurar factores vía PUT /kilns/{id}/occupancy-factors
    factors_payload = [
        {"min_percentage": 1, "max_percentage": 50, "factor": "2.0"},
        {"min_percentage": 51, "max_percentage": 100, "factor": "1.0"},
    ]
    set_factors_resp = await api.put(
        f"{KILNS}/{kiln['id']}/occupancy-factors",
        json=factors_payload,
        headers=head(admin_csrf),
    )
    assert set_factors_resp.status_code == 200
    factors_out = set_factors_resp.json()
    assert len(factors_out) == 2

    # 5. Calcular nuevamente -> 200 éxito
    calc_ok = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "sessions": [{"kiln_id": kiln["id"], "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Taza",
                    "quantity": 1,
                    "length_cm": "10",
                    "width_cm": "10",
                    "height_cm": "10",
                    "low_kiln_id": kiln["id"],
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert calc_ok.status_code == 200


async def test_tarifa_futura_no_aplica_antes_de_su_fecha_efectiva(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, _ = await hornos_de_referencia(api, admin_csrf, db_session)

    # Horno chico tiene tarifa LOW actual (90.00).
    # Agregamos una tarifa futura para 2026-09-01 (150.00).
    tarifa_futura = await api.post(
        f"{KILNS}/{chico}/rates",
        json={"firing_type": "LOW", "rate": "150.00", "valid_from": "2026-09-01"},
        headers=head(admin_csrf),
    )
    assert tarifa_futura.status_code == 201

    # Hoy / con fecha anterior a 2026-09-01 usa la tarifa vigente anterior (90.00)
    calc_hoy = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "firing_date": "2026-08-23",
            "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Plato",
                    "quantity": 1,
                    "length_cm": "10",
                    "width_cm": "10",
                    "height_cm": "5",
                    "low_kiln_id": chico,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert calc_hoy.status_code == 200
    assert calc_hoy.json()["sessions"][0]["rate_snapshot"] == "90.000000"

    # Con fecha en el futuro (2026-09-05) aplica la nueva tarifa (150.00)
    calc_futuro = await api.post(
        f"{FIRINGS}/calculate",
        json={
            "firing_date": "2026-09-05",
            "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
            "lines": [
                {
                    "description": "Plato",
                    "quantity": 1,
                    "length_cm": "10",
                    "width_cm": "10",
                    "height_cm": "5",
                    "low_kiln_id": chico,
                }
            ],
        },
        headers=head(admin_csrf),
    )
    assert calc_futuro.status_code == 200
    assert calc_futuro.json()["sessions"][0]["rate_snapshot"] == "150.000000"


async def test_product_id_inexistente_rechaza_con_422(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, _grande = await hornos_de_referencia(api, admin_csrf, db_session)
    stale_payload = {
        "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
        "lines": [
            {
                "product_id": 999999,
                "description": "Pieza Fantasma",
                "quantity": 1,
                "length_cm": "10",
                "width_cm": "10",
                "height_cm": "5",
                "low_kiln_id": chico,
            }
        ],
    }

    # calculate con product_id inexistente -> 422
    calc_resp = await api.post(f"{FIRINGS}/calculate", json=stale_payload, headers=head(admin_csrf))
    assert calc_resp.status_code == 422
    assert calc_resp.json()["error"]["code"] == "PRODUCT_NOT_FOUND"

    # create con product_id inexistente -> 422 (no 500)
    create_resp = await api.post(FIRINGS, json=stale_payload, headers=head(admin_csrf))
    assert create_resp.status_code == 422
    assert create_resp.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


async def test_volumen_que_desborda_precision_rechaza_con_422(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, _ = await hornos_de_referencia(api, admin_csrf, db_session)
    overflow_payload = {
        "sessions": [{"kiln_id": chico, "firing_type": "LOW"}],
        "lines": [
            {
                "description": "Pieza Gigante",
                "quantity": 1,
                "length_cm": "100000",
                "width_cm": "100000",
                "height_cm": "100000",
                "low_kiln_id": chico,
            }
        ],
    }

    calc_resp = await api.post(
        f"{FIRINGS}/calculate", json=overflow_payload, headers=head(admin_csrf)
    )
    assert calc_resp.status_code == 422
    assert calc_resp.json()["error"]["code"] == "FIRING_VOLUME_OVERFLOW"

    create_resp = await api.post(FIRINGS, json=overflow_payload, headers=head(admin_csrf))
    assert create_resp.status_code == 422
    assert create_resp.json()["error"]["code"] == "FIRING_VOLUME_OVERFLOW"


async def test_concurrencia_update_vs_confirm_solo_un_ganador(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    chico, grande = await hornos_de_referencia(api, admin_csrf, db_session)
    creada = (
        await api.post(
            FIRINGS,
            json=hoja_de_referencia(chico, grande),
            headers=head(admin_csrf),
        )
    ).json()
    firing_id = creada["id"]

    update_payload = hoja_de_referencia(chico, grande)
    update_payload["notes"] = "Nota modificada concurrentemente"

    async def do_update() -> httpx.Response:
        return await api.put(
            f"{FIRINGS}/{firing_id}", json=update_payload, headers=head(admin_csrf)
        )

    async def do_confirm() -> httpx.Response:
        return await api.post(f"{FIRINGS}/{firing_id}/confirm", headers=head(admin_csrf))

    resp_update, resp_confirm = await asyncio.gather(do_update(), do_confirm())

    assert resp_update.status_code in (200, 409)
    assert resp_confirm.status_code in (200, 409)

    final = (await api.get(f"{FIRINGS}/{firing_id}")).json()
    if resp_confirm.status_code == 200:
        assert final["status"] == "CONFIRMED"
        if resp_update.status_code == 409:
            assert resp_update.json()["error"]["code"] == "FIRING_NOT_EDITABLE"
    elif resp_update.status_code == 200:
        assert final["status"] in ("DRAFT", "CONFIRMED")
