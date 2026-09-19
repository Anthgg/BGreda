"""Fase 010J — el caso canonico del Excel FINAL, recorrido por la API del flujo.

Oraculo: «Cotizador_Greda_V2_modelo_CORREGIDO_SIN_NOMBRE.xlsx». A diferencia de
la prueba de 010G, aqui no se fuerza ningun importe con acuerdos: la cotizacion
se arma con las ENTRADAS de la hoja —pesos, costo por gramo de cada pasta, el
esmalte de referencia, las tecnicas con su rendimiento, el trabajador interno,
la ilustracion del producto, los hornos con sus tarifas— y lo que sale tiene
que ser, al centimo, lo que el Excel calcula:

    Externo, por menor, horno Chico, baja + alta, quema COMPARTIDA,
    separacion 3 cm, factor x3, PEN, IGV 18 %, 4 dias efectivos.

    Plato palta 20 u x S/242,00       4840,00
    Tasa Buho 50 u x S/45,50          2275,00
    PLATOS HONDOS CHICOS 12 u x 245   2940,00
    SUBTOTAL 10055,00   IGV 1809,90   TOTAL 11864,90

Las reglas que el caso ejercita son justamente las que 010J cambio: quema
proporcional (501,88 % cobra 5,0188 hornadas, no 6), separacion entre piezas,
mano de obra interna a costo cero, ilustracion cargada al producto que la
lleva y precio objetivo al factor x3.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import httpx
from pypdf import PdfReader

from tests.db.v2_capacidades import habilitar

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
        cotizacion, lineas, _hornos = await armar_cotizacion(api, admin_csrf)

        mano_de_obra = (await api.get(f"{V2}/{cotizacion}/labor")).json()
        exacto(mano_de_obra["labor_cost"], "0", "mano de obra interna")

        precio = (await api.get(f"{V2}/{cotizacion}/pricing")).json()
        cerca(precio["materials_cost"], "285.36", "materiales")
        exacto(precio["labor_cost"], "0", "mano de obra")
        cerca(precio["illustration_cost"], "44", "ilustracion")
        exacto(precio["space_cost"], "560", "espacio")
        exacto(precio["administration_cost"], "200", "administracion")
        exacto(precio["extras_cost"], "0", "adicionales")
        cerca(precio["production_cost"], "3347.830588", "costo de produccion")
        cerca(precio["real_cost"], "1616.336471", "costo real")
        exacto(precio["factor_target"], "3", "factor objetivo")
        cerca(precio["price_min"], "6695.661176", "precio minimo x2")
        cerca(precio["price_target"], "10043.491764", "precio objetivo x3")

        unitarios = {linea["line_id"]: linea for linea in precio["lines"]}
        for nombre, unitario, subtotal in (
            ("Plato palta", "242", "4840"),
            ("Tasa Buho", "45.5", "2275"),
            ("PLATOS HONDOS CHICOS", "245", "2940"),
        ):
            exacto(unitarios[lineas[nombre]]["unit_price"], unitario, f"unitario {nombre}")
            exacto(unitarios[lineas[nombre]]["line_subtotal"], subtotal, f"subtotal {nombre}")

        exacto(precio["subtotal"], "10055", "subtotal")
        exacto(precio["tax"], "1809.90", "IGV")
        exacto(precio["total"], "11864.90", "total")
        cerca(precio["rounding_adjustment"], "11.508236", "ajuste de redondeo")
        cerca(precio["estimated_profit"], "8438.663529", "ganancia")
        cerca(precio["effective_margin_percent"], "83.925048", "margen %")

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
        exacto(congelado["subtotal_amount"], "10055", "subtotal emitido")
        exacto(congelado["total_amount"], "11864.90", "total emitido")

        pdf = await api.get(f"{V2}/{cotizacion}/pdf")
        assert pdf.status_code == 200, pdf.text
        texto = (
            "".join(
                (pagina.extract_text() or "") for pagina in PdfReader(io.BytesIO(pdf.content)).pages
            )
            .replace(" ", "")
            .replace(chr(10), "")
        )
        assert "S/11,864.90" in texto
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
        exacto(reducciones["current_subtotal"], "10055", "subtotal actual")
        items = {item["code"]: item for item in reducciones["items"]}

        cerca(items["OTHER_KILN"]["savings"], "4343.791764", "ahorro con Grande")
        cerca(items["OTHER_KILN"]["estimated_subtotal"], "5711.208236", "estimado con Grande")
        assert items["OTHER_KILN"]["suggestion"] == "Horno Grande 010J"
        cerca(items["MIN_FACTOR"]["savings"], "3347.830588", "ahorro factor x2")
        cerca(items["MIN_FACTOR"]["estimated_subtotal"], "6707.169412", "estimado factor x2")
        exacto(items["MIN_FACTOR"]["suggestion"], "2", "nunca por debajo de x2")
        exacto(items["REMOVE_ILLUSTRATION"]["savings"], "132", "ahorro sin ilustracion")
        exacto(items["REMOVE_ILLUSTRATION"]["estimated_subtotal"], "9923", "sin ilustracion")
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
        """Mismo caso con el trabajador EXTERNO: 29,546667 h a 220/8 = 27,5 por hora."""
        cotizacion, _lineas, _hornos = await armar_cotizacion(api, admin_csrf, "EXTERNAL")
        mano_de_obra = (await api.get(f"{V2}/{cotizacion}/labor")).json()
        # 10,666667 + 8 + 3,84 + 3,2 + 3,84 horas.
        cerca(mano_de_obra["labor_cost"], "812.533343", "externo por hora", Decimal("0.0001"))
        reducciones = (await api.get(f"{V2}/{cotizacion}/reductions")).json()
        interno = next(i for i in reducciones["items"] if i["code"] == "INTERNAL_STAFF")
        assert interno["applicable"]
        cerca(interno["cost_reduction"], "812.533343", "ahorro con interno", Decimal("0.0001"))
