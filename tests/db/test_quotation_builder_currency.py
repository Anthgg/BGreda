"""Fase 009F — moneda de emisión y tipo de cambio, con la cotización entera.

La aritmética de la conversión está en `tests/unit/test_pricing_math.py`. Aquí
se comprueba lo que sólo se ve de punta a punta: que la moneda es intención de
la cotización y no de la configuración, que la tasa viaja con el borrador al
guardar y al duplicar, que confirmar la congela, y que cambiar de moneda
recalcula desde los costos en soles y no desde un precio en dólares ya
redondeado.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head

COMMERCIAL = "/api/v1/settings/commercial"
TASA = "3.75"


def _usd(payload: dict[str, Any], tasa: str = TASA) -> dict[str, Any]:
    return {**payload, "currency_code": "USD", "exchange_rate": tasa}


def _por_margen(payload: dict[str, Any]) -> dict[str, Any]:
    """Quita los precios manuales del fixture compartido.

    `_complete_payload` fija un precio por producto, y un precio manual YA esta
    en moneda de emision: no se convierte. Con el puesto, la tasa no toca el
    resultado y una prueba de conversion no probaria nada. Para ver la
    conversion hace falta el camino del margen.
    """
    return {
        **payload,
        "items": [
            {k: v for k, v in item.items() if k != "commercial_sale_unit_price"}
            for item in payload["items"]
        ],
    }


async def _preview(api: httpx.AsyncClient, csrf: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await api.post(f"{BUILDER}/preview", json=payload, headers=head(csrf))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# La moneda es de la cotización
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_una_cotizacion_en_soles_no_lleva_tipo_de_cambio(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    data = await _preview(api, admin_csrf, payload)

    assert data["currency_code_snapshot"] == "PEN"
    assert data["exchange_rate_snapshot"] is None
    assert data["exchange_rate_source_snapshot"] is None


@pytest.mark.asyncio
async def test_el_neto_en_dolares_es_el_de_soles_dividido_por_la_tasa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """USD_DIVIDES_BY_RATE, con la cotización real y no con una línea suelta."""
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    en_soles = await _preview(api, admin_csrf, payload)
    en_dolares = await _preview(api, admin_csrf, _usd(payload))

    assert en_dolares["currency_code_snapshot"] == "USD"
    assert Decimal(en_dolares["exchange_rate_snapshot"]) == Decimal(TASA)
    assert en_dolares["exchange_rate_source_snapshot"] == "MANUAL"

    for linea_pen, linea_usd in zip(en_soles["items"], en_dolares["items"], strict=True):
        # El costo interno NO se convierte: sigue estando en soles.
        assert linea_usd["technical_cost"] == linea_pen["technical_cost"]
        assert linea_usd["commercial_base_cost"] == linea_pen["commercial_base_cost"]
        # El neto sí, y por división.
        assert Decimal(linea_usd["raw_net_unit_base"]) == Decimal(linea_pen["raw_net_unit"])
        assert Decimal(linea_usd["raw_net_unit"]) == (
            Decimal(linea_pen["raw_net_unit"]) / Decimal(TASA)
        )


@pytest.mark.asyncio
async def test_todas_las_lineas_comparten_la_misma_tasa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """ONE_EXCHANGE_RATE_PER_QUOTATION.

    Dos tasas en un mismo documento darían un total que nadie puede explicar.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    data = await _preview(api, admin_csrf, _usd(payload))

    assert len(data["items"]) > 1
    # Comparado como Decimal: el serializador puede devolver "3.750000".
    assert {Decimal(item["exchange_rate_snapshot"]) for item in data["items"]} == {Decimal(TASA)}
    assert {item["currency_code_snapshot"] for item in data["items"]} == {"USD"}


@pytest.mark.asyncio
async def test_el_total_en_dolares_reconcilia(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """USD_NO_SECOND_TOTAL_ROUNDING y NET + TAX == GROSS, en dólares."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    data = await _preview(api, admin_csrf, _usd(payload))

    suma = sum(Decimal(item["line_total_gross"]) for item in data["items"])
    assert suma == Decimal(data["total_with_tax"])
    assert Decimal(data["commercial_subtotal"]) + Decimal(data["tax_amount"]) == Decimal(
        data["total_with_tax"]
    )
    for item in data["items"]:
        assert Decimal(item["final_net_unit"]) + Decimal(item["final_tax_unit"]) == Decimal(
            item["final_gross_unit"]
        )


# ---------------------------------------------------------------------------
# Validaciones de la puerta de entrada
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("caso", "extra"),
    [
        ("USD_SIN_TASA", {"currency_code": "USD"}),
        ("USD_TASA_CERO", {"currency_code": "USD", "exchange_rate": "0"}),
        ("USD_TASA_NEGATIVA", {"currency_code": "USD", "exchange_rate": "-3.75"}),
        ("PEN_CON_TASA", {"currency_code": "PEN", "exchange_rate": "3.75"}),
        ("MONEDA_NO_ADMITIDA", {"currency_code": "EUR", "exchange_rate": "4.10"}),
    ],
)
@pytest.mark.asyncio
async def test_la_api_rechaza_las_combinaciones_incoherentes(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    caso: str,
    extra: dict[str, Any],
) -> None:
    """422 y no 500: un error de integridad no le dice al usuario qué falta."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    response = await api.post(
        f"{BUILDER}/preview", json={**payload, **extra}, headers=head(admin_csrf)
    )
    assert response.status_code == 422, f"{caso}: {response.status_code} {response.text}"


# ---------------------------------------------------------------------------
# Precio manual
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_precio_manual_en_dolares_no_se_convierte_otra_vez(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """MANUAL_PRICE_NOT_DOUBLE_CONVERTED.

    Quien escribe 100 cotizando en dólares quiere cobrar cien dólares.
    Dividirlo otra vez por 3,75 cobraría 26,67.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    con_manual = _usd(payload)
    con_manual["items"] = [
        {**item, "commercial_sale_unit_price": "100"} for item in con_manual["items"]
    ]
    data = await _preview(api, admin_csrf, con_manual)

    for item in data["items"]:
        assert Decimal(item["raw_net_unit"]) == Decimal(100)
        # Y sigue pasando por el IGV y el redondeo contractual.
        assert Decimal(item["final_gross_unit"]) % Decimal("0.50") == Decimal(0)


# ---------------------------------------------------------------------------
# Guardar, reabrir, duplicar y confirmar
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_guardar_y_reabrir_conserva_moneda_y_tasa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """SAVE_REOPEN_USD."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    quotation_id = creada.json()["id"]

    reabierta = await api.get(f"{BUILDER}/{quotation_id}")
    assert reabierta.status_code == 200
    data = reabierta.json()
    assert data["currency_code_snapshot"] == "USD"
    assert Decimal(data["exchange_rate_snapshot"]) == Decimal(TASA)
    assert data["exchange_rate_source_snapshot"] == "MANUAL"


@pytest.mark.asyncio
async def test_duplicar_hereda_la_moneda_como_intencion_editable(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DUPLICATE_USD_*: hereda la intención, no la inmutabilidad."""
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    original = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert original.status_code == 201, original.text
    origen = original.json()

    copia = await api.post(f"{BUILDER}/{origen['id']}/duplicate", headers=head(admin_csrf))
    assert copia.status_code == 200, copia.text
    nueva = copia.json()

    assert nueva["id"] != origen["id"]
    assert nueva["status"] == "DRAFT"
    assert nueva["currency_code_snapshot"] == "USD"
    assert Decimal(nueva["exchange_rate_snapshot"]) == Decimal(TASA)

    # Y la tasa es editable en la copia sin tocar el original.
    editada = await api.put(
        f"{BUILDER}/{nueva['id']}",
        json={
            **_usd(payload, "4.00"),
            "expected_updated_at": nueva["updated_at"],
        },
        headers=head(admin_csrf),
    )
    assert editada.status_code == 200, editada.text
    assert Decimal(editada.json()["exchange_rate_snapshot"]) == Decimal("4.00")

    sin_tocar = await api.get(f"{BUILDER}/{origen['id']}")
    assert Decimal(sin_tocar.json()["exchange_rate_snapshot"]) == Decimal(TASA)


@pytest.mark.asyncio
async def test_cambiar_la_tasa_en_un_borrador_recalcula(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DRAFT_USD_RATE_CHANGE_RECALCULATES.

    Una tasa más alta compra menos dólares por sol, así que el precio en
    dólares baja.
    """
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    a_375 = await _preview(api, admin_csrf, _usd(payload, "3.75"))
    a_400 = await _preview(api, admin_csrf, _usd(payload, "4.00"))

    assert Decimal(a_400["items"][0]["raw_net_unit"]) < Decimal(a_375["items"][0]["raw_net_unit"])
    # El costo interno en soles no se movió: sólo cambió la conversión.
    assert a_400["items"][0]["commercial_base_cost"] == a_375["items"][0]["commercial_base_cost"]


@pytest.mark.asyncio
async def test_volver_a_soles_recalcula_desde_los_costos_base(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """USD_TO_PEN_RECALCULATES_FROM_BASE_PEN.

    Ir a dólares y volver tiene que devolver exactamente el precio original.
    Si la vuelta se hiciera multiplicando el precio en dólares YA redondeado
    por la tasa, el precio en soles cambiaría al pasear por otra moneda.
    """
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    original = await _preview(api, admin_csrf, payload)
    await _preview(api, admin_csrf, _usd(payload))
    de_vuelta = await _preview(api, admin_csrf, payload)

    assert de_vuelta["total_with_tax"] == original["total_with_tax"]
    for antes, despues in zip(original["items"], de_vuelta["items"], strict=True):
        assert despues["final_gross_unit"] == antes["final_gross_unit"]


@pytest.mark.asyncio
async def test_una_confirmada_en_dolares_no_se_mueve(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_USD_IMMUTABLE.

    Cambiar el IGV después de confirmar no puede tocar un precio ya
    comprometido: el cliente firmó ese número.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    borrador = creada.json()

    confirmada = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    congelada = confirmada.json()
    assert congelada["status"] == "CONFIRMED"
    assert congelada["currency_code_snapshot"] == "USD"
    assert Decimal(congelada["exchange_rate_snapshot"]) == Decimal(TASA)

    actual = await api.get(COMMERCIAL)
    await api.put(
        COMMERCIAL,
        json={
            **{k: v for k, v in actual.json().items() if k != "updated_at"},
            "tax_percent": "21",
            "expected_version": actual.json()["version"],
        },
        headers=head(admin_csrf),
    )

    despues = await api.get(f"{BUILDER}/{borrador['id']}")
    assert despues.json()["total_with_tax"] == congelada["total_with_tax"]
    assert Decimal(despues.json()["exchange_rate_snapshot"]) == Decimal(TASA)


# ---------------------------------------------------------------------------
# Totales por moneda
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_los_totales_se_agrupan_por_moneda_y_nunca_se_suman(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """TOTALS_BY_CURRENCY y PEN_USD_RAW_SUM: 0.

    Sumar soles y dólares da un número impecable aritméticamente e inútil
    financieramente.
    """
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    en_soles = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert en_soles.status_code == 201, en_soles.text
    en_dolares = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert en_dolares.status_code == 201, en_dolares.text

    response = await api.get("/api/v1/quotations/totals")
    assert response.status_code == 200, response.text
    totales = {fila["currency_code"]: fila for fila in response.json()["totals"]}

    assert set(totales) == {"PEN", "USD"}
    assert totales["PEN"]["currency_symbol"] == "S/"
    assert totales["USD"]["currency_symbol"] == "US$"
    assert Decimal(totales["PEN"]["total"]) == Decimal(en_soles.json()["total_with_tax"])
    assert Decimal(totales["USD"]["total"]) == Decimal(en_dolares.json()["total_with_tax"])
    # Y los dos importes son distintos: si se hubieran mezclado, coincidirían.
    assert totales["PEN"]["total"] != totales["USD"]["total"]


@pytest.mark.asyncio
async def test_la_ruta_de_totales_no_se_confunde_con_un_id(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """`/quotations/totals` va declarada ANTES de `/quotations/{id}`.

    Al reves, FastAPI leeria «totals» como identificador y devolveria un 422
    que nadie relacionaria con el orden de las rutas.
    """
    response = await api.get("/api/v1/quotations/totals")
    assert response.status_code == 200, response.text
    assert "totals" in response.json()


@pytest.mark.asyncio
async def test_un_precio_manual_en_dolares_no_depende_de_la_tasa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El corolario de que el precio manual esté en moneda de emisión.

    Si el usuario fija 100 dólares, cambiar el tipo de cambio no puede mover
    ese precio: lo que cambia es cuántos soles cuesta producirlo, no lo que se
    factura. Esta propiedad hizo fallar tres pruebas escritas sin darse cuenta
    de que el fixture ya traía precios manuales.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    a_375 = await _preview(api, admin_csrf, _usd(payload, "3.75"))
    a_400 = await _preview(api, admin_csrf, _usd(payload, "4.00"))

    assert a_375["total_with_tax"] == a_400["total_with_tax"]
    for linea_a, linea_b in zip(a_375["items"], a_400["items"], strict=True):
        assert linea_a["final_gross_unit"] == linea_b["final_gross_unit"]


@pytest.mark.asyncio
async def test_el_detalle_lee_la_moneda_de_la_fila_y_no_de_la_configuracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_DETAIL_USES_SNAPSHOT.

    El detalle sacaba la moneda del calculo en vez de la fila, asi que una
    cotizacion confirmada se habria explicado con la configuracion vigente en
    lugar de con lo que se congelo. Mientras todo estuvo en soles no se notaba;
    con dolares, cambiar la moneda por defecto reescribiria el pasado.
    """
    payload, _ = await _complete_payload(api, admin_csrf, db_session)
    creada = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    borrador = creada.json()

    confirmada = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text

    detalle = await api.get(f"{BUILDER}/{borrador['id']}")
    assert detalle.status_code == 200
    data = detalle.json()
    assert data["currency_code_snapshot"] == "USD"
    assert Decimal(data["exchange_rate_snapshot"]) == Decimal(TASA)
    assert data["exchange_rate_source_snapshot"] == "MANUAL"


# ---------------------------------------------------------------------------
# Traza del plan comercial por línea
# ---------------------------------------------------------------------------
async def _plan_guardado(db_session: AsyncSession, quotation_id: int) -> list[dict[str, Any]]:
    """Lee el `commercial_plan` tal y como quedó en la base."""
    filas = (
        (
            await db_session.execute(
                text(
                    "SELECT sort_order, production_snapshot FROM quotation_items "
                    "WHERE quotation_id = :qid ORDER BY sort_order"
                ),
                {"qid": quotation_id},
            )
        )
        .mappings()
        .all()
    )
    return [(fila["production_snapshot"] or {}).get("commercial_plan") or {} for fila in filas]


@pytest.mark.asyncio
async def test_el_plan_guardado_conserva_moneda_tasa_y_neto_base(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMMERCIAL_PLAN_*_PERSISTED.

    El motor calculaba bien pero el plan guardado no registraba con qué tasa:
    una línea confirmada no podía explicar su propio precio sin reconstruirlo.
    La cabecera sigue siendo la autoridad; esto es la traza que la acompaña.
    """
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    creada = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    planes = await _plan_guardado(db_session, creada.json()["id"])
    assert planes, "no se guardó ningún plan"
    for plan in planes:
        assert plan["currency"] == "USD"
        assert Decimal(str(plan["exchange_rate"])) == Decimal(TASA)
        assert plan["exchange_rate_source"] == "MANUAL"
        assert plan["raw_net_unit_base"] is not None
        # Y la traza cuadra: el neto en dólares sale de dividir el de soles.
        assert Decimal(str(plan["raw_net_unit"])) == (
            Decimal(str(plan["raw_net_unit_base"])) / Decimal(TASA)
        )


@pytest.mark.asyncio
async def test_el_plan_en_soles_no_inventa_una_tasa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMMERCIAL_PLAN_PEN_CANONICAL: PEN no lleva tasa ni fuente."""
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    creada = await api.post(BUILDER, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    for plan in await _plan_guardado(db_session, creada.json()["id"]):
        assert plan["currency"] == "PEN"
        assert plan["exchange_rate"] is None
        assert plan["exchange_rate_source"] is None
        # Sin conversión, el neto base y el de emisión coinciden.
        assert Decimal(str(plan["raw_net_unit_base"])) == Decimal(str(plan["raw_net_unit"]))


@pytest.mark.asyncio
async def test_la_linea_no_puede_llevar_una_tasa_distinta_de_la_cabecera(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMMERCIAL_PLAN_HEADER_*_MATCH y ONE_EXCHANGE_RATE_PER_QUOTATION.

    Una línea a 3,31 con la cabecera a 3,75 daría un total que nadie puede
    explicar. La copia es traza, no una segunda autoridad.
    """
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    creada = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    cabecera = creada.json()

    planes = await _plan_guardado(db_session, cabecera["id"])
    assert len(planes) > 1, "hacen falta varias líneas para probar esto"
    assert {plan["currency"] for plan in planes} == {cabecera["currency_code_snapshot"]}
    assert {Decimal(str(plan["exchange_rate"])) for plan in planes} == {
        Decimal(cabecera["exchange_rate_snapshot"])
    }
    assert {plan["exchange_rate_source"] for plan in planes} == {
        cabecera["exchange_rate_source_snapshot"]
    }


@pytest.mark.asyncio
async def test_la_traza_sobrevive_a_guardar_y_reabrir(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMMERCIAL_PLAN_SAVE_REOPEN."""
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    creada = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    reabierta = await api.get(f"{BUILDER}/{creada.json()['id']}")
    assert reabierta.status_code == 200
    for linea in reabierta.json()["items"]:
        assert linea["currency_code_snapshot"] == "USD"
        assert Decimal(linea["exchange_rate_snapshot"]) == Decimal(TASA)
        # El neto en soles es mayor que el de dólares: se dividió por 3,75.
        assert Decimal(linea["raw_net_unit_base"]) > Decimal(linea["raw_net_unit"])


@pytest.mark.asyncio
async def test_una_confirmada_conserva_su_traza_aunque_cambie_la_configuracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_COMMERCIAL_PLAN_IMMUTABLE."""
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    creada = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    borrador = creada.json()

    confirmada = await api.post(
        f"{BUILDER}/{borrador['id']}/confirm",
        json={"expected_updated_at": borrador["updated_at"]},
        headers=head(admin_csrf),
    )
    assert confirmada.status_code == 200, confirmada.text
    antes = await _plan_guardado(db_session, borrador["id"])

    actual = await api.get(COMMERCIAL)
    await api.put(
        COMMERCIAL,
        json={
            **{k: v for k, v in actual.json().items() if k != "updated_at"},
            "tax_percent": "21",
            "expected_version": actual.json()["version"],
        },
        headers=head(admin_csrf),
    )

    despues = await _plan_guardado(db_session, borrador["id"])
    assert despues == antes, "la configuración no puede mover un precio confirmado"


@pytest.mark.asyncio
async def test_duplicar_no_comparte_el_plan_con_el_original(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DUPLICATE_COMMERCIAL_PLAN_NOT_SHARED."""
    payload = _por_margen((await _complete_payload(api, admin_csrf, db_session))[0])
    original = await api.post(BUILDER, json=_usd(payload), headers=head(admin_csrf))
    assert original.status_code == 201, original.text
    origen = original.json()

    copia = await api.post(f"{BUILDER}/{origen['id']}/duplicate", headers=head(admin_csrf))
    assert copia.status_code == 200, copia.text
    nueva = copia.json()

    editada = await api.put(
        f"{BUILDER}/{nueva['id']}",
        json={**_usd(payload, "4.00"), "expected_updated_at": nueva["updated_at"]},
        headers=head(admin_csrf),
    )
    assert editada.status_code == 200, editada.text

    planes_copia = await _plan_guardado(db_session, nueva["id"])
    planes_origen = await _plan_guardado(db_session, origen["id"])
    assert {Decimal(str(p["exchange_rate"])) for p in planes_copia} == {Decimal("4.00")}
    assert {Decimal(str(p["exchange_rate"])) for p in planes_origen} == {Decimal(TASA)}
