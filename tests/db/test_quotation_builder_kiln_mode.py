"""Fase 009K.3 — cargar el horno junto, o producto a producto.

`TOGETHER` no es codigo nuevo: es lo que el Cotizador ha hecho siempre. Una
sola hoja de quema donde todos los productos comparten sesion, el volumen se
acumula y la tarifa se reparte por participacion. La auditoria de esta fase lo
dejo demostrado, y por eso una cotizacion sin modo declarado se lee `TOGETHER`.

`PER_PRODUCT` es lo nuevo: cada producto planifica SU hornada aunque el horno
sea el mismo, y por tanto paga la quema entera. Dos productos con el mismo
horno dejan de sumar volumen. Eso no es una duplicacion accidental — es
literalmente lo que significa «por producto», y es la afirmacion que estas
pruebas tienen que dejar clavada, porque a simple vista parece un defecto.

Lo que NO cambia, y aqui se comprueba: `compute_firing`, la cuenta de
capacidad, los tramos de ocupacion y el factor por tramo, y el prorrateo
interno de cada sesion. El modo decide QUE piezas van en cada hoja; a partir
de ahi la matematica es la de siempre.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.masters import Product
from app.models.quotations import Quotation
from tests.db.test_firings_api import FACTORES_CHICO, crear_horno
from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, _customer, head
from tests.db.test_quotations_api import _finished_product_and_recipe

#: Tarifas del horno de estas pruebas. Distintas entre si para que un error de
#: reparto no pueda esconderse detras de dos numeros iguales.
TARIFA_BAJA = Decimal(120)
TARIFA_ALTA = Decimal(180)
QUEMA_COMPLETA = TARIFA_BAJA + TARIFA_ALTA
DIAS_POR_HORNADA = 3


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


async def _dos_productos_un_horno(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    sufijo: str,
    capacidad: str,
    lado: str = "10",
    cantidades: tuple[int, int] = (1, 1),
) -> dict[str, Any]:
    """Dos productos cubicos identicos y un solo horno.

    Identicos a proposito: si los dos productos tienen el mismo volumen, la
    participacion en `TOGETHER` es exactamente la mitad para cada uno y
    cualquier desvio del reparto salta a la vista sin decimales raros.
    """
    customer = await _customer(api, csrf)
    producto_a, receta_a = await _finished_product_and_recipe(api, csrf, f"{sufijo}_a")
    producto_b, receta_b = await _finished_product_and_recipe(api, csrf, f"{sufijo}_b")
    await db_session.execute(
        update(Product)
        .where(Product.id.in_([producto_a["id"], producto_b["id"]]))
        .values(width=Decimal(lado), height=Decimal(lado), length=Decimal(lado))
    )
    await db_session.commit()
    horno = await crear_horno(
        api,
        csrf,
        db_session,
        nombre=f"Horno 009K3{sufijo}",
        capacidad=capacidad,
        baja=str(TARIFA_BAJA),
        alta=str(TARIFA_ALTA),
        factores=FACTORES_CHICO,
    )
    return {
        "name": f"009K.3 {sufijo}",
        "customer_id": customer["id"],
        "kiln_id": horno["id"],
        "items": [
            {
                "product_id": producto["id"],
                "quantity": cantidad,
                "recipe_id": receta["id"],
                "recipe_version_id": receta["current_version"]["id"],
                "material_grams_per_piece": "10",
                "markup_percent": "100",
                "sort_order": indice,
            }
            for indice, (producto, receta, cantidad) in enumerate(
                [
                    (producto_a, receta_a, cantidades[0]),
                    (producto_b, receta_b, cantidades[1]),
                ]
            )
        ],
    }


def _base_por_linea(body: dict[str, Any]) -> list[Decimal]:
    """Costo de quema de cada linea ANTES del factor de ocupacion."""
    return [Decimal(linea["base_cost"]) for linea in body["production_summary"]["lines"]]


# ---------------------------------------------------------------------------
# BH01-BH06: TOGETHER, que es lo de siempre
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_modo_por_defecto_es_todo_junto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH01. QUOTATION_KILN_MODE_DEFAULT: TOGETHER."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)

    body = await _preview(api, admin_csrf, payload)

    assert body["kiln_mode"] == "TOGETHER"


@pytest.mark.asyncio
async def test_juntos_dos_productos_comparten_una_sola_sesion_por_tipo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH02 + BH03. El volumen de la sesion es la SUMA de las dos piezas."""
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_junto", capacidad="1000000"
    )

    body = await _preview(api, admin_csrf, payload)

    sesiones = body["production_summary"]["sessions"]
    assert len(sesiones) == 2, "una sesion de quema baja y una de alta, no cuatro"
    assert {sesion["firing_type"] for sesion in sesiones} == {"LOW", "HIGH"}
    # 10x10x10 dos veces: 2000 cm3 en cada sesion.
    for sesion in sesiones:
        assert Decimal(sesion["assigned_volume_cm3"]) == Decimal(2000)


@pytest.mark.asyncio
async def test_juntos_la_quema_se_reparte_por_volumen_y_se_cobra_una_vez(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH04 + BH06. CURRENT_PRORATION_RULE_CHANGED: NO.

    Dos piezas iguales pagan media quema cada una, y la suma es UNA quema.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_reparto", capacidad="1000000"
    )

    body = await _preview(api, admin_csrf, payload)

    assert body["production_summary"]["total_batches"] == 2, "una hornada baja y una alta"
    bases = _base_por_linea(body)
    assert bases == [QUEMA_COMPLETA / 2, QUEMA_COMPLETA / 2]
    assert sum(bases) == QUEMA_COMPLETA


@pytest.mark.asyncio
async def test_juntos_el_volumen_que_no_cabe_se_resuelve_con_mas_hornadas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH05. El techo es exacto: 2000 en un horno de 700 son 3 hornadas.

    Y con tres hornadas el horno se enciende tres veces, asi que la sesion
    cuesta tres tarifas. Redondear hacia abajo cobraria dos.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_tope", capacidad="700"
    )

    body = await _preview(api, admin_csrf, payload)

    sesiones = {sesion["firing_type"]: sesion for sesion in body["production_summary"]["sessions"]}
    assert sesiones["LOW"]["batches"] == 3
    assert Decimal(sesiones["LOW"]["subtotal"]) == TARIFA_BAJA * 3
    assert sesiones["HIGH"]["batches"] == 3
    assert body["production_summary"]["total_batches"] == 6


# ---------------------------------------------------------------------------
# BH07-BH10: PER_PRODUCT
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_por_producto_el_mismo_horno_ya_no_comparte_volumen(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH07 + BH08. PER_PRODUCT_SESSION_VOLUME_SHARED_BETWEEN_PRODUCTS: NO.

    Es LA afirmacion de la fase. Los dos productos van al mismo horno y aun
    asi cada uno abre sus sesiones con SU volumen: 1000, no 2000.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_porprod", capacidad="1000000"
    )

    body = await _preview(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    assert body["kiln_mode"] == "PER_PRODUCT"
    sesiones = body["production_summary"]["sessions"]
    assert len(sesiones) == 4, "dos sesiones por producto, aunque el horno sea el mismo"
    for sesion in sesiones:
        assert Decimal(sesion["assigned_volume_cm3"]) == Decimal(1000)


@pytest.mark.asyncio
async def test_por_producto_cada_uno_paga_su_quema_entera(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH07 bis. La consecuencia economica, dicha con numeros.

    Junto: 300 en total, 150 cada uno. Por producto: 300 cada uno, 600 en
    total. El doble no es un error de calculo — son dos hornadas de verdad.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_pago", capacidad="1000000"
    )

    juntos = await _preview(api, admin_csrf, payload)
    por_producto = await _preview(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    assert sum(_base_por_linea(juntos)) == QUEMA_COMPLETA
    assert _base_por_linea(por_producto) == [QUEMA_COMPLETA, QUEMA_COMPLETA]
    assert sum(_base_por_linea(por_producto)) == QUEMA_COMPLETA * 2


@pytest.mark.asyncio
async def test_por_producto_las_hornadas_se_cuentan_por_separado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH09. Cada pieza aplica el techo a SU volumen.

    2000 cm3 en un horno de 700 son 3 hornadas si van juntas. Separadas son
    1000 cada una: 2 hornadas cada una, 4 en total. Que salgan mas hornadas
    que juntas es exactamente el coste de no compartir horno.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_hornadas", capacidad="700"
    )

    body = await _preview(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    sesiones = body["production_summary"]["sessions"]
    assert [sesion["batches"] for sesion in sesiones] == [2, 2, 2, 2]
    assert body["production_summary"]["total_batches"] == 8
    for item in body["items"]:
        # Dos hornadas de baja mas dos de alta, a tres dias cada una.
        assert item["calculated_days"] == 4 * DIAS_POR_HORNADA


@pytest.mark.asyncio
async def test_por_producto_cada_pieza_solo_ve_sus_propias_hornadas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH09 bis. La trampa que este modo introduce y que hay que vigilar.

    Antes, el plan de una pieza se sacaba filtrando las sesiones de la hoja
    por sus rutas `(horno, tipo)`. Con `PER_PRODUCT` ese filtro le atribuiria
    a cada pieza TAMBIEN las sesiones de la otra —mismo horno, mismo tipo—, y
    con ellas sus hornadas, sus dias y todos los gastos por dia.

    Las cantidades son distintas a proposito: si una pieza se comiera el plan
    de la otra, sus dias saldrian iguales.
    """
    payload = await _dos_productos_un_horno(
        api,
        admin_csrf,
        db_session,
        sufijo="_planes",
        capacidad="1500",
        cantidades=(1, 3),
    )

    body = await _preview(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    primero, segundo = body["items"]
    # 1000 cm3 en 1500 -> 1 hornada por tipo -> 2 hornadas -> 6 dias.
    assert primero["calculated_days"] == 2 * DIAS_POR_HORNADA
    # 3000 cm3 en 1500 -> 2 hornadas por tipo -> 4 hornadas -> 12 dias.
    assert segundo["calculated_days"] == 4 * DIAS_POR_HORNADA


@pytest.mark.asyncio
async def test_por_producto_admite_hornos_distintos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH10. Dos piezas, dos hornos, cada una con la tarifa del suyo."""
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_doshornos", capacidad="1000000"
    )
    otro = await crear_horno(
        api,
        admin_csrf,
        db_session,
        nombre="Horno 009K3 segundo",
        capacidad="1000000",
        baja="500",
        alta="700",
        factores=FACTORES_CHICO,
    )
    payload["items"][1] = {
        **payload["items"][1],
        "low_kiln_id": otro["id"],
        "high_kiln_id": otro["id"],
    }

    body = await _preview(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    assert _base_por_linea(body) == [QUEMA_COMPLETA, Decimal(500) + Decimal(700)]


# ---------------------------------------------------------------------------
# BH11-BH13: guardar, reabrir, cambiar de modo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("modo", ["TOGETHER", "PER_PRODUCT"])
async def test_el_borrador_recuerda_el_modo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession, modo: str
) -> None:
    """BH11 + BH12. DRAFT_KILN_MODE_PERSISTED."""
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo=f"_draft_{modo.lower()}", capacidad="1000000"
    )

    creada = await _crear(api, admin_csrf, {**payload, "kiln_mode": modo})
    reabierta = await _leer(api, admin_csrf, creada["id"])

    assert creada["kiln_mode"] == modo
    assert reabierta["kiln_mode"] == modo
    esperado = QUEMA_COMPLETA * (2 if modo == "PER_PRODUCT" else 1)
    assert sum(Decimal(item["firing_cost"]) for item in reabierta["items"]) > 0
    assert sum(_base_por_linea(reabierta)) == esperado


@pytest.mark.asyncio
async def test_volver_a_juntos_no_deja_cargos_del_modo_anterior(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH13. STALE_PER_PRODUCT_FIRING_CHARGES_AFTER_SWITCH: 0.

    El viaje de ida y vuelta sobre el MISMO borrador guardado: si al volver a
    `TOGETHER` quedara una sesion del modo anterior, el total no coincidiria
    con el de partida.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_vuelta", capacidad="1000000"
    )
    juntos = await _crear(api, admin_csrf, payload)
    total_inicial = Decimal(juntos["quotation_gross_total"])

    separados = await api.put(
        f"{BUILDER}/{juntos['id']}",
        json={**payload, "kiln_mode": "PER_PRODUCT", "expected_updated_at": juntos["updated_at"]},
        headers=head(admin_csrf),
    )
    assert separados.status_code == 200, separados.text
    assert len(separados.json()["production_summary"]["sessions"]) == 4

    de_vuelta = await api.put(
        f"{BUILDER}/{juntos['id']}",
        json={
            **payload,
            "kiln_mode": "TOGETHER",
            "expected_updated_at": separados.json()["updated_at"],
        },
        headers=head(admin_csrf),
    )
    assert de_vuelta.status_code == 200, de_vuelta.text
    assert len(de_vuelta.json()["production_summary"]["sessions"]) == 2
    assert Decimal(de_vuelta.json()["quotation_gross_total"]) == total_inicial


# ---------------------------------------------------------------------------
# BH14-BH16: confirmar, historia y lo que no se toca
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_confirmar_congela_el_modo_y_su_costo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH14. CONFIRMED_KILN_ASSIGNMENT_IMMUTABLE + CONFIRMED_FIRING_COST_IMMUTABLE."""
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_confirm", capacidad="1000000"
    )
    borrador = await _crear(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    confirmada = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    cuerpo = confirmada.json()
    assert cuerpo["status"] == "CONFIRMED"
    assert cuerpo["kiln_mode"] == "PER_PRODUCT"
    assert sum(_base_por_linea(cuerpo)) == QUEMA_COMPLETA * 2

    releida = await _leer(api, admin_csrf, borrador["id"])
    assert releida["kiln_mode"] == "PER_PRODUCT"
    assert Decimal(releida["quotation_gross_total"]) == Decimal(cuerpo["quotation_gross_total"])


@pytest.mark.asyncio
async def test_una_cotizacion_sin_modo_se_lee_todo_junto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH15. LEGACY_NULL_KILN_MODE_INTERPRETATION: TOGETHER.

    Se fabrica el estado de antes de 0026 —columna en NULL— y se comprueba
    que no aparece un tercer modo ni se reinterpreta el costo.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_legacy", capacidad="1000000"
    )
    creada = await _crear(api, admin_csrf, payload)
    total = Decimal(creada["quotation_gross_total"])

    await db_session.execute(
        update(Quotation).where(Quotation.id == creada["id"]).values(kiln_mode=None)
    )
    await db_session.commit()

    historica = await _leer(api, admin_csrf, creada["id"])
    assert historica["kiln_mode"] == "TOGETHER"
    assert Decimal(historica["quotation_gross_total"]) == total


@pytest.mark.asyncio
async def test_el_factor_de_ocupacion_no_cambia_con_el_modo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BH16. KILN_OCCUPANCY_FACTOR_LOGIC_CHANGED: NO.

    El tramo de una pieza se calcula sobre SU volumen y la capacidad del
    horno, y eso no depende de con quien comparta hoja. Que el factor salga
    identico en los dos modos es la prueba de que el modo reparte piezas y no
    toca la matematica de ocupacion.
    """
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_ocupacion", capacidad="4000"
    )

    juntos = await _preview(api, admin_csrf, payload)
    separados = await _preview(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    def tramos(body: dict[str, Any]) -> list[tuple[int, str]]:
        return [
            (linea["occupancy_bracket"], linea["occupancy_factor"])
            for linea in body["production_summary"]["lines"]
        ]

    assert tramos(juntos) == tramos(separados)


@pytest.mark.asyncio
async def test_el_modo_se_guarda_como_texto_conocido(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La columna no admite un tercer valor: el CHECK esta puesto de verdad."""
    payload = await _dos_productos_un_horno(
        api, admin_csrf, db_session, sufijo="_check", capacidad="1000000"
    )
    creada = await _crear(api, admin_csrf, {**payload, "kiln_mode": "PER_PRODUCT"})

    guardado = await db_session.scalar(
        text("SELECT kiln_mode FROM quotations WHERE id = :qid"), {"qid": creada["id"]}
    )
    assert guardado == "PER_PRODUCT"

    rechazado = await api.post(
        f"{BUILDER}/preview",
        json={**payload, "kiln_mode": "POR_PRODUCTO"},
        headers=head(admin_csrf),
    )
    assert rechazado.status_code == 422, rechazado.text


# ---------------------------------------------------------------------------
# BM01-BM04: la matriz. Las dos decisiones son independientes
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factor", "modo"),
    [(False, "TOGETHER"), (True, "TOGETHER"), (False, "PER_PRODUCT"), (True, "PER_PRODUCT")],
)
async def test_las_dos_decisiones_no_se_estorban(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    factor: bool,
    modo: str,
) -> None:
    """BM01-BM04. FACTOR_AND_KILN_MODE_COUPLED: NO.

    El factor multiplica el costo TECNICO; el modo decide como se planifica la
    QUEMA. Cada uno tiene que salir como si el otro no existiera: el factor,
    el que corresponda a su bandera; el numero de sesiones, el que corresponda
    a su modo.
    """
    payload = await _dos_productos_un_horno(
        api,
        admin_csrf,
        db_session,
        sufijo=f"_matriz_{int(factor)}_{modo.lower()}",
        capacidad="1000000",
    )

    body = await _preview(
        api,
        admin_csrf,
        {**payload, "production_factor_enabled": factor, "kiln_mode": modo},
    )

    assert body["production_factor_enabled"] is factor
    assert Decimal(body["production_factor"]) == (Decimal(3) if factor else Decimal(1))
    assert body["kiln_mode"] == modo
    assert len(body["production_summary"]["sessions"]) == (4 if modo == "PER_PRODUCT" else 2)
    esperado = QUEMA_COMPLETA * (2 if modo == "PER_PRODUCT" else 1)
    assert sum(_base_por_linea(body)) == esperado
