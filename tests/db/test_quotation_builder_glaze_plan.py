"""Fase 009D — el plan de esmaltes sobrevive guardar, reabrir y confirmar.

Todo esto vive en `QuotationItem.production_snapshot["glaze_plan"]`. No hay
tabla nueva ni migracion: el snapshot ya era el sitio donde la linea guarda la
intencion del usuario (`low_kiln_selected`, `dimensions_overridden`) y el plan
congelado (`firing_plan`), y un plan de esmaltes es exactamente eso.

Lo que se comprueba aqui no es la aritmetica —esa esta en
tests/unit/test_preparations_math.py— sino las reglas de ciclo de vida: que la
ELECCION del usuario se conserve literal, que los DERIVADOS se recalculen
mientras es borrador, que al confirmar queden congelados, y que nada de esto
mueva un gramo de almacen.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from app.models.masters import Product
from tests.db.test_glaze_estimate_api import _lote
from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head

COMMERCIAL = "/api/v1/settings/commercial"

#: Peso de la pieza que se fija en el maestro. Con el 15 % por defecto da
#: 75 g por pieza; con 100 unidades, 7500 g de esmalte para el lote.
PIECE_WEIGHT_G = Decimal(500)


async def _scenario(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Cotización completa de dos productos + dos lotes de esmalte reales."""
    payload, products = await _complete_payload(api, csrf, db_session)
    await db_session.execute(
        update(Product)
        .where(Product.id.in_([product["id"] for product in products]))
        .values(grammage=PIECE_WEIGHT_G)
    )
    await db_session.commit()

    # 1000 g secos en 5000 ml -> 0,2 g/ml y 0,05 por ml.
    uno = await _lote(api, csrf, suffix="_plan1", final_yield_ml="5000")
    # Mismo peso seco, el doble de rendimiento -> 0,1 g/ml. Que los dos lotes
    # conviertan distinto es lo que prueba que el factor es del lote.
    dos = await _lote(api, csrf, suffix="_plan2", final_yield_ml="10000")
    return payload, uno, dos


def _with_glazes(payload: dict[str, Any], glazes: list[dict[str, Any]], unit: str = "g") -> dict:
    """Añade el plan de esmaltes a la PRIMERA línea de la cotización."""
    items = [dict(item) for item in payload["items"]]
    items[0] = {**items[0], "glazes": glazes, "glaze_unit": unit}
    return {**payload, "items": items}


async def _inventory_fingerprint(session: AsyncSession) -> tuple[int, list[tuple[int, int, str]]]:
    session.expire_all()
    movements = await session.scalar(select(func.count()).select_from(StockMovement))
    balances = (
        await session.execute(
            select(
                StockBalance.product_id,
                StockBalance.location_id,
                StockBalance.quantity,
            ).order_by(StockBalance.product_id, StockBalance.location_id)
        )
    ).all()
    return int(movements or 0), [(p, loc, str(q)) for p, loc, q in balances]


# ---------------------------------------------------------------------------
# Persistencia y reapertura
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_la_seleccion_de_esmaltes_se_guarda_y_se_reabre(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """GLAZE_SELECTION_PERSISTED + DRAFT_SAVE_REOPEN + G_ML_UNIT_REOPEN."""
    payload, uno, dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [
            {"preparation_id": uno["id"], "share": "70"},
            {"preparation_id": dos["id"], "share": "30"},
        ],
        unit="ml",
    )

    creada = await api.post(BUILDER, json=body, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    reabierta = await api.get(f"{BUILDER}/{creada.json()['id']}", headers=head(admin_csrf))
    assert reabierta.status_code == 200, reabierta.text
    linea = reabierta.json()["items"][0]

    assert linea["glaze_unit"] == "ml"
    plan = linea["glaze_plan"]
    assert [a["preparation_id"] for a in plan["allocations"]] == [uno["id"], dos["id"]]
    # SHARE_LITERAL_PRESERVED: lo que tecleó el usuario, tal cual.
    assert [Decimal(a["share"]) for a in plan["allocations"]] == [Decimal(70), Decimal(30)]


@pytest.mark.asyncio
async def test_el_share_no_es_un_porcentaje_y_el_backend_lo_resuelve(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ALLOCATION_PERCENT_RESOLVED.

    Con share 1 y 1 el reparto es mitad y mitad. Si alguien tratase el share
    como porcentaje directo, esto daría 1 % y 1 % y perdería el 98 % restante.
    """
    payload, uno, dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [
            {"preparation_id": uno["id"], "share": "1"},
            {"preparation_id": dos["id"], "share": "1"},
        ],
    )

    respuesta = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    plan = respuesta.json()["items"][0]["glaze_plan"]

    assert [Decimal(a["share"]) for a in plan["allocations"]] == [Decimal(1), Decimal(1)]
    assert [Decimal(a["allocation_percent"]) for a in plan["allocations"]] == [
        Decimal(50),
        Decimal(50),
    ]


@pytest.mark.asyncio
async def test_el_total_se_reparte_y_reconcilia_exactamente(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MULTI_GLAZE_ALLOCATION_PERSISTED + TOTAL_ALLOCATION_RECONCILES.

    500 g x 15 % x 100 piezas = 7500 g en total, repartidos 70/30. Dos
    esmaltes no gastan el doble: gastan lo mismo, en dos baldes.
    """
    payload, uno, dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [
            {"preparation_id": uno["id"], "share": "70"},
            {"preparation_id": dos["id"], "share": "30"},
        ],
    )

    respuesta = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    plan = respuesta.json()["items"][0]["glaze_plan"]

    assert Decimal(plan["estimated_glaze_percent_snapshot"]) == Decimal(15)
    assert Decimal(plan["grams_per_piece"]) == Decimal(75)
    assert Decimal(plan["total_estimated_solids_g"]) == Decimal(7500)

    gramos = [Decimal(a["grams"]) for a in plan["allocations"]]
    assert gramos == [Decimal(5250), Decimal(2250)]
    # Ni un gramo perdido por redondeo.
    assert sum(gramos) == Decimal(plan["total_estimated_solids_g"])


@pytest.mark.asyncio
async def test_cada_lote_convierte_con_su_propia_concentracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """No se asume densidad 1, y el factor es del lote, no de la unidad."""
    payload, uno, dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [
            {"preparation_id": uno["id"], "share": "1"},
            {"preparation_id": dos["id"], "share": "1"},
        ],
    )

    respuesta = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    a, b = respuesta.json()["items"][0]["glaze_plan"]["allocations"]

    # Los mismos 3750 g: a 0,2 g/ml son 18750 ml; a 0,1 g/ml son 37500.
    assert Decimal(a["grams"]) == Decimal(b["grams"]) == Decimal(3750)
    assert Decimal(a["solids_g_per_ml_snapshot"]) == Decimal("0.2")
    assert Decimal(b["solids_g_per_ml_snapshot"]) == Decimal("0.1")
    assert Decimal(a["millilitres"]) == Decimal(18750)
    assert Decimal(b["millilitres"]) == Decimal(37500)


@pytest.mark.asyncio
async def test_pedir_mililitros_sin_lote_avisa_en_vez_de_inventar(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ML_REQUIRES_CONCENTRATION.

    Sin lote no hay concentración, y sin concentración no hay conversión. El
    borrador sigue abriéndose —se muestra en gramos— pero avisa, y el aviso
    impide completar.
    """
    payload, uno, _dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [{"prepared_product_id": uno["prepared_product_id"], "share": "1"}],
        unit="ml",
    )

    respuesta = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    linea = respuesta.json()["items"][0]

    assert "GLAZE_ML_REQUIRES_PREPARATION" in linea["warnings"]
    assert linea["glaze_plan"]["unit"] == "g"
    assert linea["glaze_plan"]["allocations"][0]["millilitres"] is None
    assert not respuesta.json()["complete"]


@pytest.mark.asyncio
async def test_un_reparto_en_cero_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """INVALID_SHARE: un cero no reparte nada y falsearía el total."""
    payload, uno, _dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(payload, [{"preparation_id": uno["id"], "share": "0"}])

    respuesta = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))
    assert respuesta.status_code == 422, respuesta.text


@pytest.mark.asyncio
async def test_sin_gramaje_en_el_maestro_no_se_estima(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Inventar un peso de pieza daría un costo de esmalte creíble y falso."""
    payload, uno, _dos = await _scenario(api, admin_csrf, db_session)
    await db_session.execute(
        update(Product).where(Product.id == payload["items"][0]["product_id"]).values(grammage=None)
    )
    await db_session.commit()

    body = _with_glazes(payload, [{"preparation_id": uno["id"], "share": "1"}])
    respuesta = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))

    assert respuesta.status_code == 200, respuesta.text
    linea = respuesta.json()["items"][0]
    assert "GLAZE_PIECE_WEIGHT_REQUIRED" in linea["warnings"]
    assert linea["glaze_plan"] is None


# ---------------------------------------------------------------------------
# Borrador vs confirmada frente a un cambio de configuracion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cambiar_el_porcentaje_recalcula_el_borrador_sin_perder_la_eleccion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DRAFT_CONFIG_CHANGE_RECALCULATES."""
    payload, uno, dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [
            {"preparation_id": uno["id"], "share": "70"},
            {"preparation_id": dos["id"], "share": "30"},
        ],
    )
    creada = await api.post(BUILDER, json=body, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]
    assert Decimal(creada.json()["items"][0]["glaze_plan"]["total_estimated_solids_g"]) == Decimal(
        7500
    )

    cambio = await api.put(
        COMMERCIAL,
        json={"version": 1, "estimated_glaze_percent": "20"},
        headers=head(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    reabierta = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    recalculada = await api.put(
        f"{BUILDER}/{quotation_id}",
        json={**body, "expected_updated_at": reabierta.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert recalculada.status_code == 200, recalculada.text
    plan = recalculada.json()["items"][0]["glaze_plan"]

    # Los DERIVADOS siguen la configuración vigente: 500 x 20 % x 100 = 10000.
    assert Decimal(plan["estimated_glaze_percent_snapshot"]) == Decimal(20)
    assert Decimal(plan["total_estimated_solids_g"]) == Decimal(10000)
    # Y las SELECCIONES del usuario no se han tocado.
    assert [a["preparation_id"] for a in plan["allocations"]] == [uno["id"], dos["id"]]
    assert [Decimal(a["share"]) for a in plan["allocations"]] == [Decimal(70), Decimal(30)]


@pytest.mark.asyncio
async def test_cambiar_el_porcentaje_no_bloquea_confirmar_con_source_changed(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DRAFT_CONFIG_CHANGE_NO_SOURCE_CHANGED.

    Mientras el esmalte sea una estimación técnica y no autoridad de precio,
    tocar su porcentaje no puede invalidar un borrador correcto. Se reevalúa
    en 009E/009H, cuando participe del costo o del consumo.
    """
    payload, uno, _dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(payload, [{"preparation_id": uno["id"], "share": "1"}])
    creada = await api.post(BUILDER, json=body, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]

    cambio = await api.put(
        COMMERCIAL,
        json={"version": 1, "estimated_glaze_percent": "20"},
        headers=head(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    reabierta = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    confirmada = await api.post(
        f"{BUILDER}/{quotation_id}/confirm",
        json={"expected_updated_at": reabierta.json()["updated_at"]},
        headers=head(admin_csrf),
    )

    assert confirmada.status_code == 200, confirmada.text
    assert confirmada.json()["status"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_una_cotizacion_confirmada_congela_su_plan(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_GLAZE_SNAPSHOT_IMMUTABLE.

    Se confirma con el 15 %, se cambia la configuración al 20 %, y la
    cotización sigue diciendo exactamente lo que decía. Una cotización
    confirmada es un compromiso; recalcularla al leerla la haría mentir.
    """
    payload, uno, dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [
            {"preparation_id": uno["id"], "share": "70"},
            {"preparation_id": dos["id"], "share": "30"},
        ],
        unit="ml",
    )
    creada = await api.post(BUILDER, json=body, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]

    reabierta = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    confirmada = await api.post(
        f"{BUILDER}/{quotation_id}/confirm",
        json={"expected_updated_at": reabierta.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    congelado = confirmada.json()["items"][0]["glaze_plan"]

    cambio = await api.put(
        COMMERCIAL,
        json={"version": 1, "estimated_glaze_percent": "20"},
        headers=head(admin_csrf),
    )
    assert cambio.status_code == 200, cambio.text

    despues = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    assert despues.status_code == 200, despues.text
    plan = despues.json()["items"][0]["glaze_plan"]

    assert Decimal(plan["estimated_glaze_percent_snapshot"]) == Decimal(15)
    assert Decimal(plan["total_estimated_solids_g"]) == Decimal(7500)
    assert plan == congelado
    assert despues.json()["items"][0]["glaze_unit"] == "ml"

    # Y sigue sin poder editarse: la inmutabilidad la impone el estado.
    bloqueada = await api.put(
        f"{BUILDER}/{quotation_id}",
        json={**body, "expected_updated_at": despues.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert bloqueada.status_code == 409, bloqueada.text


@pytest.mark.asyncio
async def test_el_plan_convive_con_el_de_quemas_sin_pisarlo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El snapshot guarda dos planes, no una sopa: ninguno borra al otro."""
    payload, uno, _dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(payload, [{"preparation_id": uno["id"], "share": "1"}])

    respuesta = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    snapshot = respuesta.json()["items"][0]["production_snapshot"]

    assert "glaze_plan" in snapshot
    assert snapshot["firing_plan"], "el plan de quemas debe seguir ahí"
    assert snapshot["calculated_firing_days"] > 0
    assert "dimensions_overridden" in snapshot


# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_planificar_esmaltes_no_mueve_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTATION_DOES_NOT_MUTATE_INVENTORY, ahora con plan de esmaltes."""
    payload, uno, dos = await _scenario(api, admin_csrf, db_session)
    body = _with_glazes(
        payload,
        [
            {"preparation_id": uno["id"], "share": "70"},
            {"preparation_id": dos["id"], "share": "30"},
        ],
        unit="ml",
    )
    antes = await _inventory_fingerprint(db_session)

    preview = await api.post(f"{BUILDER}/preview", json=body, headers=head(admin_csrf))
    assert preview.status_code == 200, preview.text
    assert await _inventory_fingerprint(db_session) == antes

    creada = await api.post(BUILDER, json=body, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]
    assert await _inventory_fingerprint(db_session) == antes

    reabierta = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    assert await _inventory_fingerprint(db_session) == antes

    recalculada = await api.put(
        f"{BUILDER}/{quotation_id}",
        json={**body, "expected_updated_at": reabierta.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert recalculada.status_code == 200, recalculada.text
    assert await _inventory_fingerprint(db_session) == antes

    confirmada = await api.post(
        f"{BUILDER}/{quotation_id}/confirm",
        json={"expected_updated_at": recalculada.json()["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    assert await _inventory_fingerprint(db_session) == antes
