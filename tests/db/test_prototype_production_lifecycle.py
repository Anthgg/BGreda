"""Fase 009K.4 — lo que rodea a la orden de una muestra.

Tres cosas, y las tres son de coherencia mas que de funcionalidad:

1. **El material sale de la muestra.** De sus lineas, elegidas a mano, y no de
   una receta sintetizada para que encajara en el modelo de la cotizacion. Un
   movimiento por producto efectivo, ni uno mas.
2. **La evaluacion y las iteraciones no se mudan.** Siguen viviendo en el
   prototipo, que es su autoridad; la orden solo abre la puerta. Y una sucesora
   que hay que volver a fabricar recibe SU orden, nunca la de la anterior.
3. **Lo historico se queda quieto.** Las 11 muestras anteriores a esta fase no
   tienen orden, y abrirlas no se la crea: un GET que materializara una orden
   convertiria leer en gestionar, y les inventaria un almacen que nadie eligio.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import MovementType, StockMovement
from app.models.production import ProductionOrder, ProductionOrderStatus
from app.models.prototypes import Prototype, PrototypeStatus
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_production_orders_api import crear_ubicacion, dar_existencia
from tests.db.test_prototype_production_order import ORDENES, _cpr_confirmada
from tests.db.test_prototype_quotations import COTIZADOR, _payload, cobrar
from tests.db.test_prototypes import _muestra_lista as _muestra_por_el_camino_antiguo
from tests.db.test_quotation_builder_api import head

PROTOTIPOS = "/api/v1/prototypes"


async def _muestra_lista(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    sufijo: str,
    *,
    existencia: str = "10000",
) -> dict[str, Any]:
    """Una muestra cobrada, con su orden y con barro en el almacen."""
    escenario = await _cpr_confirmada(api, csrf, db_session, sufijo)
    almacen = await crear_ubicacion(api, csrf, f"Almacen ciclo{sufijo}")
    pasta = escenario["caso"]["_pasta"]
    await dar_existencia(
        api, csrf, product_id=pasta["id"], location_id=almacen, cantidad=existencia
    )
    pagada = await cobrar(api, csrf, escenario["documento"]["id"], stock_location_id=almacen)
    assert pagada.status_code == 200, pagada.text
    return {
        "almacen": almacen,
        "pasta": pasta,
        "cpr": escenario["documento"],
        "cobro": pagada.json(),
        "orden_id": pagada.json()["production_order_id"],
        "muestra_id": pagada.json()["prototype_id"],
    }


# ---------------------------------------------------------------------------
# B14 + B15: el material es el de la muestra, y no hay receta que valga
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_material_sale_de_las_lineas_de_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B14 + B15 + PROTOTYPE_START_PROTOTYPE_OUT_COUNT.

    Con DOS materiales, que es donde se nota: si la orden dedujera el material
    de una receta sintetizada saldria uno solo —el cuerpo—, y el esmalte se
    gastaria sin que el inventario se enterara.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_mats")
    categoria = await create_category(api, admin_csrf, "Esmaltes k4 mats")
    segundo = await create_product(
        api,
        admin_csrf,
        product_category_id=categoria["id"],
        product_type="RAW_MATERIAL",
        name="Esmalte prototipo k4",
        base_uom_code="kg",
        cost="20",
    )
    assert segundo.status_code == 201, segundo.text
    esmalte = segundo.json()

    caso = dict(escenario["caso"])
    caso["materials"] = [
        *caso["materials"],
        {"product_id": esmalte["id"], "quantity_per_prototype": "0.4"},
    ]
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(
        f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(admin_csrf)
    )
    assert confirmada.status_code == 200, confirmada.text

    almacen = await crear_ubicacion(api, admin_csrf, "Almacen k4 mats")
    for producto in (escenario["caso"]["_pasta"], esmalte):
        await dar_existencia(
            api, admin_csrf, product_id=producto["id"], location_id=almacen, cantidad="10000"
        )
    pagada = await cobrar(api, admin_csrf, confirmada.json()["id"], stock_location_id=almacen)
    assert pagada.status_code == 200, pagada.text
    orden_id = pagada.json()["production_order_id"]
    muestra_id = pagada.json()["prototype_id"]

    # La orden sigue teniendo UNA linea —una muestra es una pieza— y esa linea
    # no tiene receta ni preparado: el material no vive ahi.
    detalle = (await api.get(f"{ORDENES}/{orden_id}", headers=head(admin_csrf))).json()
    assert len(detalle["lines"]) == 1
    assert detalle["lines"][0]["recipe_id"] is None
    assert detalle["lines"][0]["recipe_version_id"] is None
    assert detalle["lines"][0]["prepared_product_id"] is None

    arrancada = await api.post(f"{ORDENES}/{orden_id}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    db_session.expire_all()
    movimientos = list(
        (
            await db_session.execute(
                select(StockMovement).where(StockMovement.prototype_id == muestra_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(movimientos) == 2, "uno por material efectivo, ni uno mas"
    assert all(m.movement_type is MovementType.PROTOTYPE_OUT for m in movimientos)
    gastado = {m.product_id: -m.quantity for m in movimientos}
    assert gastado[escenario["caso"]["_pasta"]["id"]] == Decimal("1.25")
    assert gastado[esmalte["id"]] == Decimal("0.4")


# ---------------------------------------------------------------------------
# Parte 5: el estado de la muestra no contradice al de su orden
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_completar_la_orden_completa_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCTION_ORDER_AND_PRT_STATE_CONSISTENCY.

    No es cosmetico: aprobar o rechazar una muestra exige que este COMPLETED.
    Una orden terminada con la muestra todavia en STARTED la dejaria imposible
    de evaluar, que es justo el paso siguiente.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, "_k4_estado")
    await api.post(f"{ORDENES}/{datos['orden_id']}/start", headers=head(admin_csrf))
    completada = await api.post(f"{ORDENES}/{datos['orden_id']}/complete", headers=head(admin_csrf))
    assert completada.status_code == 200, completada.text

    db_session.expire_all()
    muestra = await db_session.get(Prototype, datos["muestra_id"])
    assert muestra is not None
    assert muestra.status is PrototypeStatus.COMPLETED
    assert muestra.completed_at is not None
    assert muestra.approval.value == "PENDING", "terminar de fabricar no es dar por buena"


@pytest.mark.asyncio
async def test_anular_la_orden_anula_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Una muestra viva cuya unica orden esta anulada no podria fabricarse.

    El UNIQUE de `prototype_id` impide darle una segunda orden, asi que
    quedaria para siempre en CREADA sin forma de avanzar.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, "_k4_anula")
    anulada = await api.post(f"{ORDENES}/{datos['orden_id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text

    db_session.expire_all()
    muestra = await db_session.get(Prototype, datos["muestra_id"])
    assert muestra is not None
    assert muestra.status is PrototypeStatus.CANCELLED
    assert muestra.cancelled_at is not None


# ---------------------------------------------------------------------------
# B31 + B32 + B33: evaluar e iterar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_evaluacion_se_guarda_en_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B31. PROTOTYPE_EVALUATION_DATA_PRESERVED.

    Esta fase mueve la EJECUCION a la orden, no el veredicto. La aprobacion y
    su fecha siguen en el prototipo, que es donde estaban, y la orden solo lleva
    hasta el: duplicar el dato en las dos tablas seria tener dos respuestas para
    la misma pregunta.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, "_k4_evalua")
    await api.post(f"{ORDENES}/{datos['orden_id']}/start", headers=head(admin_csrf))
    await api.post(f"{ORDENES}/{datos['orden_id']}/complete", headers=head(admin_csrf))

    aprobada = await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/approve",
        json={"note": "Sirve"},
        headers=head(admin_csrf),
    )
    assert aprobada.status_code == 200, aprobada.text
    assert aprobada.json()["approval"] == "APPROVED"
    assert aprobada.json()["decided_at"] is not None

    db_session.expire_all()
    muestra = await db_session.get(Prototype, datos["muestra_id"])
    assert muestra is not None
    assert muestra.approval.value == "APPROVED"
    assert muestra.decided_at is not None

    # Y se llega desde la orden: el detalle dice cual es su muestra.
    detalle = (await api.get(f"{ORDENES}/{datos['orden_id']}", headers=head(admin_csrf))).json()
    assert detalle["prototype_id"] == datos["muestra_id"]


@pytest.mark.asyncio
async def test_una_muestra_rechazada_se_repite_con_su_propia_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B32 + B33. NEW_PHYSICAL_ITERATION_NEW_PRODUCTION_ORDER.

    La cadena de iteraciones se conserva entera y la sucesora recibe SU orden.
    Reutilizar la de la anterior mezclaria dos fabricaciones distintas —dos
    consumos, dos fechas, dos hojas de taller— en un solo documento.
    """
    datos = await _muestra_lista(api, admin_csrf, db_session, "_k4_itera")
    await api.post(f"{ORDENES}/{datos['orden_id']}/start", headers=head(admin_csrf))
    await api.post(f"{ORDENES}/{datos['orden_id']}/complete", headers=head(admin_csrf))

    rechazada = await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/reject",
        json={"note": "El vidriado no cuajó"},
        headers=head(admin_csrf),
    )
    assert rechazada.status_code == 200, rechazada.text

    sucesora = await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/successor",
        json={"notes": "Segunda vuelta"},
        headers=head(admin_csrf),
    )
    assert sucesora.status_code == 201, sucesora.text
    hija = sucesora.json()
    assert hija["supersedes_prototype_id"] == datos["muestra_id"]
    assert hija["production_order_id"] is None, "nacer sucesora no fabrica nada todavía"

    # La anterior no se reescribe: un rechazo es un hecho.
    anterior = (
        await api.get(f"{PROTOTIPOS}/{datos['muestra_id']}", headers=head(admin_csrf))
    ).json()
    assert anterior["approval"] == "REJECTED"
    assert anterior["production_order_id"] == datos["orden_id"]

    nueva = await api.post(
        ORDENES,
        json={"prototype_id": hija["id"], "stock_location_id": datos["almacen"]},
        headers=head(admin_csrf),
    )
    assert nueva.status_code == 201, nueva.text
    assert nueva.json()["id"] != datos["orden_id"], "la sucesora no hereda la orden anterior"
    assert nueva.json()["origin_type"] == "PROTOTYPE"
    assert nueva.json()["prototype_id"] == hija["id"]

    ordenes = await db_session.scalar(
        select(func.count())
        .select_from(ProductionOrder)
        .where(ProductionOrder.prototype_id.in_([datos["muestra_id"], hija["id"]]))
    )
    assert ordenes == 2, "una orden por muestra, cada una con la suya"


@pytest.mark.asyncio
async def test_no_se_le_crea_orden_a_una_muestra_ya_fabricada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LEGACY_PRODUCTION_ORDER_BACKFILL: NO.

    La puerta que abre esta fase para las iteraciones no sirve para rellenar
    hacia atras. Una muestra ya arrancada gasto su material por el camino que
    fuera; darle ahora una orden arrancable seria invitar a gastarlo dos veces.
    """
    datos = await _muestra_por_el_camino_antiguo(api, admin_csrf, db_session, suffix="_k4_backfill")
    muestra_id = datos["prototipo"]["id"]
    arrancada = await api.post(f"{PROTOTIPOS}/{muestra_id}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    respuesta = await api.post(
        ORDENES,
        json={"prototype_id": muestra_id, "stock_location_id": datos["location_id"]},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "PRODUCTION_ORDER_PROTOTYPE_NOT_PRODUCIBLE"

    db_session.expire_all()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProductionOrder)
            .where(ProductionOrder.prototype_id == muestra_id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_un_alta_de_orden_exige_un_origen_y_solo_uno(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La misma regla que el CHECK de la tabla, dicha antes de llegar a ella.

    Un 422 explica que falta; un error de PostgreSQL en la respuesta no le dice
    nada a quien lo lee.
    """
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen k4 origen alta")

    sin_origen = await api.post(
        ORDENES, json={"stock_location_id": almacen}, headers=head(admin_csrf)
    )
    assert sin_origen.status_code == 422, sin_origen.text

    con_los_dos = await api.post(
        ORDENES,
        json={"quotation_id": 1, "prototype_id": 1, "stock_location_id": almacen},
        headers=head(admin_csrf),
    )
    assert con_los_dos.status_code == 422, con_los_dos.text


# ---------------------------------------------------------------------------
# B34 + B35: lo historico se queda quieto
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_abrir_una_muestra_sin_orden_no_le_crea_una(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B34. LEGACY_PRT_GET_CREATES_PRODUCTION_ORDER: NO.

    Son 11 en produccion. Leerlas tiene que seguir siendo leer: crear la orden
    al abrirlas les inventaria un almacen que nadie eligio y convertiria una
    consulta en un acto de gestion.
    """
    antes = await db_session.scalar(select(func.count()).select_from(ProductionOrder))
    creada = await api.post(
        PROTOTIPOS,
        json={"name": "Muestra sin orden k4", "quantity": 1, "materials": []},
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    muestra_id = creada.json()["id"]

    for _ in range(3):
        ficha = await api.get(f"{PROTOTIPOS}/{muestra_id}", headers=head(admin_csrf))
        assert ficha.status_code == 200, ficha.text
        assert ficha.json()["production_order_id"] is None
        assert ficha.json()["production_order_code"] is None

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(ProductionOrder)) == antes
    ligadas = await db_session.scalar(
        select(func.count())
        .select_from(ProductionOrder)
        .where(ProductionOrder.prototype_id == muestra_id)
    )
    assert ligadas == 0


@pytest.mark.asyncio
async def test_una_muestra_arrancada_por_el_camino_antiguo_no_se_toca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B35. LEGACY_STARTED_PRT_PRESERVED.

    En produccion hay una asi. Su consumo ya ocurrio, su `PROTOTYPE_OUT` ya
    esta escrito, y esta fase no lo revisa ni lo repite: solo tiene que seguir
    leyendose.
    """
    datos = await _muestra_por_el_camino_antiguo(api, admin_csrf, db_session, suffix="_k4_legado")
    muestra_id = datos["prototipo"]["id"]
    arrancada = await api.post(f"{PROTOTIPOS}/{muestra_id}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    db_session.expire_all()
    movimientos = list(
        (
            await db_session.execute(
                select(StockMovement).where(StockMovement.prototype_id == muestra_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(movimientos) == 1
    assert movimientos[0].movement_type is MovementType.PROTOTYPE_OUT

    # Se lee tantas veces como haga falta y sigue igual: sin orden, arrancada.
    for _ in range(3):
        ficha = await api.get(f"{PROTOTIPOS}/{muestra_id}", headers=head(admin_csrf))
        assert ficha.status_code == 200, ficha.text
        assert ficha.json()["status"] == "STARTED"
        assert ficha.json()["production_order_id"] is None

    db_session.expire_all()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProductionOrder)
            .where(ProductionOrder.prototype_id == muestra_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.prototype_id == muestra_id)
        )
        == 1
    ), "leerla no vuelve a consumir"


# ---------------------------------------------------------------------------
# B48: ni con un solo almacen activo se elige solo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_con_un_unico_almacen_activo_el_cobro_sigue_exigiendolo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B48. ONLY_ONE_ACTIVE_LOCATION_AUTO_SELECTED: NO.

    Es la tentacion evidente —«si solo hay uno, es ese»— y es justo la que hay
    que resistir: el dia que haya dos, el default silencioso descontara del
    equivocado sin que nadie lo note, y para entonces la costumbre de no elegir
    ya estara aprendida.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_unico")
    unico = await crear_ubicacion(api, admin_csrf, "Almacen único k4")
    await db_session.execute(
        text("UPDATE stock_locations SET active = false WHERE id <> :id"), {"id": unico}
    )
    await db_session.commit()
    activos = await db_session.scalar(
        select(func.count()).select_from(text("stock_locations")).where(text("active"))
    )
    assert activos == 1, "el escenario que se quiere probar"

    respuesta = await api.post(
        f"{COTIZADOR}/{escenario['documento']['id']}/mark-paid",
        json={},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422, respuesta.text

    db_session.expire_all()
    documento = await api.get(
        f"{COTIZADOR}/{escenario['documento']['id']}", headers=head(admin_csrf)
    )
    assert documento.json()["payment_status"] != "PAID"
    assert documento.json()["production_order_id"] is None

    # Y con el almacen dicho a mano —el mismo unico— si cobra.
    await db_session.execute(text("UPDATE stock_locations SET active = true"))
    await db_session.commit()
    pagada = await cobrar(api, admin_csrf, escenario["documento"]["id"], stock_location_id=unico)
    assert pagada.status_code == 200, pagada.text
    assert pagada.json()["production_order_id"] is not None


@pytest.mark.asyncio
async def test_veinte_cobros_simultaneos_dejan_una_sola_orden(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """B05. La unicidad la impone la BASE, no la comprobacion previa.

    Veinte peticiones a la vez pasan todas la lectura de «¿ya tiene orden?»
    antes de que ninguna haya escrito. Lo que impide la segunda es el UNIQUE de
    `prototype_id`, y lo que impide el 500 es el cerrojo del cobro.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_k4_conc")
    almacen = await crear_ubicacion(api, admin_csrf, "Almacen k4 concurrente")

    respuestas = await asyncio.gather(
        *(
            api.post(
                f"{COTIZADOR}/{escenario['documento']['id']}/mark-paid",
                json={"stock_location_id": almacen},
                headers=head(admin_csrf),
            )
            for _ in range(20)
        ),
        return_exceptions=True,
    )
    correctas = [r for r in respuestas if isinstance(r, httpx.Response) and r.status_code == 200]
    assert correctas, [str(r) for r in respuestas][:3]
    assert not [r for r in respuestas if isinstance(r, httpx.Response) and r.status_code >= 500]
    assert len({r.json()["production_order_id"] for r in correctas}) == 1

    db_session.expire_all()
    muestra_id = correctas[0].json()["prototype_id"]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ProductionOrder)
            .where(ProductionOrder.prototype_id == muestra_id)
        )
        == 1
    )
    orden = await db_session.get(ProductionOrder, correctas[0].json()["production_order_id"])
    assert orden is not None
    assert orden.status is ProductionOrderStatus.CREATED
