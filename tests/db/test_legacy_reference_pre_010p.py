"""Recorrido vivo de componentes del caso de Excel anterior a 010P.

Los totales históricos de v1 se conservan en
``tests/fixtures/LEGACY_REFERENCE_PRE_010P.json``. Este smoke usa hoy reglas
010P y solo comprueba componentes que esa fase no cambió, junto con la
coherencia entre precio, confirmación y PDF.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pypdf import PdfReader

from tests.db.v2_capacidades import habilitar

#: Caso canonico del Excel anterior a 010P: referencia historica, no oraculo de
#: las reglas 010P (tests/fixtures/LEGACY_REFERENCE_PRE_010P.json).
pytestmark = pytest.mark.legacy_reference_pre_010p

V2 = "/api/v1/quotations-v2"
KILNS = "/api/v1/kilns"
V2_SETTINGS = "/api/v1/quoter-v2/settings"
COMMERCIAL = "/api/v1/settings/commercial"
WORKERS = "/api/v1/quoter-v2/workers"
TECHNIQUES = "/api/v1/quoter-v2/techniques"

#: Las columnas de dinero guardan seis decimales; el Excel calcula en doble.
MILLONESIMA = Decimal("0.000001")

#: Hoja «Productos». Nombre, cantidad, medidas, pasta, gramos, esmalte.
PRODUCTOS: list[dict[str, Any]] = [
    {
        "name": "Plato palta",
        "quantity": 20,
        "dims": ("18", "12", "3"),
        "body": "Terranova",
        "grams": "450",
        "glaze": True,
    },
    {
        "name": "Tasa Buho",
        "quantity": 50,
        "dims": ("1", "15", "3"),
        "body": "Terranova",
        "grams": "300",
        "glaze": False,
    },
    {
        "name": "PLATOS HONDOS CHICOS",
        "quantity": 12,
        "dims": ("15", "12", "5"),
        "body": "Arcilla reciclada",
        "grams": "400",
        "glaze": True,
    },
]

#: Hoja «Mano de obra»: (producto, tecnica). Todo lo hace el trabajador interno.
TAREAS = [
    ("Plato palta", "A mano"),
    ("Tasa Buho", "Torno facil"),
    ("PLATOS HONDOS CHICOS", "Torno dificil"),
    ("Plato palta", "Vidriado por inmersion"),
    ("PLATOS HONDOS CHICOS", "Vidriado a mano alzada"),
]
#: Hoja «Configuracion»: rendimiento por jornada de cada tecnica.
RENDIMIENTOS = {
    "A mano": "15",
    "Torno facil": "50",
    "Torno dificil": "25",
    "Vidriado por inmersion": "50",
    "Vidriado a mano alzada": "25",
}


def cerca(valor: Any, esperado: str, etiqueta: str, tolerancia: Decimal = MILLONESIMA) -> None:
    real = Decimal(str(valor))
    assert abs(real - Decimal(esperado)) <= tolerancia, (
        f"{etiqueta}: el sistema dice {real} y el Excel {esperado}"
    )


def exacto(valor: Any, esperado: str, etiqueta: str) -> None:
    assert Decimal(str(valor)) == Decimal(esperado), (
        f"{etiqueta}: el sistema dice {valor} y el Excel {esperado}"
    )


async def _post(api: httpx.AsyncClient, csrf: str, url: str, datos: dict[str, Any]) -> Any:
    respuesta = await api.post(url, json=datos, headers={"X-CSRF-Token": csrf})
    assert respuesta.status_code in (200, 201), respuesta.text
    return respuesta.json()


async def _put(api: httpx.AsyncClient, csrf: str, url: str, datos: dict[str, Any]) -> Any:
    respuesta = await api.put(url, json=datos, headers={"X-CSRF-Token": csrf})
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


async def preparar_configuracion(api: httpx.AsyncClient, csrf: str) -> dict[str, int]:
    """La hoja «Configuracion»: IGV, escalon, hornos, tarifas y defaults."""
    vigente = (await api.get(COMMERCIAL)).json()
    await _put(
        api,
        csrf,
        COMMERCIAL,
        {"version": vigente["version"], "tax_percent": "18", "rounding_step": "0.5"},
    )

    hornos: dict[str, int] = {}
    for nombre, capacidad, gas, externo in (
        ("Chico", "17000", ("35", "70"), ("200", "250")),
        ("Grande", "200000", ("55", "110"), ("700", "1200")),
    ):
        horno = await _post(
            api, csrf, KILNS, {"name": f"Horno {nombre} 010J", "capacity_volume_cm3": capacidad}
        )
        hornos[nombre] = int(horno["id"])
        for indice, tipo in enumerate(("LOW", "HIGH")):
            await _put(
                api,
                csrf,
                f"{V2_SETTINGS}/kiln-rates/{hornos[nombre]}/{tipo}",
                {"gas_cost": gas[indice], "external_rate": externo[indice]},
            )

    actual = (await api.get(V2_SETTINGS)).json()["settings"]
    await _put(
        api,
        csrf,
        V2_SETTINGS,
        {
            "expected_version": actual["version"],
            "workday_hours": "8",
            "space_service_cost_per_day": "140",
            "administrative_cost_per_quote": "200",
            "commercial_factor_min": "2",
            "commercial_factor_default": "3",
            "commercial_factor_max": "10",
            "retail_kiln_id": hornos["Chico"],
            "wholesale_kiln_id": hornos["Grande"],
            "low_fire_enabled_default": True,
            "high_fire_enabled_default": True,
            "default_customer_kind": "EXTERNAL",
            "illustration_daily_rate": "110",
            "illustration_pieces_per_workday": "50",
            "piece_separation_cm": "3",
        },
    )
    return hornos


async def _material(
    api: httpx.AsyncClient, csrf: str, categoria: int, nombre: str, tipo: str, compra: str
) -> int:
    """Un material con su costo por gramo = compra / 100 000 g (o / 1000 g el esmalte)."""
    producto = await _post(
        api,
        csrf,
        "/api/v1/products",
        {
            "name": f"{nombre} 010J",
            "product_type": "RAW_MATERIAL",
            "product_category_id": categoria,
            "base_uom_code": "g",
            "purchasable": True,
        },
    )
    material_id = int(producto["id"])
    cantidad = "1000" if tipo == "GLAZE" else "100000"
    await _put(
        api,
        csrf,
        f"/api/v1/quoter-v2/materials/{material_id}",
        {
            "material_kind": tipo,
            "origin": "PURCHASE",
            "purchase_quantity": cantidad,
            "purchase_cost": compra,
            "transport_cost": "0",
        },
    )
    return material_id


async def preparar_maestros(
    api: httpx.AsyncClient, csrf: str, tipo_trabajador: str = "INTERNAL"
) -> tuple[dict[str, int], int, dict[str, int]]:
    categoria = int(
        (
            await _post(
                api, csrf, "/api/v1/categories", {"name": "Cat Excel 010J", "parent_id": None}
            )
        )["id"]
    )
    materiales = {
        # 0,0013 y 0,0012 soles por gramo; el esmalte premium, 0,12.
        "Terranova": await _material(api, csrf, categoria, "Terranova", "BODY", "130"),
        "Arcilla reciclada": await _material(
            api, csrf, categoria, "Arcilla reciclada", "BODY", "120"
        ),
        "Esmalte premium": await _material(api, csrf, categoria, "Esmalte premium", "GLAZE", "120"),
    }
    trabajador = await _post(
        api,
        csrf,
        WORKERS,
        # Con jornal: el costo cero del interno es una REGLA, no un dato en cero.
        {"name": "Trabajador taller 010J", "worker_type": tipo_trabajador, "daily_rate": "220"},
    )
    tecnicas: dict[str, int] = {}
    for indice, (nombre, rendimiento) in enumerate(RENDIMIENTOS.items()):
        tecnica = await _post(
            api,
            csrf,
            TECHNIQUES,
            {
                "code": f"T010J-{indice}",
                "name": nombre,
                "default_capacity_per_workday": rendimiento,
            },
        )
        tecnicas[nombre] = int(tecnica["id"])
    return materiales, int(trabajador["id"]), tecnicas


async def armar_cotizacion(
    api: httpx.AsyncClient, csrf: str, tipo_trabajador: str = "INTERNAL"
) -> tuple[int, dict[str, int], dict[str, int]]:
    """El recorrido del asistente con las entradas del Excel. Devuelve la cotizacion."""
    hornos = await preparar_configuracion(api, csrf)
    materiales, worker_id, tecnicas = await preparar_maestros(api, csrf, tipo_trabajador)

    cotizacion = int(
        (await _post(api, csrf, V2, {"name": "Caso Excel 010J", "production_type": "RETAIL"}))["id"]
    )
    lineas: dict[str, int] = {}
    for producto in PRODUCTOS:
        largo, ancho, alto = producto["dims"]
        datos: dict[str, Any] = {
            "product_name": producto["name"],
            "quantity": producto["quantity"],
            "production_time_per_unit_minutes": "10",
            "length_cm": largo,
            "width_cm": ancho,
            "height_cm": alto,
            "body_material_id": materiales[producto["body"]],
            "body_unit_weight": producto["grams"],
            "requires_glaze": producto["glaze"],
        }
        if producto["glaze"]:
            datos["glaze_material_id"] = materiales["Esmalte premium"]
        linea = await _post(api, csrf, f"{V2}/{cotizacion}/products", datos)
        lineas[producto["name"]] = int(linea["id"])

    await habilitar(api, csrf, worker_id, *tecnicas.values())
    cantidades = {producto["name"]: producto["quantity"] for producto in PRODUCTOS}
    for nombre, tecnica in TAREAS:
        await _post(
            api,
            csrf,
            f"{V2}/{cotizacion}/labor",
            {
                "v2_quotation_product_id": lineas[nombre],
                "worker_id": worker_id,
                "technique_id": tecnicas[tecnica],
                "quantity": str(cantidades[nombre]),
            },
        )

    # Hoja «Ilustracion»: 20 piezas de «Plato palta», 50 por jornada de 8 h a S/110.
    ilustracion = await _put(
        api,
        csrf,
        f"{V2}/{cotizacion}/illustration",
        {
            "illustration_enabled": True,
            "illustration_quantity": "0",
            "lines": [{"line_id": lineas["Plato palta"], "quantity": "20"}],
        },
    )
    exacto(ilustracion["total_cost"], "44", "ilustracion")
    exacto(ilustracion["cost"], "0", "ilustracion sin producto")

    await _put(api, csrf, f"{V2}/{cotizacion}/planning", {"effective_work_days": 4})
    return cotizacion, lineas, hornos


class TestElCasoCanonicoDelExcelFinal:
    async def test_quema_compartida_con_separacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _lineas, hornos = await armar_cotizacion(api, admin_csrf)
        quema = (await api.get(f"{V2}/{cotizacion}/firing")).json()

        assert quema["kiln_id"] == hornos["Chico"], "por menor nace en el horno chico"
        assert quema["firing_mode"] == "SHARED"
        exacto(quema["piece_separation_cm"], "3", "separacion")
        exacto(quema["total_volume_cm3"], "85320", "volumen con separacion")
        cerca(quema["occupancy_percent"], "501.882353", "ocupacion")
        assert quema["firing_count"] == 6, "hornadas fisicas"
        exacto(quema["billed_load"], "5.018823529412", "carga facturada")
        cerca(quema["commercial_total"], "2258.470588", "quema comercial")
        cerca(quema["gas_total"], "526.976471", "gas")
        cerca(quema["difference"], "1731.494117", "diferencia de quema")
        assert [Decimal(str(c)) for c in quema["batch_loads"]][-1] == Decimal("1.882353")

        grande = next(h for h in quema["kilns"] if h["kiln_id"] == hornos["Grande"])
        cerca(grande["occupancy_percent"], "42.66", "ocupacion grande")
        cerca(grande["commercial_total"], "810.54", "quema comercial grande")
        cerca(grande["gas_total"], "70.389", "gas grande")
        # Sugerencia, no cambio: el horno sigue siendo el chico.
        assert quema["cheaper_kiln"]["kiln_id"] == hornos["Grande"]
        cerca(quema["cheaper_kiln"]["savings"], "1447.930588", "Grande reduce la quema en")
        assert quema["kiln_id"] == hornos["Chico"]

        por_linea = {linea["line_id"]: linea for linea in quema["lines"]}
        assert sorted(Decimal(str(v["total_volume_cm3"])) for v in por_linea.values()) == [
            Decimal(21600),
            Decimal(25920),
            Decimal(37800),
        ]

    async def test_costos_precio_ganancia_y_documento(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _lineas, _hornos = await armar_cotizacion(api, admin_csrf)

        mano_de_obra = (await api.get(f"{V2}/{cotizacion}/labor")).json()
        exacto(mano_de_obra["labor_cost"], "0", "mano de obra interna")

        precio = (await api.get(f"{V2}/{cotizacion}/pricing")).json()
        cerca(precio["materials_cost"], "285.36", "materiales")
        exacto(precio["labor_cost"], "0", "mano de obra")
        cerca(precio["illustration_cost"], "44", "ilustracion")
        cerca(precio["gas_cost"], "526.976471", "gas")
        cerca(precio["firing_commercial_cost"], "2258.470588", "quema comercial")
        exacto(precio["administration_cost"], "0", "administracion retail")
        exacto(precio["extras_cost"], "0", "adicionales")
        assert precio["pricing_rules_version"] == 2
        assert Decimal(str(precio["active_production_minutes"])) > 0
        subtotal_calculado = sum(
            (Decimal(str(linea["line_subtotal"])) for linea in precio["lines"]),
            Decimal(0),
        )
        subtotal = Decimal(str(precio["subtotal"]))
        total = Decimal(str(precio["total"]))
        assert subtotal == subtotal_calculado
        assert total == subtotal + Decimal(str(precio["tax"]))

        # --- El documento: emitido con esos numeros y sin un costo interno ----
        cliente = await _post(
            api,
            admin_csrf,
            "/api/v1/partners",
            {"name": "Cliente demo Excel 010J", "role": "CLIENT"},
        )
        await _put(api, admin_csrf, f"{V2}/{cotizacion}", {"customer_id": int(cliente["id"])})
        resumen = (await api.get(f"{V2}/{cotizacion}/confirmation-preview")).json()
        assert resumen["can_confirm"], resumen["blockers"]
        await _post(
            api,
            admin_csrf,
            f"{V2}/{cotizacion}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        congelado = (await api.get(f"{V2}/{cotizacion}/confirmation-preview")).json()
        assert Decimal(str(congelado["subtotal_amount"])) == subtotal
        assert Decimal(str(congelado["total_amount"])) == total

        pdf = await api.get(f"{V2}/{cotizacion}/pdf")
        assert pdf.status_code == 200, pdf.text
        texto = (
            "".join(
                (pagina.extract_text() or "") for pagina in PdfReader(io.BytesIO(pdf.content)).pages
            )
            .replace(" ", "")
            .replace(chr(10), "")
        )
        assert f"S/{total:,.2f}" in texto
        for prohibido in (
            "Costoreal",
            "Gasreal",
            "Ganancia",
            "Margen",
            "Factor",
            "Compartida",
            "Separacion",
            "Hornada",
        ):
            assert prohibido not in texto, prohibido

    async def test_reducciones_sugeridas_sin_aplicar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _lineas, hornos = await armar_cotizacion(api, admin_csrf)
        antes = (await api.get(f"{V2}/{cotizacion}/pricing")).json()

        reducciones = (await api.get(f"{V2}/{cotizacion}/reductions")).json()
        assert Decimal(str(reducciones["current_subtotal"])) == Decimal(str(antes["subtotal"]))
        items = {item["code"]: item for item in reducciones["items"]}

        assert items["OTHER_KILN"]["suggestion"] == "Horno Grande 010J"
        exacto(items["MIN_FACTOR"]["suggestion"], "2", "nunca por debajo de x2")
        assert not items["REMOVE_EXTRAS"]["applicable"]
        assert not items["INTERNAL_STAFF"]["applicable"], "ya es todo interno"
        assert not items["SHARED_FIRING"]["applicable"], "ya es compartida"

        # Sugerir no es aplicar: nada se movio.
        despues = (await api.get(f"{V2}/{cotizacion}/pricing")).json()
        assert despues["subtotal"] == antes["subtotal"]
        assert (await api.get(f"{V2}/{cotizacion}/firing")).json()["kiln_id"] == hornos["Chico"]

    async def test_exclusiva_cobra_seis_hornadas_y_vuelve(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _lineas, _hornos = await armar_cotizacion(api, admin_csrf)
        quema = await _put(
            api, admin_csrf, f"{V2}/{cotizacion}/firing", {"firing_mode": "EXCLUSIVE"}
        )
        exacto(quema["billed_load"], "6", "carga exclusiva")
        exacto(quema["commercial_total"], "2700", "comercial exclusiva")
        exacto(quema["gas_total"], "630", "gas exclusiva")

        reducciones = (await api.get(f"{V2}/{cotizacion}/reductions")).json()
        compartida = next(i for i in reducciones["items"] if i["code"] == "SHARED_FIRING")
        assert compartida["applicable"]
        cerca(compartida["cost_reduction"], "441.529412", "exclusiva menos compartida")

        vuelta = await _put(api, admin_csrf, f"{V2}/{cotizacion}/firing", {"firing_mode": "SHARED"})
        cerca(vuelta["commercial_total"], "2258.470588", "de vuelta a compartida")

    async def test_separacion_cero_y_solo_baja(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _lineas, _hornos = await armar_cotizacion(api, admin_csrf)
        quema = await _put(
            api,
            admin_csrf,
            f"{V2}/{cotizacion}/firing",
            {"piece_separation_cm": "0", "high_fire_enabled": False},
        )
        # 18x12x3x20 + 1x15x3x50 + 15x12x5x12 = 12960 + 2250 + 10800.
        exacto(quema["total_volume_cm3"], "26010", "volumen sin separacion")
        exacto(quema["billed_load"], "1.53", "carga")
        exacto(quema["commercial_total"], "306", "solo baja: 1,53 x 200")
        exacto(quema["gas_total"], "53.55", "solo baja: 1,53 x 35")
        assert quema["high_fire_count"] == 0

    async def test_personal_externo_se_paga_por_hora(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Mismo caso con el trabajador EXTERNO, ya con las reglas 010P.

        El importe historico (812,533343: horas de CADA tarea x 27,5) era de v1 y
        vive solo en LEGACY_REFERENCE_PRE_010P. Con 010P el externo cuesta por
        las horas ACTIVAS del pedido, el MAXIMO de las lineas: 50 tazas x 10 min
        = 500 min = 8,333333 h. Al cliente, 8,333333 x 220/8 = 229,166667; al
        taller, jornales enteros: ceil(8,33 / 8) = 2 x 220 = 440.
        """
        cotizacion, _lineas, _hornos = await armar_cotizacion(api, admin_csrf, "EXTERNAL")
        #: Las horas se guardan con seis decimales: 8,333333 x 27,5.
        horas = Decimal("0.0001")
        precio = (await api.get(f"{V2}/{cotizacion}/pricing")).json()
        cerca(precio["commercial_external_labor_cost"], "229.166667", "externo comercial", horas)
        cerca(precio["real_external_labor_cost"], "440", "externo real")
        cerca(precio["labor_cost_gap"], "210.833333", "brecha", horas)
        mano_de_obra = (await api.get(f"{V2}/{cotizacion}/labor")).json()
        cerca(mano_de_obra["labor_cost"], "229.166667", "mano de obra del pedido", horas)
        assert all(Decimal(t["labor_cost"]) == 0 for t in mano_de_obra["items"])
        reducciones = (await api.get(f"{V2}/{cotizacion}/reductions")).json()
        interno = next(i for i in reducciones["items"] if i["code"] == "INTERNAL_STAFF")
        assert interno["applicable"]
        cerca(interno["cost_reduction"], "229.166667", "ahorro con interno", horas)
