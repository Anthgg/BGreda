"""Fase 009K.4 (addendum) — anular, declarar el cuerpo y conservar la genealogía.

Tres cierres sobre lo ya construido, y ninguno es cosmético:

1. **Anular la orden anula la muestra.** Una orden de producción es un intento
   físico; si se cancela antes de producir, el intento no ocurrió. Dejar la
   muestra viva la condenaba además a no fabricarse nunca, porque el UNIQUE de
   `prototype_id` impide darle una segunda orden. Lo que NO se toca al anular:
   el inventario, la cotización de prototipo, el producto y las líneas de
   material.

2. **El cuerpo declarado en la CPR llega a la muestra como rol.** La cotización
   dice cuál de sus materiales es el barro de la pieza; hasta aquí ese dato se
   perdía al materializar, y era el único que permite saber qué material puede
   viajar después al Cotizador como material base.

3. **La sucesora hereda la cotización de prototipo.** Repetir una muestra no es
   un encargo nuevo: es el mismo, que salió mal a la primera. Sin heredarla, la
   cadena perdía su origen comercial en el primer intento fallido.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockMovement
from app.models.masters import Product
from app.models.production import ProductionOrder, ProductionOrderStatus
from app.models.prototype_quotations import PrototypeQuotation
from app.models.prototypes import Prototype, PrototypeMaterialLine, PrototypeStatus
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_production_orders_api import crear_ubicacion, dar_existencia
from tests.db.test_prototype_production_order import ORDENES, _cpr_confirmada
from tests.db.test_prototype_quotations import COTIZADOR, _payload, cobrar
from tests.db.test_quotation_builder_api import head

PROTOTIPOS = "/api/v1/prototypes"


async def _lineas(db_session: AsyncSession, prototype_id: int) -> list[PrototypeMaterialLine]:
    filas = await db_session.execute(
        select(PrototypeMaterialLine)
        .where(PrototypeMaterialLine.prototype_id == prototype_id)
        .order_by(PrototypeMaterialLine.sort_order, PrototypeMaterialLine.id)
    )
    return list(filas.scalars().all())


async def _cobrada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, sufijo: str
) -> dict[str, Any]:
    """Una cotización de prototipo cobrada, con su muestra y su orden."""
    escenario = await _cpr_confirmada(api, csrf, db_session, sufijo)
    almacen = await crear_ubicacion(api, csrf, f"Almacen add{sufijo}")
    pagada = await cobrar(api, csrf, escenario["documento"]["id"], stock_location_id=almacen)
    assert pagada.status_code == 200, pagada.text
    return {
        "almacen": almacen,
        "pasta": escenario["caso"]["_pasta"],
        "cpr_id": escenario["documento"]["id"],
        "cpr_code": escenario["documento"]["code"],
        "cobro": pagada.json(),
        "orden_id": pagada.json()["production_order_id"],
        "muestra_id": pagada.json()["prototype_id"],
        "product_id": pagada.json()["product_id"],
    }


# ---------------------------------------------------------------------------
# C01-C07 — anular la orden de una muestra
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_anular_la_orden_anula_la_muestra_y_no_toca_nada_mas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """C01 + C02 + C03 + C04 + C05 + C06.

    Anular es la vuelta atrás de un intento que no llegó a gastar nada, así que
    no puede gastar nada al deshacerse: ni un movimiento de inventario, ni una
    línea de material borrada, ni el cobro revertido. El cliente pagó, y eso
    sigue siendo cierto aunque el taller no llegara a empezar.
    """
    datos = await _cobrada(api, admin_csrf, db_session, "_add_cancel")
    movimientos_antes = await db_session.scalar(select(func.count()).select_from(StockMovement))
    lineas_antes = await _lineas(db_session, datos["muestra_id"])
    assert lineas_antes, "el escenario tiene materiales que no deben desaparecer"

    anulada = await api.post(f"{ORDENES}/{datos['orden_id']}/cancel", headers=head(admin_csrf))
    assert anulada.status_code == 200, anulada.text
    assert anulada.json()["status"] == "CANCELLED"

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, datos["orden_id"])
    muestra = await db_session.get(Prototype, datos["muestra_id"])
    assert orden is not None and muestra is not None

    # C01 + C02: los dos, en la misma transacción.
    assert orden.status is ProductionOrderStatus.CANCELLED
    assert orden.cancelled_at is not None
    assert muestra.status is PrototypeStatus.CANCELLED
    assert muestra.cancelled_at is not None

    # C03 + C04: el almacén no se entera. No había nada que devolver.
    assert (
        await db_session.scalar(select(func.count()).select_from(StockMovement))
        == movimientos_antes
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.prototype_id == datos["muestra_id"])
        )
        == 0
    )

    # C05: el cobro sigue cobrado. Anular el intento físico no devuelve dinero.
    cpr = await db_session.get(PrototypeQuotation, datos["cpr_id"])
    assert cpr is not None
    assert cpr.payment_status.value == "PAID"
    assert cpr.status.value == "CONFIRMED"

    # C06: el producto del catálogo se queda como estaba.
    producto = await db_session.get(Product, datos["product_id"])
    assert producto is not None
    assert producto.active is True

    # Y las líneas de material no se borran: son la historia de lo que se iba
    # a gastar, y siguen explicando la muestra anulada.
    lineas_despues = await _lineas(db_session, datos["muestra_id"])
    assert [linea.id for linea in lineas_despues] == [linea.id for linea in lineas_antes]


@pytest.mark.asyncio
async def test_una_orden_arrancada_no_se_puede_anular(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """C07. STARTED_PROTOTYPE_ORDER_CAN_BE_CANCELLED: NO.

    Ya gastó material, y anularla no lo devuelve al saco. Permitirlo dejaría el
    inventario contando una cosa y el documento diciendo otra; si hubo un
    error, se corrige con un ajuste, que deja su propio responsable.
    """
    datos = await _cobrada(api, admin_csrf, db_session, "_add_cancel_start")
    await dar_existencia(
        api,
        admin_csrf,
        product_id=datos["pasta"]["id"],
        location_id=datos["almacen"],
        cantidad="10000",
    )
    arrancada = await api.post(f"{ORDENES}/{datos['orden_id']}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    negada = await api.post(f"{ORDENES}/{datos['orden_id']}/cancel", headers=head(admin_csrf))
    assert negada.status_code == 409, negada.text

    db_session.expire_all()
    orden = await db_session.get(ProductionOrder, datos["orden_id"])
    muestra = await db_session.get(Prototype, datos["muestra_id"])
    assert orden is not None and muestra is not None
    assert orden.status is ProductionOrderStatus.STARTED
    assert muestra.status is PrototypeStatus.STARTED, "la muestra tampoco se anula"


# ---------------------------------------------------------------------------
# M01-M05 — el cuerpo declarado en la cotización
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_cuerpo_declarado_en_la_cotizacion_llega_como_rol(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """M01 + M04 + M05. CPR_BODY_MATERIAL_TO_PRT_ROLE: BODY.

    Con DOS materiales, que es donde se distingue traducir de adivinar: uno
    declarado cuerpo y otro no. El segundo se queda SIN rol, porque «no es el
    cuerpo» no significa «es un esmalte»: la cotización no lo dice y nadie lo
    decidió. La etapa se queda nula por lo mismo: no hay fuente.
    """
    escenario = await _cpr_confirmada(api, admin_csrf, db_session, "_add_body")
    categoria = await create_category(api, admin_csrf, "Esmaltes add body")
    segundo = await create_product(
        api,
        admin_csrf,
        product_category_id=categoria["id"],
        product_type="RAW_MATERIAL",
        name="Esmalte add body",
        base_uom_code="kg",
        cost="20",
    )
    assert segundo.status_code == 201, segundo.text
    esmalte = segundo.json()

    caso = dict(escenario["caso"])
    caso["materials"] = [
        # El primero llega con `is_body_material=True` desde `_caso_referencia`.
        *caso["materials"],
        {"product_id": esmalte["id"], "quantity_per_prototype": "0.4"},
    ]
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(
        f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(admin_csrf)
    )
    assert confirmada.status_code == 200, confirmada.text

    almacen = await crear_ubicacion(api, admin_csrf, "Almacen add body")
    pagada = await cobrar(api, admin_csrf, confirmada.json()["id"], stock_location_id=almacen)
    assert pagada.status_code == 200, pagada.text

    db_session.expire_all()
    lineas = await _lineas(db_session, pagada.json()["prototype_id"])
    por_producto = {linea.product_id: linea for linea in lineas}

    cuerpo = por_producto[escenario["caso"]["_pasta"]["id"]]
    assert cuerpo.material_role is not None
    assert cuerpo.material_role.value == "BODY"

    # M04: el que no se declaró cuerpo no se convierte en cuerpo... ni en nada.
    otro = por_producto[esmalte["id"]]
    assert otro.material_role is None, "«no es el cuerpo» no es un rol"

    # M05: sin fuente para la etapa, la etapa se queda nula.
    assert all(linea.stage is None for linea in lineas)


@pytest.mark.asyncio
async def test_la_ficha_de_la_orden_ensena_el_rol_del_cuerpo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """M02. El rol viaja hasta la pantalla, que es donde sirve de algo."""
    datos = await _cobrada(api, admin_csrf, db_session, "_add_body_api")

    detalle = await api.get(f"{PROTOTIPOS}/{datos['muestra_id']}", headers=head(admin_csrf))
    assert detalle.status_code == 200, detalle.text
    materiales = detalle.json()["materials"]
    assert materiales, "la muestra tiene material"
    assert materiales[0]["material_role"] == "BODY"
    assert materiales[0]["stage"] is None

    # Y la orden sigue llevando hasta esa muestra, que es su autoridad.
    orden = await api.get(f"{ORDENES}/{datos['orden_id']}", headers=head(admin_csrf))
    assert orden.json()["prototype_id"] == datos["muestra_id"]


@pytest.mark.asyncio
async def test_la_hoja_de_taller_no_cambia_por_el_rol(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """M03. El renderer no expone el rol, y esta fase no se lo añade.

    La hoja de taller dice QUÉ material y CUÁNTO, que es lo que hace falta para
    ir al almacén. El rol distingue el cuerpo para el Cotizador, no para quien
    pesa el barro: meterlo en el papel sería ruido, y el addendum sólo autoriza
    enseñarlo «si el renderer lo expone». No lo expone.
    """
    datos = await _cobrada(api, admin_csrf, db_session, "_add_body_pdf")

    documento = await api.get(f"{ORDENES}/{datos['orden_id']}/document", headers=head(admin_csrf))
    assert documento.status_code == 200, documento.text
    assert documento.content.startswith(b"%PDF-")

    # Sigue llevando lo suyo: el material y su cantidad.
    from tests.db.test_prototype_production_document import _seguido

    texto = _seguido(documento.content)
    assert str(datos["pasta"]["name"]) in texto
    assert "1.25 kg" in texto


# ---------------------------------------------------------------------------
# S01-S08 — la genealogía de una iteración
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_sucesora_hereda_la_cotizacion_de_prototipo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """S01 a S07. SUCCESSOR_INHERITS_PROTOTYPE_QUOTATION_ID.

    Repetir una muestra no es un encargo nuevo: es el mismo, que salió mal a la
    primera. Sin heredar la cotización, la cadena perdía su origen comercial en
    el primer intento fallido y la orden de la sucesora no sabía decir de qué
    cobro venía.
    """
    datos = await _cobrada(api, admin_csrf, db_session, "_add_linaje")
    await dar_existencia(
        api,
        admin_csrf,
        product_id=datos["pasta"]["id"],
        location_id=datos["almacen"],
        cantidad="10000",
    )
    await api.post(f"{ORDENES}/{datos['orden_id']}/start", headers=head(admin_csrf))
    await api.post(f"{ORDENES}/{datos['orden_id']}/complete", headers=head(admin_csrf))

    # S02: se rechaza el intento.
    rechazada = await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/reject",
        json={"note": "El vidriado no cuajó"},
        headers=head(admin_csrf),
    )
    assert rechazada.status_code == 200, rechazada.text

    # S03: nace la sucesora.
    sucesora = await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/successor",
        json={"notes": "Segunda vuelta"},
        headers=head(admin_csrf),
    )
    assert sucesora.status_code == 201, sucesora.text
    hija = sucesora.json()

    db_session.expire_all()
    padre = await db_session.get(Prototype, datos["muestra_id"])
    nueva = await db_session.get(Prototype, hija["id"])
    assert padre is not None and nueva is not None

    # S04: la misma cotización, no una copia ni una nueva.
    assert nueva.prototype_quotation_id == padre.prototype_quotation_id
    assert nueva.prototype_quotation_id == datos["cpr_id"]
    assert nueva.supersedes_prototype_id == padre.id
    assert await db_session.scalar(select(func.count()).select_from(PrototypeQuotation)) == 1, (
        "no se fabrica una segunda cotización de prototipo"
    )

    # S05 + S06: su orden es NUEVA y sabe decir de qué cobro viene.
    creada = await api.post(
        ORDENES,
        json={"prototype_id": hija["id"], "stock_location_id": datos["almacen"]},
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    orden_nueva = creada.json()
    assert orden_nueva["prototype_id"] == hija["id"]
    assert orden_nueva["prototype_quotation_id"] == datos["cpr_id"]
    assert orden_nueva["prototype_quotation_code"] == datos["cpr_code"]

    # S07: la orden anterior no se reutiliza ni se toca.
    assert orden_nueva["id"] != datos["orden_id"]
    anterior = await db_session.get(ProductionOrder, datos["orden_id"])
    assert anterior is not None
    assert anterior.prototype_id == datos["muestra_id"]
    assert anterior.status is ProductionOrderStatus.COMPLETED


@pytest.mark.asyncio
async def test_una_sucesora_de_muestra_sin_cotizacion_sigue_sin_cotizacion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """S08. SUCCESSOR_WITH_NULL_PARENT_CPR: NULL.

    Se hereda lo que hay. Inventarle una cotización a una muestra que nació sin
    ninguna sería atribuirle un cobro que nadie hizo.
    """
    from tests.db.test_prototypes import _muestra_lista as _muestra_por_el_camino_antiguo

    datos = await _muestra_por_el_camino_antiguo(api, admin_csrf, db_session, suffix="_add_sin_cpr")
    muestra_id = datos["prototipo"]["id"]
    await api.post(f"{PROTOTIPOS}/{muestra_id}/start", headers=head(admin_csrf))
    await api.post(f"{PROTOTIPOS}/{muestra_id}/complete", headers=head(admin_csrf))
    await api.post(
        f"{PROTOTIPOS}/{muestra_id}/reject", json={"note": "No sirve"}, headers=head(admin_csrf)
    )

    sucesora = await api.post(
        f"{PROTOTIPOS}/{muestra_id}/successor", json={"notes": "Otra"}, headers=head(admin_csrf)
    )
    assert sucesora.status_code == 201, sucesora.text

    db_session.expire_all()
    padre = await db_session.get(Prototype, muestra_id)
    hija = await db_session.get(Prototype, sucesora.json()["id"])
    assert padre is not None and hija is not None
    assert padre.prototype_quotation_id is None
    assert hija.prototype_quotation_id is None


@pytest.mark.asyncio
async def test_cobrar_dos_veces_con_sucesora_sigue_devolviendo_la_primera(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La idempotencia del cobro sobrevive a la herencia.

    Heredar `prototype_quotation_id` hace que una cotización pueda tener varias
    muestras colgando, y el cobro las busca por ahí. Sin un orden explícito, un
    `LIMIT 1` podía devolver la sucesora y el segundo cobro habría contestado
    con la orden equivocada. La raíz de la cadena es la que este cobro
    materializó, y es la que se devuelve siempre.
    """
    datos = await _cobrada(api, admin_csrf, db_session, "_add_idem")
    await dar_existencia(
        api,
        admin_csrf,
        product_id=datos["pasta"]["id"],
        location_id=datos["almacen"],
        cantidad="10000",
    )
    await api.post(f"{ORDENES}/{datos['orden_id']}/start", headers=head(admin_csrf))
    await api.post(f"{ORDENES}/{datos['orden_id']}/complete", headers=head(admin_csrf))
    await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/reject",
        json={"note": "No"},
        headers=head(admin_csrf),
    )
    sucesora = await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/successor", json={}, headers=head(admin_csrf)
    )
    assert sucesora.status_code == 201, sucesora.text

    db_session.expire_all()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prototype)
            .where(Prototype.prototype_quotation_id == datos["cpr_id"])
        )
        == 2
    ), "el escenario que hace ambigua la búsqueda"

    repetido = await cobrar(api, admin_csrf, datos["cpr_id"], stock_location_id=datos["almacen"])
    assert repetido.status_code == 200, repetido.text
    assert repetido.json()["prototype_id"] == datos["muestra_id"]
    assert repetido.json()["production_order_id"] == datos["orden_id"]
    assert repetido.json()["product_id"] == datos["product_id"]

    # Y la lectura de la cotización dice lo mismo que el cobro.
    documento = await api.get(f"{COTIZADOR}/{datos['cpr_id']}", headers=head(admin_csrf))
    assert documento.json()["prototype_id"] == datos["muestra_id"]
    assert documento.json()["production_order_id"] == datos["orden_id"]


@pytest.mark.asyncio
async def test_la_sucesora_arranca_con_el_guardia_de_cobro_de_su_cotizacion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Heredar la cotización devuelve una comprobación que antes se saltaba.

    Hasta el addendum, la sucesora no tenía cotización de prototipo y su
    evaluación de disponibilidad no comprobaba cobro alguno. Ahora la hereda, y
    como está pagada, la sucesora arranca: el guardia existe y deja pasar por
    la razón correcta.
    """
    datos = await _cobrada(api, admin_csrf, db_session, "_add_guardia")
    await dar_existencia(
        api,
        admin_csrf,
        product_id=datos["pasta"]["id"],
        location_id=datos["almacen"],
        cantidad="10000",
    )
    await api.post(f"{ORDENES}/{datos['orden_id']}/start", headers=head(admin_csrf))
    await api.post(f"{ORDENES}/{datos['orden_id']}/complete", headers=head(admin_csrf))
    await api.post(
        f"{PROTOTIPOS}/{datos['muestra_id']}/reject",
        json={"note": "No"},
        headers=head(admin_csrf),
    )
    hija = (
        await api.post(
            f"{PROTOTIPOS}/{datos['muestra_id']}/successor", json={}, headers=head(admin_csrf)
        )
    ).json()

    creada = await api.post(
        ORDENES,
        json={"prototype_id": hija["id"], "stock_location_id": datos["almacen"]},
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    orden_hija = creada.json()["id"]

    arrancada = await api.post(f"{ORDENES}/{orden_hija}/start", headers=head(admin_csrf))
    assert arrancada.status_code == 200, arrancada.text

    db_session.expire_all()
    lineas = await _lineas(db_session, hija["id"])
    assert lineas and all(linea.quantity_actual is not None for linea in lineas)
    # Y el material heredado conserva el rol que declaró la cotización.
    assert lineas[0].material_role is not None
    assert lineas[0].material_role.value == "BODY"
    assert lineas[0].quantity_planned == Decimal("1.25")
