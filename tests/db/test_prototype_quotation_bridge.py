"""Fase 009K.1 — el puente de la muestra aprobada a la cotizacion final.

Lo que se comprueba aqui, contra PostgreSQL de verdad, es sobre todo lo que el
puente NO debe hacer: no cotizar desde una muestra que no valio, no inventar
medidas de un texto, no elegir material cuando hay dos candidatos, y no dejar
dos borradores gemelos cuando alguien pulsa dos veces.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockMovement
from app.models.masters import Product
from app.models.prototypes import Prototype, PrototypeMaterialLine, PrototypeMaterialRole
from app.models.quotations import Quotation, QuotationStatus
from tests.db.test_prototypes import (
    PROTOTYPES,
    _como_operario,
    _material,
    _muestra_lista,
)
from tests.db.test_quotation_builder_api import head

BUILDER = "/api/v1/quotation-builder"


def _final(prototype_id: int) -> str:
    return f"{PROTOTYPES}/{prototype_id}/final-quotation"


async def _lineas_material(
    db_session: AsyncSession, prototype_id: int
) -> list[PrototypeMaterialLine]:
    db_session.expire_all()
    return list(
        (
            await db_session.execute(
                select(PrototypeMaterialLine)
                .where(PrototypeMaterialLine.prototype_id == prototype_id)
                .order_by(PrototypeMaterialLine.sort_order)
            )
        )
        .scalars()
        .all()
    )


async def _cotizaciones(db_session: AsyncSession) -> int:
    db_session.expire_all()
    return int(await db_session.scalar(select(func.count()).select_from(Quotation)) or 0)


async def _movimientos(db_session: AsyncSession) -> int:
    db_session.expire_all()
    return int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)


async def _liga_producto(
    api: httpx.AsyncClient, csrf: str, datos: dict[str, Any], **extra: Any
) -> None:
    """La muestra tiene que decir DE QUE producto es.

    Sin ese vinculo el puente no tiene nada que precargar y devuelve un
    borrador sin lineas, que es lo que fija
    `test_una_muestra_sin_producto_da_un_borrador_vacio_y_no_uno_inventado`.
    """
    respuesta = await api.put(
        f"{PROTOTYPES}/{datos['prototipo']['id']}",
        json={"product_id": datos["producto"]["id"], **extra},
        headers=head(csrf),
    )
    assert respuesta.status_code == 200, respuesta.text


async def _aprobada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, *, suffix: str, **extra: Any
) -> dict[str, Any]:
    """Una muestra fabricada, completada y aprobada: la unica que autoriza cotizar."""
    datos = await _muestra_lista(api, csrf, db_session, suffix=suffix, **extra)
    proto_id = datos["prototipo"]["id"]
    await _liga_producto(api, csrf, datos)
    assert (await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(csrf))).status_code == 200
    assert (
        await api.post(f"{PROTOTYPES}/{proto_id}/complete", headers=head(csrf))
    ).status_code == 200
    assert (
        await api.post(f"{PROTOTYPES}/{proto_id}/approve", json={}, headers=head(csrf))
    ).status_code == 200
    return datos


# ---------------------------------------------------------------------------
# La matriz de estados
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_muestra_recien_creada_no_autoriza_cotizar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CREATED: la muestra ni siquiera se ha fabricado."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_created")
    antes = await _cotizaciones(db_session)

    respuesta = await api.post(_final(datos["prototipo"]["id"]), headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_NOT_APPROVED_FOR_QUOTATION"
    assert await _cotizaciones(db_session) == antes


@pytest.mark.asyncio
async def test_una_muestra_en_produccion_no_autoriza_cotizar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """STARTED: gasto material, pero todavia no se ha mirado."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_started")
    proto_id = datos["prototipo"]["id"]
    assert (
        await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(admin_csrf))
    ).status_code == 200
    antes = await _cotizaciones(db_session)

    respuesta = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert respuesta.status_code == 409
    assert await _cotizaciones(db_session) == antes


@pytest.mark.asyncio
async def test_una_muestra_completada_sin_decidir_no_autoriza_cotizar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMPLETED + PENDING es el estado normal mientras alguien la evalua."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_pending")
    proto_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{proto_id}/complete", headers=head(admin_csrf))
    antes = await _cotizaciones(db_session)

    respuesta = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert respuesta.status_code == 409
    assert await _cotizaciones(db_session) == antes


@pytest.mark.asyncio
async def test_una_muestra_rechazada_no_autoriza_cotizar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Cotizar desde la que no valio es exactamente lo que hay que impedir."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_rejected")
    proto_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{proto_id}/complete", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{proto_id}/reject", json={}, headers=head(admin_csrf))
    antes = await _cotizaciones(db_session)

    respuesta = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert respuesta.status_code == 409
    assert await _cotizaciones(db_session) == antes


@pytest.mark.asyncio
async def test_una_predecesora_aprobada_pero_sustituida_no_autoriza_cotizar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La vigente es la que manda, aunque la anterior este COMPLETED + APPROVED.

    Es el caso mas dificil de ver: la predecesora cumple los dos requisitos de
    estado, y aun asi cotizar desde ella seria cotizar una muestra que alguien
    ya decidio repetir.
    """
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_super")
    anterior = datos["prototipo"]["id"]
    sucesora = await api.post(
        f"{PROTOTYPES}/{anterior}/successor", json={}, headers=head(admin_csrf)
    )
    assert sucesora.status_code == 201, sucesora.text
    antes = await _cotizaciones(db_session)

    respuesta = await api.post(_final(anterior), headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PROTOTYPE_SUPERSEDED_FOR_QUOTATION"
    assert await _cotizaciones(db_session) == antes


# ---------------------------------------------------------------------------
# El camino que si
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_muestra_vigente_aprobada_crea_un_borrador_y_nada_mas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Crea DRAFT, guarda el origen, y no toca inventario ni produccion."""
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_ok")
    proto_id = datos["prototipo"]["id"]
    movimientos_antes = await _movimientos(db_session)

    respuesta = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert respuesta.status_code == 201, respuesta.text
    creada = respuesta.json()
    assert creada["status"] == "DRAFT"

    fila = await db_session.get(Quotation, creada["id"])
    assert fila is not None
    assert fila.origin_prototype_id == proto_id
    assert fila.confirmed_at is None
    assert fila.paid_at is None
    assert await _movimientos(db_session) == movimientos_antes

    # La cantidad de muestra NO es la del pedido: la pone una persona.
    assert all(item.get("quantity") in (None, 0) for item in creada["items"])
    assert creada["customer_id"] is None


@pytest.mark.asyncio
async def test_pulsar_dos_veces_devuelve_el_mismo_borrador(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """201 la primera, 200 la segunda, y una sola cotizacion."""
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_idem")
    proto_id = datos["prototipo"]["id"]

    primera = await api.post(_final(proto_id), headers=head(admin_csrf))
    segunda = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert primera.status_code == 201
    assert segunda.status_code == 200
    assert primera.json()["id"] == segunda.json()["id"]

    db_session.expire_all()
    borradores = await db_session.scalar(
        select(func.count())
        .select_from(Quotation)
        .where(
            Quotation.origin_prototype_id == proto_id,
            Quotation.status == QuotationStatus.DRAFT,
        )
    )
    assert borradores == 1


@pytest.mark.asyncio
async def test_veinte_peticiones_simultaneas_crean_una_sola_cotizacion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_TO_QUOTE_CONCURRENCY_SAFE.

    El indice unico parcial protege la base, pero el servicio tiene que
    ABSORBER la carrera: si dependiera del indice, una de las veinte se
    llevaria un error de integridad en la cara en vez de la cotizacion que
    pidio.
    """
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_conc")
    proto_id = datos["prototipo"]["id"]

    respuestas = await asyncio.gather(
        *(api.post(_final(proto_id), headers=head(admin_csrf)) for _ in range(20))
    )

    codigos = [r.status_code for r in respuestas]
    assert all(c in (200, 201) for c in codigos), codigos
    assert sum(1 for c in codigos if c == 201) == 1, codigos
    assert len({r.json()["id"] for r in respuestas}) == 1

    db_session.expire_all()
    borradores = await db_session.scalar(
        select(func.count())
        .select_from(Quotation)
        .where(
            Quotation.origin_prototype_id == proto_id,
            Quotation.status == QuotationStatus.DRAFT,
        )
    )
    assert borradores == 1


@pytest.mark.asyncio
async def test_una_muestra_puede_originar_varias_cotizaciones_a_lo_largo_del_tiempo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_MULTIPLE_HISTORICAL_QUOTES.

    Anular el borrador libera el hueco: recotizar la misma muestra mas
    adelante es legitimo, y el unico parcial esta puesto para permitirlo.
    """
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_hist")
    proto_id = datos["prototipo"]["id"]

    primera = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert primera.status_code == 201
    anulada = await api.post(f"{BUILDER}/{primera.json()['id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text

    segunda = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert segunda.status_code == 201, segunda.text
    assert segunda.json()["id"] != primera.json()["id"]

    db_session.expire_all()
    total = await db_session.scalar(
        select(func.count()).select_from(Quotation).where(Quotation.origin_prototype_id == proto_id)
    )
    assert total == 2


# ---------------------------------------------------------------------------
# Precarga
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_las_medidas_salen_de_la_ficha_estructurada_y_no_de_las_notas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DIMENSIONS_PREFILL + NOTES_PARSED_FOR_DIMENSIONS = 0.

    La muestra lleva unas medidas en la ficha y OTRAS distintas escritas en el
    texto libre. Si el puente leyera las notas, las de la nota ganarian.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_dim")
    proto_id = datos["prototipo"]["id"]
    editada = await api.put(
        f"{PROTOTYPES}/{proto_id}",
        json={
            "product_id": datos["producto"]["id"],
            "notes": "Especificaciones\nAncho cm: 99\nAlto cm: 99\nLargo cm: 99",
            "technical_specifications": {
                "width_cm": "12",
                "height_cm": "18",
                "length_cm": "24",
            },
        },
        headers=head(admin_csrf),
    )
    assert editada.status_code == 200, editada.text

    for accion in ("start", "complete"):
        await api.post(f"{PROTOTYPES}/{proto_id}/{accion}", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{proto_id}/approve", json={}, headers=head(admin_csrf))

    creada = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    linea = creada.json()["items"][0]
    assert Decimal(linea["width"]) == Decimal(12)
    assert Decimal(linea["height"]) == Decimal(18)
    assert Decimal(linea["length"]) == Decimal(24)
    assert linea["dimensions_overridden"] is True

    # Y el maestro del producto no se toca: la medida es de ESTA pieza.
    producto = await db_session.get(Product, datos["producto"]["id"])
    assert producto is not None
    assert producto.width != Decimal(12) or producto.height != Decimal(18)


@pytest.mark.asyncio
async def test_una_muestra_sin_ficha_no_precarga_medidas_inventadas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Lo historico no tiene ficha, y eso no es un defecto: es que no existio."""
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_nodim")
    creada = await api.post(_final(datos["prototipo"]["id"]), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    fila = await db_session.get(Prototype, datos["prototipo"]["id"])
    assert fila is not None
    assert fila.technical_specifications is None


@pytest.mark.asyncio
async def test_el_material_del_cuerpo_sale_del_consumo_real_dividido_entre_las_piezas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BODY_MATERIAL_USES_ACTUAL.

    Dos piezas de muestra que gastaron 60 g dan 30 g por pieza. Lo PREVISTO no
    se usa: la cotizacion hereda lo que de verdad costo hacerla.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_body", gramos="60")
    proto_id = datos["prototipo"]["id"]
    barro = datos["barro"]

    # Dos piezas de muestra, y el barro declarado como CUERPO.
    await _liga_producto(api, admin_csrf, datos, quantity=2)
    materiales = await api.put(
        f"{PROTOTYPES}/{proto_id}/materials",
        json={
            "materials": [{"product_id": barro["id"], "quantity": "60", "material_role": "BODY"}]
        },
        headers=head(admin_csrf),
    )
    assert materiales.status_code == 200, materiales.text

    for accion in ("start", "complete"):
        await api.post(f"{PROTOTYPES}/{proto_id}/{accion}", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{proto_id}/approve", json={}, headers=head(admin_csrf))

    creada = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    cuerpo = creada.json()["items"][0]["body_material"]
    assert cuerpo is not None
    assert cuerpo["product_id"] == barro["id"]
    assert Decimal(cuerpo["quantity_per_piece"]) == Decimal(30)
    assert cuerpo["uom"] == "g"


@pytest.mark.asyncio
async def test_sin_rol_declarado_no_se_adivina_el_material_del_cuerpo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """BODY_MATERIAL_FIRST_LINE_HEURISTIC = 0.

    La muestra gasto un material, pero nadie dijo que fuera el cuerpo. Coger
    «el primero» produciria una cotizacion con el material equivocado y un
    numero creible al lado.
    """
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_norole")
    creada = await api.post(_final(datos["prototipo"]["id"]), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    assert creada.json()["items"][0]["body_material"] is None


@pytest.mark.asyncio
async def test_con_dos_cuerpos_declarados_el_puente_no_elige(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Dos BODY es ambiguedad, y la ambiguedad la resuelve una persona."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_2body")
    await _liga_producto(api, admin_csrf, datos)
    proto_id = datos["prototipo"]["id"]
    segundo = await _material(api, admin_csrf, nombre="Arcilla E2E dos_br_2body")
    await api.post(
        "/api/v1/inventory/adjustments",
        json={
            "product_id": segundo["id"],
            "location_id": datos["location_id"],
            "quantity": "1000",
            "reason": "QA 009K.1",
        },
        headers=head(admin_csrf),
    )
    materiales = await api.put(
        f"{PROTOTYPES}/{proto_id}/materials",
        json={
            "materials": [
                {"product_id": datos["barro"]["id"], "quantity": "30", "material_role": "BODY"},
                {"product_id": segundo["id"], "quantity": "20", "material_role": "BODY"},
            ]
        },
        headers=head(admin_csrf),
    )
    assert materiales.status_code == 200, materiales.text

    for accion in ("start", "complete"):
        await api.post(f"{PROTOTYPES}/{proto_id}/{accion}", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{proto_id}/approve", json={}, headers=head(admin_csrf))

    creada = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    assert creada.json()["items"][0]["body_material"] is None


@pytest.mark.asyncio
async def test_el_esmalte_de_la_muestra_no_se_convierte_en_plan_de_esmaltes(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """GLAZE_SHARE_INVENTED = 0.

    La muestra registra gramos absolutos; el Cotizador reparte un total por
    pesos relativos. No hay traduccion honesta entre las dos cosas.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_glaze")
    await _liga_producto(api, admin_csrf, datos)
    proto_id = datos["prototipo"]["id"]
    materiales = await api.put(
        f"{PROTOTYPES}/{proto_id}/materials",
        json={
            "materials": [
                {"product_id": datos["barro"]["id"], "quantity": "30", "material_role": "GLAZE"}
            ]
        },
        headers=head(admin_csrf),
    )
    assert materiales.status_code == 200, materiales.text

    for accion in ("start", "complete"):
        await api.post(f"{PROTOTYPES}/{proto_id}/{accion}", headers=head(admin_csrf))
    await api.post(f"{PROTOTYPES}/{proto_id}/approve", json={}, headers=head(admin_csrf))

    creada = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    linea = creada.json()["items"][0]
    assert linea["body_material"] is None
    # El plan de esmaltes se configura como en cualquier otra cotizacion.
    assert linea["glaze_selection_touched"] is False


# ---------------------------------------------------------------------------
# Permisos
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_taller_no_cotiza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Cotizar es decidir un precio, y eso es administracion (matriz de 009J)."""
    datos = await _aprobada(api, admin_csrf, db_session, suffix="_br_rbac")
    antes = await _cotizaciones(db_session)

    # El cambio de sesion va AQUI, no en una fixture: el montaje de arriba se
    # hace como admin y le pisaria la cookie al operario, dejando su token
    # CSRF emparejado con la sesion equivocada.
    operario = await _como_operario(api)
    respuesta = await api.post(_final(datos["prototipo"]["id"]), headers=head(operario))
    assert respuesta.status_code == 403, respuesta.text
    assert await _cotizaciones(db_session) == antes


# ---------------------------------------------------------------------------
# El consumo real: lo escribe el arranque, y nadie mas
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_arrancar_escribe_el_consumo_real_junto_al_movimiento(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUANTITY_ACTUAL_ATOMIC.

    Lo previsto son 30 g. Tras arrancar, el movimiento saca 30 y la columna
    dice 30: son la misma cifra porque se escriben en la misma transaccion. Dos
    sitios diciendo cuanto se gasto acabarian discrepando.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_qa_ok", gramos="30")
    proto_id = datos["prototipo"]["id"]

    lineas_antes = await _lineas_material(db_session, proto_id)
    assert [linea.quantity_planned for linea in lineas_antes] == [Decimal(30)]
    assert [linea.quantity_actual for linea in lineas_antes] == [None]

    assert (
        await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(admin_csrf))
    ).status_code == 200

    lineas = await _lineas_material(db_session, proto_id)
    assert [linea.quantity_actual for linea in lineas] == [Decimal(30)]

    db_session.expire_all()
    salida = (
        (
            await db_session.execute(
                select(StockMovement.quantity).where(StockMovement.prototype_id == proto_id)
            )
        )
        .scalars()
        .all()
    )
    assert [Decimal(valor) for valor in salida] == [Decimal(-30)]


@pytest.mark.asyncio
async def test_un_arranque_que_falla_no_deja_consumo_registrado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """FAILED_START_QUANTITY_ACTUAL_MUTATION: 0.

    Sin saldo suficiente el arranque se rechaza, y la transaccion se deshace
    entera: no puede quedar un consumo anotado que el almacen no respalde.
    """
    datos = await _muestra_lista(
        api, admin_csrf, db_session, suffix="_qa_fail", gramos="30", existencia="5"
    )
    proto_id = datos["prototipo"]["id"]

    respuesta = await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text

    lineas = await _lineas_material(db_session, proto_id)
    assert [linea.quantity_actual for linea in lineas] == [None]
    db_session.expire_all()
    salidas = await db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.prototype_id == proto_id)
    )
    assert salidas == 0


@pytest.mark.asyncio
async def test_arrancar_dos_veces_no_reescribe_el_consumo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """SECOND_START_QUANTITY_ACTUAL_MUTATION: 0."""
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_qa_twice", gramos="30")
    proto_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(admin_csrf))

    lineas_primero = await _lineas_material(db_session, proto_id)
    await api.post(f"{PROTOTYPES}/{proto_id}/start", headers=head(admin_csrf))
    lineas_despues = await _lineas_material(db_session, proto_id)

    assert [linea.quantity_actual for linea in lineas_despues] == [
        linea.quantity_actual for linea in lineas_primero
    ]
    db_session.expire_all()
    salidas = await db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.prototype_id == proto_id)
    )
    assert salidas == 1


@pytest.mark.asyncio
async def test_una_muestra_sin_producto_da_un_borrador_vacio_y_no_uno_inventado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Una muestra puede no decir de que producto es, y entonces no hay que precargar.

    El puente no elige un producto por parecido ni deja la linea a medias con
    un hueco donde iria: devuelve el borrador sin lineas y que la persona que
    cotiza ponga el producto. Precargar aqui seria adivinar.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_sinprod")
    proto_id = datos["prototipo"]["id"]
    for accion in ("start", "complete"):
        assert (
            await api.post(f"{PROTOTYPES}/{proto_id}/{accion}", headers=head(admin_csrf))
        ).status_code == 200
    assert (
        await api.post(f"{PROTOTYPES}/{proto_id}/approve", json={}, headers=head(admin_csrf))
    ).status_code == 200

    creada = await api.post(_final(proto_id), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    assert creada.json()["items"] == []

    fila = await db_session.get(Quotation, creada.json()["id"])
    assert fila is not None
    assert fila.origin_prototype_id == proto_id


@pytest.mark.asyncio
async def test_lo_que_se_declara_al_dar_de_alta_la_muestra_se_guarda(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La ficha y el rol del material sobreviven al alta, no solo a la edicion.

    Aceptar un campo en el esquema y no escribirlo devuelve 201 y pierde el
    dato en silencio: el taller rellena la ficha, la ve confirmada, y el puente
    no encuentra nada que precargar. Se comprueba releyendo desde la base y no
    desde la respuesta, porque una respuesta puede reflejar lo que se mando en
    vez de lo que se guardo.
    """
    barro = await _material(api, admin_csrf, nombre="Arcilla alta 009K1")
    creado = await api.post(
        PROTOTYPES,
        json={
            "name": "E2E-009K1 alta",
            "quantity": 1,
            "technical_specifications": {"width_cm": "12", "technique": "Torno"},
            "materials": [{"product_id": barro["id"], "quantity": "30", "material_role": "BODY"}],
        },
        headers=head(admin_csrf),
    )
    assert creado.status_code == 201, creado.text

    db_session.expire_all()
    fila = await db_session.get(Prototype, creado.json()["id"])
    assert fila is not None
    # `evaluation` viaja con su valor por defecto: la ficha admite criterios y
    # esta muestra no declaro ninguno. Se afirma el dict COMPLETO a proposito,
    # para que un campo nuevo que empiece a guardarse solo se vea aqui.
    assert fila.technical_specifications == {
        "width_cm": "12",
        "technique": "Torno",
        "evaluation": [],
    }

    lineas = await _lineas_material(db_session, fila.id)
    assert [linea.material_role for linea in lineas] == [PrototypeMaterialRole.BODY]


@pytest.mark.asyncio
async def test_editar_la_ficha_la_guarda_de_verdad(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """TECHNICAL_SPECIFICATIONS_PERSIST_ON_UPDATE.

    Se relee desde PostgreSQL despues del commit, y no de la respuesta del PUT:
    una respuesta se construye desde el objeto en memoria y puede devolver
    intacto lo que se acaba de mandar aunque no haya llegado a la columna.

    La ficha se reemplaza ENTERA. Que la tecnica anterior desaparezca al mandar
    solo el ancho no es una perdida: es un formulario, y conservar a medias
    daria una ficha que nadie escribio.
    """
    creado = await api.post(
        PROTOTYPES,
        json={
            "name": "E2E-009K1 ficha",
            "quantity": 1,
            "technical_specifications": {"width_cm": "9", "technique": "Colado"},
        },
        headers=head(admin_csrf),
    )
    assert creado.status_code == 201, creado.text
    proto_id = creado.json()["id"]

    editada = await api.put(
        f"{PROTOTYPES}/{proto_id}",
        json={"technical_specifications": {"width_cm": "12", "height_cm": "18"}},
        headers=head(admin_csrf),
    )
    assert editada.status_code == 200, editada.text

    db_session.expire_all()
    fila = await db_session.get(Prototype, proto_id)
    assert fila is not None
    # Exacto, no un subconjunto: lo que se prueba es que la ficha se REEMPLAZA.
    # Si `technique` sobreviviera al PUT que no lo menciona, esto se cae.
    assert fila.technical_specifications == {
        "width_cm": "12",
        "height_cm": "18",
        "evaluation": [],
    }


@pytest.mark.asyncio
async def test_la_muestra_sucesora_hereda_la_ficha_y_los_roles(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """TECHNICAL_SPECIFICATIONS_SUCCESSOR_PRESERVED.

    Repetir una muestra rechazada es rehacer la MISMA pieza: la ficha tecnica y
    el rol de cada material son de la pieza, no del intento. Las notas si son
    nuevas, porque explican por que se repite.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_br_suc")
    proto_id = datos["prototipo"]["id"]
    ficha = await api.put(
        f"{PROTOTYPES}/{proto_id}",
        json={"technical_specifications": {"width_cm": "15", "mold": "Molde 3"}},
        headers=head(admin_csrf),
    )
    assert ficha.status_code == 200, ficha.text
    materiales = await api.put(
        f"{PROTOTYPES}/{proto_id}/materials",
        json={
            "materials": [
                {"product_id": datos["barro"]["id"], "quantity": "40", "material_role": "BODY"}
            ]
        },
        headers=head(admin_csrf),
    )
    assert materiales.status_code == 200, materiales.text

    for accion in ("start", "complete"):
        respuesta = await api.post(f"{PROTOTYPES}/{proto_id}/{accion}", headers=head(admin_csrf))
        assert respuesta.status_code == 200, respuesta.text
    rechazo = await api.post(f"{PROTOTYPES}/{proto_id}/reject", json={}, headers=head(admin_csrf))
    assert rechazo.status_code == 200, rechazo.text

    sucesora = await api.post(
        f"{PROTOTYPES}/{proto_id}/successor",
        json={"notes": "Se repite: la boca quedo ovalada"},
        headers=head(admin_csrf),
    )
    assert sucesora.status_code == 201, sucesora.text

    db_session.expire_all()
    fila = await db_session.get(Prototype, sucesora.json()["id"])
    assert fila is not None
    assert fila.technical_specifications == {
        "width_cm": "15",
        "mold": "Molde 3",
        "evaluation": [],
    }
    assert fila.notes == "Se repite: la boca quedo ovalada"

    lineas = await _lineas_material(db_session, fila.id)
    assert [linea.material_role for linea in lineas] == [PrototypeMaterialRole.BODY]
