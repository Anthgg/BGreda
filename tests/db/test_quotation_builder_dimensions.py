"""Fase 009B — dimensiones personalizadas por linea de cotizacion.

Cubre lo que test_quotation_builder_api.py ya prueba de forma incidental
(CASO A/CASO C basico, maestro no mutado) pero de forma aislada: cada test
aqui abre su propio borrador y no encadena varios PUT/confirm sobre el mismo
recurso, para no acoplarse a la coreografia de QUOTATION_BUILDER_SOURCE_CHANGED
que ya cubre el test original.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.masters import Product
from tests.db.test_firings_api import FACTORES_CHICO, crear_horno
from tests.db.test_quotation_builder_api import _complete_payload, _customer, head
from tests.db.test_quotations_api import _finished_product_and_recipe

BUILDER = "/api/v1/quotation-builder"


async def _draft_setup(
    api: httpx.AsyncClient,
    csrf: str,
    db_session: AsyncSession,
    *,
    suffix: str,
    master_dimensions: dict[str, str] | None = None,
    customer: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Cliente + producto (con o sin dimensiones en el maestro) + horno.

    `customer` es opcional: _customer() siempre usa el mismo RUC fijo, asi
    que un test que necesite dos productos (mismo cliente, dos lineas) debe
    reutilizar el cliente ya creado en la primera llamada en vez de invocar
    _draft_setup dos veces sin mas — la segunda pisaria con un 409
    MASTER_VALUE_EXISTS al intentar crear el mismo RUC otra vez.
    """
    customer = customer or await _customer(api, csrf)
    product, recipe = await _finished_product_and_recipe(api, csrf, suffix)
    if master_dimensions:
        await db_session.execute(
            update(Product)
            .where(Product.id == product["id"])
            .values(**{field: Decimal(value) for field, value in master_dimensions.items()})
        )
        await db_session.commit()
    kiln = await crear_horno(
        api,
        csrf,
        db_session,
        nombre=f"Horno 009B{suffix}",
        capacidad="10000000",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )
    return customer, product, {"recipe": recipe, "kiln": kiln}


def _base_item(product: dict[str, Any], recipe: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": product["id"],
        "quantity": 10,
        "recipe_id": recipe["id"],
        "recipe_version_id": recipe["current_version"]["id"],
        "material_grams_per_piece": "10",
        "markup_percent": "100",
        "commercial_sale_unit_price": "20",
    }


@pytest.mark.asyncio
async def test_caso_a_standard_dimensions_effective_equals_master(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """1. STANDARD_DIMENSIONS: sin override, effective == master."""
    customer, product, ctx = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_a",
        master_dimensions={"width": "12", "height": "18", "length": "12"},
    )
    payload = {
        "name": "009B Caso A",
        "customer_id": customer["id"],
        "kiln_id": ctx["kiln"]["id"],
        "items": [_base_item(product, ctx["recipe"])],
    }
    response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert response.status_code == 201, response.text
    draft = response.json()
    item = draft["items"][0]
    assert Decimal(item["width"]) == Decimal("12")
    assert Decimal(item["height"]) == Decimal("18")
    assert Decimal(item["length"]) == Decimal("12")
    assert item["dimensions_overridden"] is False


@pytest.mark.asyncio
async def test_caso_b_custom_dimensions_master_unchanged(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """2 + 5. CUSTOM_DIMENSIONS y PRODUCT_MASTER_MUTATED: NO."""
    customer, product, ctx = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_b",
        master_dimensions={"width": "12", "height": "18", "length": "12"},
    )
    item = _base_item(product, ctx["recipe"])
    item["dimensions"] = {"width": "15", "height": "22", "length": "15"}
    item["dimensions_overridden"] = True
    payload = {
        "name": "009B Caso B",
        "customer_id": customer["id"],
        "kiln_id": ctx["kiln"]["id"],
        "items": [item],
    }
    response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert response.status_code == 201, response.text
    draft = response.json()
    saved_item = draft["items"][0]
    assert Decimal(saved_item["width"]) == Decimal("15")
    assert Decimal(saved_item["height"]) == Decimal("22")
    assert Decimal(saved_item["length"]) == Decimal("15")
    assert saved_item["dimensions_overridden"] is True

    master = await api.get(f"/api/v1/products/{product['id']}")
    assert master.status_code == 200
    assert master.json()["width"] == "12.000000"
    assert master.json()["height"] == "18.000000"
    assert master.json()["length"] == "12.000000"


@pytest.mark.asyncio
async def test_caso_c_missing_master_dimensions_completed_without_mutating_master(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """3. missing dimensions: bloquea hasta completarse; 5. maestro intacto."""
    customer, product, ctx = await _draft_setup(api, admin_csrf, db_session, suffix="_009b_c")
    incomplete_item = _base_item(product, ctx["recipe"])
    payload = {
        "name": "009B Caso C incompleto",
        "customer_id": customer["id"],
        "kiln_id": ctx["kiln"]["id"],
        "items": [incomplete_item],
    }
    preview = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview.status_code == 200, preview.text
    assert "PRODUCTION_DIMENSIONS_REQUIRED" in preview.json()["items"][0]["warnings"]
    assert preview.json()["complete"] is False

    complete_item = dict(incomplete_item)
    complete_item["dimensions"] = {"width": "10", "height": "15", "length": "10"}
    payload["items"] = [complete_item]
    response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["complete"] is True
    assert Decimal(draft["items"][0]["width"]) == Decimal("10")

    master = await api.get(f"/api/v1/products/{product['id']}")
    assert master.status_code == 200
    assert master.json()["width"] is None
    assert master.json()["height"] is None
    assert master.json()["length"] is None


@pytest.mark.asyncio
async def test_dimension_must_be_positive(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """4. dimension <= 0 rechazada por el schema (422), nunca aceptada."""
    customer, product, ctx = await _draft_setup(api, admin_csrf, db_session, suffix="_009b_neg")
    item = _base_item(product, ctx["recipe"])
    item["dimensions"] = {"width": "0", "height": "10", "length": "10"}
    payload = {
        "name": "009B dimension invalida",
        "customer_id": customer["id"],
        "kiln_id": ctx["kiln"]["id"],
        "items": [item],
    }
    response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert response.status_code == 422, response.text

    item["dimensions"]["width"] = "-5"
    response_negative = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert response_negative.status_code == 422, response_negative.text


@pytest.mark.asyncio
async def test_draft_reopen_preserves_custom_dimensions(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """6. BORRADOR: Guardar, cerrar, reabrir -> custom dims y flag preservados."""
    customer, product, ctx = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_reopen",
        master_dimensions={"width": "12", "height": "18", "length": "12"},
    )
    item = _base_item(product, ctx["recipe"])
    item["dimensions"] = {"width": "15", "height": "22", "length": "15"}
    item["dimensions_overridden"] = True
    payload = {
        "name": "009B Reopen",
        "customer_id": customer["id"],
        "kiln_id": ctx["kiln"]["id"],
        "items": [item],
    }
    created = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]

    reopened = await api.get(f"{BUILDER}/{draft_id}")
    assert reopened.status_code == 200
    reopened_item = reopened.json()["items"][0]
    assert Decimal(reopened_item["width"]) == Decimal("15")
    assert Decimal(reopened_item["height"]) == Decimal("22")
    assert reopened_item["dimensions_overridden"] is True


@pytest.mark.asyncio
async def test_multiproduct_mixed_standard_and_custom_dimensions_per_line(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """8. MULTIPRODUCTO: cada linea mantiene su propio estado, no un flag global."""
    customer, product_a, ctx_a = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_mix_a",
        master_dimensions={"width": "12", "height": "18", "length": "12"},
    )
    _, product_b, ctx_b = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_mix_b",
        master_dimensions={"width": "20", "height": "25", "length": "20"},
        customer=customer,
    )
    item_a = _base_item(product_a, ctx_a["recipe"])  # standard
    item_b = _base_item(product_b, ctx_b["recipe"])
    item_b["dimensions"] = {"width": "30", "height": "35", "length": "30"}
    item_b["dimensions_overridden"] = True
    payload = {
        "name": "009B Multiproducto mixto",
        "customer_id": customer["id"],
        "kiln_id": ctx_a["kiln"]["id"],
        "items": [item_a, item_b],
    }
    response = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert response.status_code == 201, response.text
    items_by_product = {item["product_id"]: item for item in response.json()["items"]}

    a_out = items_by_product[product_a["id"]]
    assert a_out["dimensions_overridden"] is False
    assert Decimal(a_out["width"]) == Decimal("12")

    b_out = items_by_product[product_b["id"]]
    assert b_out["dimensions_overridden"] is True
    assert Decimal(b_out["width"]) == Decimal("30")

    master_a = (await api.get(f"/api/v1/products/{product_a['id']}")).json()
    assert master_a["width"] == "12.000000"
    master_b = (await api.get(f"/api/v1/products/{product_b['id']}")).json()
    assert master_b["width"] == "20.000000"


@pytest.mark.asyncio
async def test_duplicate_quotation_copies_effective_dimensions_and_override_state(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """9. DUPLICAR: copia medidas efectivas y estado de override; queda editable."""
    customer, product, ctx = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_dup",
        master_dimensions={"width": "12", "height": "18", "length": "12"},
    )
    item = _base_item(product, ctx["recipe"])
    item["dimensions"] = {"width": "15", "height": "22", "length": "15"}
    item["dimensions_overridden"] = True
    payload = {
        "name": "009B Original",
        "customer_id": customer["id"],
        "kiln_id": ctx["kiln"]["id"],
        "items": [item],
    }
    created = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text
    original_id = created.json()["id"]

    duplicated = await api.post(f"{BUILDER}/{original_id}/duplicate", headers=head(admin_csrf))
    assert duplicated.status_code == 200, duplicated.text
    duplicate = duplicated.json()
    assert duplicate["id"] != original_id
    assert duplicate["status"] == "DRAFT"
    dup_item = duplicate["items"][0]
    assert Decimal(dup_item["width"]) == Decimal("15")
    assert Decimal(dup_item["height"]) == Decimal("22")
    assert dup_item["dimensions_overridden"] is True

    # La copia es independiente y editable: modificarla no toca la original.
    edited = dict(payload)
    edited["items"] = [dict(item)]
    edited["items"][0]["dimensions"] = {"width": "16", "height": "22", "length": "15"}
    edited["expected_updated_at"] = duplicate["updated_at"]
    edit_response = await api.put(
        f"{BUILDER}/{duplicate['id']}", json=edited, headers=head(admin_csrf)
    )
    assert edit_response.status_code == 200, edit_response.text

    original_reloaded = await api.get(f"{BUILDER}/{original_id}")
    assert Decimal(original_reloaded.json()["items"][0]["width"]) == Decimal("15")


@pytest.mark.asyncio
async def test_confirmed_dimensions_immutable_when_master_changes_later(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """7. CONFIRMED_QUOTE: snapshot congelado incluso si el maestro cambia despues."""
    customer, product, ctx = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_confirm",
        master_dimensions={"width": "12", "height": "18", "length": "12"},
    )
    item = _base_item(product, ctx["recipe"])
    item["dimensions"] = {"width": "15", "height": "22", "length": "15"}
    item["dimensions_overridden"] = True
    payload = {
        "name": "009B Confirmar con override",
        "customer_id": customer["id"],
        "kiln_id": ctx["kiln"]["id"],
        "items": [item],
    }
    created = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["complete"] is True

    confirm_response = await api.post(
        f"{BUILDER}/{draft['id']}/confirm",
        json={"expected_updated_at": draft["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirm_response.status_code == 200, confirm_response.text
    confirmed = confirm_response.json()
    assert confirmed["status"] == "CONFIRMED"
    assert Decimal(confirmed["items"][0]["width"]) == Decimal("15")

    # El maestro cambia legitimamente seis meses despues...
    await db_session.execute(
        update(Product)
        .where(Product.id == product["id"])
        .values(width=Decimal("12"), height=Decimal("20"), length=Decimal("12"))
    )
    await db_session.commit()

    reloaded = await api.get(f"{BUILDER}/{draft['id']}")
    assert reloaded.status_code == 200
    reloaded_item = reloaded.json()["items"][0]
    assert Decimal(reloaded_item["width"]) == Decimal("15")
    assert Decimal(reloaded_item["height"]) == Decimal("22")

    # Intentar editar una cotizacion confirmada sigue bloqueado (regla ya
    # existente, no propia de 009B, pero es la garantia de inmutabilidad).
    edit_attempt = dict(payload)
    edit_attempt["expected_updated_at"] = confirmed["updated_at"]
    edit_response = await api.put(
        f"{BUILDER}/{draft['id']}", json=edit_attempt, headers=head(admin_csrf)
    )
    assert edit_response.status_code == 409


@pytest.mark.asyncio
async def test_production_capacity_uses_effective_not_master_dimensions(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """10. Calculo (volumen/ocupacion de horno) usa EFFECTIVE_DIMENSIONS."""
    customer, product, ctx = await _draft_setup(
        api,
        admin_csrf,
        db_session,
        suffix="_009b_capacity",
        master_dimensions={"width": "1", "height": "1", "length": "1"},
    )
    # Un horno diminuto: el maestro (1x1x1 = 1 cm3) entra en una sola hornada,
    # pero una medida personalizada mucho mas grande necesita varias.
    tiny_kiln = await crear_horno(
        api,
        admin_csrf,
        db_session,
        nombre="Horno diminuto 009B",
        capacidad="5",
        baja="120",
        alta="180",
        factores=FACTORES_CHICO,
    )
    item = _base_item(product, ctx["recipe"])
    item["quantity"] = 1
    item["dimensions"] = {"width": "50", "height": "50", "length": "50"}
    item["dimensions_overridden"] = True
    payload = {
        "name": "009B Capacidad efectiva",
        "customer_id": customer["id"],
        "kiln_id": tiny_kiln["id"],
        "items": [item],
    }
    preview = await api.post(f"{BUILDER}/preview", json=payload, headers=head(admin_csrf))
    assert preview.status_code == 200, preview.text
    body = preview.json()
    # Desde Fase 009C exceder la capacidad ya no es una alerta: se resuelve
    # con mas hornadas. Que la medida EFECTIVA (50x50x50 = 125 000 cm3) manda
    # sobre la del maestro (1x1x1 = 1 cm3) se comprueba igual de bien —y mas
    # directamente— con el numero de hornadas que hacen falta en un horno de
    # 5 cm3: con la medida del maestro seria 1.
    assert "KILN_CAPACITY_EXCEEDED" not in body["items"][0]["warnings"]
    assert body["production_summary"]["capacity_exceeded"] is False
    assert body["production_summary"]["total_batches"] > 1


# ---------------------------------------------------------------------------
# Fase 009B — CASO C: producto sin medidas en el maestro
# ---------------------------------------------------------------------------
#
# Un producto cuyo maestro no tiene medidas puede recibirlas en la cotización.
# Esas medidas son de ESA línea, no del maestro, y tienen que sobrevivir a
# guardar y reabrir.
#
# La regresión que estas pruebas impiden: `editable_dimensions` se calculaba al
# reabrir mirando el SNAPSHOT en vez del maestro. Una medida propia guardada
# parecía entonces venir del maestro, la interfaz la protegía, dejaba de
# enviarla, y el recálculo caía al maestro —que no la tiene— y volvía a pedir
# las medidas. El borrador se completaba en pantalla y se rompía al reabrirlo.


async def _producto_sin_medidas(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, payload: dict[str, Any]
) -> int:
    """Deja el segundo producto del fixture sin medidas en el maestro."""
    product_id = payload["items"][1]["product_id"]
    await db_session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(width=None, height=None, length=None, depth=None)
    )
    await db_session.commit()
    return int(product_id)


def _con_medidas_propias(payload: dict[str, Any]) -> dict[str, Any]:
    """Da al segundo producto medidas que sólo existen en esta cotización."""
    items = [dict(item) for item in payload["items"]]
    items[1] = {**items[1], "dimensions": {"width": "6", "height": "8", "length": "6"}}
    return {**payload, "items": items}


@pytest.mark.asyncio
async def test_caso_c_las_medidas_propias_sobreviven_a_guardar_y_reabrir(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """QUOTE_CUSTOM_DIMENSIONS_SAVE_REOPEN y DRAFT_COMPLETENESS_PRESERVED."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    product_id = await _producto_sin_medidas(api, admin_csrf, db_session, payload)

    creada = await api.post(BUILDER, json=_con_medidas_propias(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]

    # ---- Se guardaron en la linea, no en el maestro --------------------
    fila = (
        (
            await db_session.execute(
                text(
                    "SELECT product_width_snapshot, product_height_snapshot, "
                    "product_length_snapshot FROM quotation_items "
                    "WHERE quotation_id = :qid AND product_id = :pid"
                ),
                {"qid": quotation_id, "pid": product_id},
            )
        )
        .mappings()
        .one()
    )
    assert Decimal(str(fila["product_width_snapshot"])) == Decimal(6)
    assert Decimal(str(fila["product_height_snapshot"])) == Decimal(8)
    assert Decimal(str(fila["product_length_snapshot"])) == Decimal(6)

    # ---- Al reabrir vuelven, y la linea sigue completa -----------------
    reabierta = await api.get(f"{BUILDER}/{quotation_id}")
    assert reabierta.status_code == 200
    data = reabierta.json()
    linea = next(item for item in data["items"] if item["product_id"] == product_id)
    assert Decimal(linea["width"]) == Decimal(6)
    assert Decimal(linea["height"]) == Decimal(8)
    assert Decimal(linea["length"]) == Decimal(6)
    assert "PRODUCTION_DIMENSIONS_REQUIRED" not in linea["warnings"]

    # ---- Y siguen siendo editables, porque el maestro no las tiene -----
    #
    # Esta es la asercion que habria impedido la regresion: si se marcan como
    # protegidas, la interfaz deja de enviarlas y el siguiente recalculo las
    # pierde.
    for campo in ("width", "height", "length"):
        assert campo in linea["editable_dimensions"], (
            f"{campo} debe seguir editable: el maestro no la tiene"
        )


@pytest.mark.asyncio
async def test_caso_c_el_maestro_no_se_toca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MASTER_DIMENSIONS_UNCHANGED.

    Antes de 009B, guardar escribía las medidas en el maestro compartido: un
    producto medido para una cotización cambiaba de tamaño para todas.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    product_id = await _producto_sin_medidas(api, admin_csrf, db_session, payload)

    creada = await api.post(BUILDER, json=_con_medidas_propias(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    maestro = (
        (
            await db_session.execute(
                text("SELECT width, height, length, depth FROM products WHERE id = :pid"),
                {"pid": product_id},
            )
        )
        .mappings()
        .one()
    )
    assert all(valor is None for valor in maestro.values()), (
        "las medidas de una cotizacion no pueden acabar en el maestro"
    )


@pytest.mark.asyncio
async def test_caso_c_recalcular_tras_reabrir_no_pierde_las_medidas(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El bucle exacto que rompía el borrador.

    Reabrir devuelve la línea; volver a guardarla con lo que devuelve tiene que
    dejarla igual. Si al reabrir las medidas dejan de marcarse como editables,
    este guardado las manda vacías y la línea se queda sin medidas.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    product_id = await _producto_sin_medidas(api, admin_csrf, db_session, payload)

    creada = await api.post(BUILDER, json=_con_medidas_propias(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    guardada = creada.json()

    # Se reenvia SOLO lo que el backend declara editable, que es justo lo que
    # hace la interfaz.
    items = []
    for item in guardada["items"]:
        editables = item["editable_dimensions"]
        dimensiones = {campo: item[campo] for campo in editables if item.get(campo) is not None}
        items.append(
            {
                "product_id": item["product_id"],
                "quantity": item["quantity"],
                "dimensions": dimensiones,
                "sort_order": item["sort_order"],
            }
        )

    reenviada = await api.put(
        f"{BUILDER}/{guardada['id']}",
        json={
            "name": guardada["name"],
            "customer_id": guardada["customer_id"],
            "items": items,
            "expected_updated_at": guardada["updated_at"],
        },
        headers=head(admin_csrf),
    )
    assert reenviada.status_code == 200, reenviada.text

    linea = next(item for item in reenviada.json()["items"] if item["product_id"] == product_id)
    assert linea["width"] is not None, "el reenvio no puede vaciar las medidas"
    assert Decimal(linea["width"]) == Decimal(6)
    assert Decimal(linea["height"]) == Decimal(8)
    assert Decimal(linea["length"]) == Decimal(6)
