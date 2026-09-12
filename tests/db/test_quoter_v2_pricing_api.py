"""Fase 010F — el motor economico del Cotizador V2 contra PostgreSQL real.

Lo que aqui se comprueba, por orden de gravedad:

1. **las dos bases no se intercambian.** El costo REAL lleva el gas que se
   quema; el costo de PRODUCCION, la tarifa que se cobra por encender. Los dos
   son numeros creibles y cambiarlos de sitio deja el margen invertido;
2. **el factor es un multiplicador global.** x3 sobre 1.000 son 3.000, no
   4.000; y es UNO por cotizacion, no uno por producto;
3. **el IGV va al final.** Aplicar el factor sobre un total que ya lo lleva
   cobraria al cliente el impuesto triplicado;
4. **el redondeo sube y el documento se reconstruye desde el unitario.** Si el
   subtotal saliera del precio anterior al redondeo, nadie podria sumar el
   documento a mano y llegar al mismo total;
5. **lo repartido suma exactamente el total.** Ni un centimo fuera;
6. **una cotizacion emitida no cambia de precio.**

Los numeros de referencia salen del Excel aprobado; la conciliacion celda a
celda vive en `tests/unit/test_quoter_v2_pricing_excel.py`.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

V2 = "/api/v1/quotations-v2"
KILNS = "/api/v1/kilns"
SETTINGS = "/api/v1/quoter-v2/settings"
COMMERCIAL = "/api/v1/settings/commercial"
MATERIALS = "/api/v1/quoter-v2/materials"
PRODUCTS = "/api/v1/products"
CATEGORIES = "/api/v1/categories"
WORKERS = "/api/v1/quoter-v2/workers"
TECHNIQUES = "/api/v1/quoter-v2/techniques"

#: Horno chico del Excel.
CHICO = 17000
#: Cien kilos en gramos: el maestro lleva los materiales en su unidad base.
CIEN_KILOS = "100000"


# ---------------------------------------------------------------------------
# Montaje
# ---------------------------------------------------------------------------
async def _categoria(api: httpx.AsyncClient, csrf: str, nombre: str) -> int:
    response = await api.post(
        CATEGORIES, json={"name": nombre, "parent_id": None}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def crear_pasta(api: httpx.AsyncClient, csrf: str, nombre: str) -> int:
    """Una pasta valorizada a S/0,0013 el gramo: 100 de compra y 30 de transporte."""
    categoria = await _categoria(api, csrf, f"Cat {nombre}")
    producto = await api.post(
        PRODUCTS,
        json={
            "name": nombre,
            "product_type": "RAW_MATERIAL",
            "product_category_id": categoria,
            "base_uom_code": "g",
            "purchasable": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert producto.status_code == 201, producto.text
    product_id = int(producto.json()["id"])
    tarifa = await api.put(
        f"{MATERIALS}/{product_id}",
        json={
            "material_kind": "BODY",
            "origin": "PURCHASE",
            "purchase_quantity": CIEN_KILOS,
            "purchase_cost": "100",
            "transport_cost": "30",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert tarifa.status_code == 200, tarifa.text
    return product_id


async def crear_horno(api: httpx.AsyncClient, csrf: str, nombre: str) -> dict[str, Any]:
    """Horno chico con las tarifas del Excel: gas 35/70, externo 200/250."""
    response = await api.post(
        KILNS,
        json={"name": nombre, "capacity_volume_cm3": str(CHICO)},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    horno = dict(response.json())
    for tipo, gas, externo in (("LOW", "35", "200"), ("HIGH", "70", "250")):
        tarifa = await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/{tipo}",
            json={"gas_cost": gas, "external_rate": externo, "student_rate": "90"},
            headers={"X-CSRF-Token": csrf},
        )
        assert tarifa.status_code == 200, tarifa.text
    return horno


async def crear_cotizacion(api: httpx.AsyncClient, csrf: str, **overrides: Any) -> int:
    payload: dict[str, Any] = {"name": "Precio 010F"}
    payload.update(overrides)
    response = await api.post(V2, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def anadir_linea(
    api: httpx.AsyncClient, csrf: str, quotation_id: int, **campos: Any
) -> dict[str, Any]:
    response = await api.post(
        f"{V2}/{quotation_id}/products", json=campos, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def precio(api: httpx.AsyncClient, quotation_id: int) -> dict[str, Any]:
    response = await api.get(f"{V2}/{quotation_id}/pricing")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def poner_precio(
    api: httpx.AsyncClient, csrf: str, quotation_id: int, **campos: Any
) -> httpx.Response:
    return await api.put(
        f"{V2}/{quotation_id}/pricing", json=campos, headers={"X-CSRF-Token": csrf}
    )


#: Cada escenario crea su horno y su pasta, y los nombres son unicos en el
#: maestro. Un contador evita que dos escenarios del mismo test choquen.
_SECUENCIA = itertools.count(1)


async def configurar_igv(api: httpx.AsyncClient, csrf: str, porcentaje: str = "18") -> None:
    """Deja el IGV de la casa puesto.

    La configuracion comercial nace SIN impuesto —es anulable a proposito— y
    una cotizacion creada antes de ponerlo se lleva un `None` congelado. El
    motor lo avisa y costea sin IGV; estas pruebas quieren el caso con IGV.
    """
    actual = (await api.get(COMMERCIAL)).json()
    respuesta = await api.put(
        COMMERCIAL,
        json={"version": actual["version"], "tax_percent": porcentaje},
        headers={"X-CSRF-Token": csrf},
    )
    assert respuesta.status_code == 200, respuesta.text


async def escenario(
    api: httpx.AsyncClient,
    csrf: str,
    *,
    dias: int | None = 2,
    piezas: int = 10,
) -> tuple[int, dict[str, Any]]:
    """Una cotizacion completa: material, quema, mano de obra y dias.

    Diez piezas de 10 x 10 x 10 ocupan 10.000 cm3 de los 17.000 del horno: una
    sola hornada, con las dos quemas encendidas. De ahi S/450 de tarifa
    comercial y S/105 de gas.
    """
    marca = next(_SECUENCIA)
    await configurar_igv(api, csrf)
    horno = await crear_horno(api, csrf, f"Horno del precio {marca}")
    pasta = await crear_pasta(api, csrf, f"Arcilla del precio {marca}")
    cotizacion = await crear_cotizacion(api, csrf)

    await api.put(
        f"{V2}/{cotizacion}/firing",
        json={"kiln_id": horno["id"], "customer_kind": "EXTERNAL"},
        headers={"X-CSRF-Token": csrf},
    )
    linea = await anadir_linea(
        api,
        csrf,
        cotizacion,
        product_name="Pieza",
        quantity=piezas,
        length_cm="10",
        width_cm="10",
        height_cm="10",
        body_material_id=pasta,
        body_unit_weight="500",
    )
    if dias is not None:
        respuesta = await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": dias},
            headers={"X-CSRF-Token": csrf},
        )
        assert respuesta.status_code == 200, respuesta.text
    return cotizacion, linea


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    async def test_sin_sesion_no_se_lee_el_precio(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(f"{V2}/1/pricing")).status_code == 401

    async def test_el_precio_es_de_administracion(self, api: httpx.AsyncClient) -> None:
        """Devuelve el costo real, el gas y la ganancia: es informacion interna."""
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        assert (await api.get(f"{V2}/1/pricing")).status_code == 403
        assert (await poner_precio(api, csrf, 1, commercial_factor="3")).status_code == 403


# ---------------------------------------------------------------------------
# Las dos bases de costo
# ---------------------------------------------------------------------------
class TestCostos:
    async def test_los_componentes_llegan_de_las_fases_anteriores(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Materiales de 010C, mano de obra de 010D, quema de 010E."""
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        # 10 piezas x 500 g x S/0,0013 el gramo.
        assert Decimal(datos["materials_cost"]) == Decimal("6.5")
        assert Decimal(datos["firing_commercial_cost"]) == Decimal(450)
        assert Decimal(datos["gas_cost"]) == Decimal(105)
        assert Decimal(datos["space_cost"]) == Decimal(280)
        assert Decimal(datos["administration_cost"]) == Decimal(200)

    async def test_el_costo_de_produccion_lleva_la_tarifa_y_el_real_el_gas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La diferencia entre las dos bases es EXACTAMENTE la de la quema.

        Es la comprobacion cruzada que caza el intercambio: si alguien pusiera
        el gas donde va la tarifa, los dos totales seguirian pareciendo
        razonables y solo esta resta lo delataria.
        """
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        produccion = Decimal(datos["production_cost"])
        real = Decimal(datos["real_cost"])

        assert produccion - real == Decimal(datos["firing_difference"])
        assert Decimal(datos["firing_difference"]) == Decimal(450) - Decimal(105)
        assert produccion > real

    async def test_el_espacio_sale_de_los_dias_efectivos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """2 dias x S/140. Por dias EFECTIVOS, nunca por vigencia de la oferta."""
        cotizacion, _ = await escenario(api, admin_csrf, dias=4)

        assert Decimal((await precio(api, cotizacion))["space_cost"]) == Decimal(560)

    async def test_sin_dias_decididos_el_espacio_no_se_inventa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Los dias son una decision humana (010D): sin ella se avisa."""
        cotizacion, _ = await escenario(api, admin_csrf, dias=None)

        datos = await precio(api, cotizacion)
        assert Decimal(datos["space_cost"]) == Decimal(0)
        assert "V2_PRICING_WORK_DAYS_NOT_SET" in datos["warnings"]

    async def test_la_administracion_se_cobra_una_vez_por_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Mil piezas cuestan administrativamente lo mismo que diez."""
        pocas, _ = await escenario(api, admin_csrf, piezas=10)
        muchas, _ = await escenario(api, admin_csrf, piezas=16)

        assert (
            Decimal((await precio(api, pocas))["administration_cost"])
            == Decimal((await precio(api, muchas))["administration_cost"])
            == Decimal(200)
        )


# ---------------------------------------------------------------------------
# El factor
# ---------------------------------------------------------------------------
class TestFactor:
    async def test_el_precio_minimo_es_el_doble_y_el_objetivo_el_triple(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        produccion = Decimal(datos["production_cost"])
        assert Decimal(datos["price_min"]) == produccion * 2
        assert Decimal(datos["price_target"]) == produccion * 3

    async def test_el_factor_multiplica_y_no_anade_un_porcentaje(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """x2 sobre el costo es el doble. «+200 %» seria el triple."""
        cotizacion, _ = await escenario(api, admin_csrf)
        await poner_precio(api, admin_csrf, cotizacion, commercial_factor="2")

        datos = await precio(api, cotizacion)
        assert Decimal(datos["negotiated_price"]) == Decimal(datos["production_cost"]) * 2

    async def test_un_factor_intermedio_se_admite(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _ = await escenario(api, admin_csrf)
        respuesta = await poner_precio(api, admin_csrf, cotizacion, commercial_factor="2.5")

        assert respuesta.status_code == 200, respuesta.text
        datos = await precio(api, cotizacion)
        assert Decimal(datos["commercial_factor"]) == Decimal("2.5")
        assert Decimal(datos["negotiated_price"]) == Decimal(datos["production_cost"]) * Decimal(
            "2.5"
        )

    async def test_por_debajo_del_suelo_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El x2 es una regla cerrada del negocio, no una preferencia."""
        cotizacion, _ = await escenario(api, admin_csrf)
        respuesta = await poner_precio(api, admin_csrf, cotizacion, commercial_factor="1.999999")

        assert respuesta.status_code == 422
        assert respuesta.json()["error"]["code"] == "V2_PRICING_FACTOR_OUT_OF_RANGE"

    async def test_por_encima_del_techo_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _ = await escenario(api, admin_csrf)
        respuesta = await poner_precio(api, admin_csrf, cotizacion, commercial_factor="3.000001")

        assert respuesta.status_code == 422

    async def test_el_factor_es_uno_por_cotizacion_y_no_por_producto(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """No hay forma de pedir un factor distinto para una linea.

        La comprobacion se hace contra el CONTRATO: el cuerpo de la linea
        rechaza el campo. Si algun dia se colara, dos productos del mismo
        documento tendrian dos precios que nadie podria explicar juntos.
        """
        cotizacion, linea = await escenario(api, admin_csrf)
        respuesta = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"commercial_factor": "2"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert respuesta.status_code == 422


# ---------------------------------------------------------------------------
# Reparto multiproducto
# ---------------------------------------------------------------------------
class TestReparto:
    async def test_lo_repartido_suma_exactamente_el_costo_de_produccion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Tres lineas iguales: un tercio no cabe exacto en ningun decimal."""
        await configurar_igv(api, admin_csrf)
        horno = await crear_horno(api, admin_csrf, "Horno reparto")
        pasta = await crear_pasta(api, admin_csrf, "Arcilla reparto")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.put(
            f"{V2}/{cotizacion}/firing",
            json={"kiln_id": horno["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )
        for nombre in ("A", "B", "C"):
            await anadir_linea(
                api,
                admin_csrf,
                cotizacion,
                product_name=nombre,
                quantity=10,
                length_cm="5",
                width_cm="5",
                height_cm="5",
                body_material_id=pasta,
                body_unit_weight="300",
            )
        await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": 3},
            headers={"X-CSRF-Token": admin_csrf},
        )

        datos = await precio(api, cotizacion)
        repartido = sum(Decimal(linea["production_cost"]) for linea in datos["lines"])
        real = sum(Decimal(linea["real_cost"]) for linea in datos["lines"])
        assert repartido == Decimal(datos["production_cost"])
        assert real == Decimal(datos["real_cost"])

    async def test_los_costos_generales_no_se_duplican(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Cada producto NO trae su propio taller.

        La administracion es una por cotizacion: si se cargara entera a cada
        linea, tres productos pagarian S/600 de administracion.
        """
        await configurar_igv(api, admin_csrf)
        horno = await crear_horno(api, admin_csrf, "Horno generales")
        pasta = await crear_pasta(api, admin_csrf, "Arcilla generales")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.put(
            f"{V2}/{cotizacion}/firing",
            json={"kiln_id": horno["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )
        for nombre in ("A", "B", "C"):
            await anadir_linea(
                api,
                admin_csrf,
                cotizacion,
                product_name=nombre,
                quantity=10,
                length_cm="5",
                width_cm="5",
                height_cm="5",
                body_material_id=pasta,
                body_unit_weight="300",
            )
        await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": 3},
            headers={"X-CSRF-Token": admin_csrf},
        )

        datos = await precio(api, cotizacion)
        generales = sum(Decimal(linea["general_cost"]) for linea in datos["lines"])
        espacios = sum(Decimal(linea["space_cost"]) for linea in datos["lines"])
        quemas = sum(Decimal(linea["firing_cost"]) for linea in datos["lines"])
        assert generales == Decimal(datos["administration_cost"])
        assert espacios == Decimal(datos["space_cost"])
        assert quemas == Decimal(datos["firing_commercial_cost"])

    async def test_el_precio_asignado_es_el_costo_asignado_por_el_factor(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El ejemplo aprobado: 70 % y 30 % del costo son 70 % y 30 % del precio."""
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        factor = Decimal(datos["commercial_factor"])
        for linea in datos["lines"]:
            assert Decimal(linea["line_price"]) == Decimal(linea["production_cost"]) * factor


# ---------------------------------------------------------------------------
# Redondeo y reconstruccion
# ---------------------------------------------------------------------------
class TestRedondeo:
    async def test_el_unitario_es_multiplo_del_escalon_y_nunca_baja(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Redondear al mas cercano regalaria medio escalon en cada pieza."""
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        paso = Decimal(datos["rounding_step"])
        for linea in datos["lines"]:
            unitario = Decimal(linea["unit_price"])
            crudo = Decimal(linea["unit_price_raw"])
            assert unitario % paso == 0
            assert unitario >= crudo
            assert unitario - crudo < paso

    async def test_el_subtotal_se_reconstruye_desde_los_unitarios(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Quien sume el documento a mano tiene que llegar al mismo total."""
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        for linea in datos["lines"]:
            assert Decimal(linea["line_subtotal"]) == Decimal(linea["unit_price"]) * Decimal(
                linea["quantity"]
            )
        assert sum(Decimal(linea["line_subtotal"]) for linea in datos["lines"]) == Decimal(
            datos["subtotal"]
        )

    async def test_el_ajuste_por_redondeo_explica_la_diferencia(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sin este numero nadie sabe por que el subtotal no es costo x factor."""
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        assert Decimal(datos["subtotal"]) - Decimal(datos["negotiated_price"]) == Decimal(
            datos["rounding_adjustment"]
        )

    async def test_una_linea_sin_piezas_no_lleva_importe(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _ = await escenario(api, admin_csrf, piezas=0)

        datos = await precio(api, cotizacion)
        assert Decimal(datos["lines"][0]["line_subtotal"]) == Decimal(0)
        assert "V2_PRICING_LINE_WITHOUT_QUANTITY" in datos["warnings"]


# ---------------------------------------------------------------------------
# IGV
# ---------------------------------------------------------------------------
class TestIgv:
    async def test_el_igv_sale_del_porcentaje_congelado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        porcentaje = Decimal(datos["tax_percent"])
        assert porcentaje == Decimal(18)
        assert Decimal(datos["tax"]) == Decimal(datos["subtotal"]) * porcentaje / 100
        assert Decimal(datos["total"]) == Decimal(datos["subtotal"]) + Decimal(datos["tax"])

    async def test_el_factor_no_se_aplica_sobre_el_igv(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El impuesto no es ingreso del taller.

        Si el factor se aplicara despues del IGV, el precio negociado llevaria
        el impuesto dentro y se triplicaria con el.
        """
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        # El precio negociado sale del COSTO, que no lleva IGV por ningun lado.
        assert Decimal(datos["negotiated_price"]) == Decimal(datos["production_cost"]) * Decimal(
            datos["commercial_factor"]
        )
        # Y el IGV se calcula sobre el subtotal, que es posterior.
        assert Decimal(datos["tax"]) < Decimal(datos["subtotal"])

    async def test_el_igv_no_cuenta_como_ganancia(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Contarlo inflaria el margen con dinero que se recauda para otro."""
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        assert Decimal(datos["estimated_profit"]) == Decimal(datos["subtotal"]) - Decimal(
            datos["real_cost"]
        )
        inflada = Decimal(datos["total"]) - Decimal(datos["real_cost"])
        assert inflada - Decimal(datos["estimated_profit"]) == Decimal(datos["tax"])


# ---------------------------------------------------------------------------
# Moneda
# ---------------------------------------------------------------------------
class TestMoneda:
    async def test_en_moneda_base_no_hay_tipo_de_cambio(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un 1 guardado ahi seria un tipo de cambio inventado (010B)."""
        cotizacion, _ = await escenario(api, admin_csrf)

        datos = await precio(api, cotizacion)
        assert datos["currency_code"] == "PEN"
        assert datos["exchange_rate"] is None

    async def test_en_dolares_se_convierte_una_sola_vez(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El costo se calcula en soles y solo el unitario se convierte.

        Convertir dos veces dividiria por el tipo de cambio al cuadrado, y el
        resultado seguiria pareciendo un precio.
        """
        await configurar_igv(api, admin_csrf)
        horno = await crear_horno(api, admin_csrf, "Horno USD")
        pasta = await crear_pasta(api, admin_csrf, "Arcilla USD")
        cotizacion = await crear_cotizacion(
            api, admin_csrf, currency_code="USD", exchange_rate="3.5"
        )
        await api.put(
            f"{V2}/{cotizacion}/firing",
            json={"kiln_id": horno["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )
        await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            product_name="Pieza USD",
            quantity=10,
            length_cm="10",
            width_cm="10",
            height_cm="10",
            body_material_id=pasta,
            body_unit_weight="500",
        )
        await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": 2},
            headers={"X-CSRF-Token": admin_csrf},
        )

        datos = await precio(api, cotizacion)
        assert datos["currency_code"] == "USD"
        assert Decimal(datos["exchange_rate"]) == Decimal("3.5")
        linea = datos["lines"][0]
        # El precio de la linea sigue en soles; el unitario ya esta en dolares.
        esperado = Decimal(linea["line_price"]) / Decimal(10) / Decimal("3.5")
        assert Decimal(linea["unit_price_raw"]) == esperado

    async def test_la_ganancia_en_dolares_se_compara_en_soles(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El costo esta en soles: restarle un subtotal en dolares seria absurdo."""
        await configurar_igv(api, admin_csrf)
        horno = await crear_horno(api, admin_csrf, "Horno ganancia USD")
        pasta = await crear_pasta(api, admin_csrf, "Arcilla ganancia USD")
        cotizacion = await crear_cotizacion(
            api, admin_csrf, currency_code="USD", exchange_rate="3.5"
        )
        await api.put(
            f"{V2}/{cotizacion}/firing",
            json={"kiln_id": horno["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )
        await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            product_name="Pieza",
            quantity=10,
            length_cm="10",
            width_cm="10",
            height_cm="10",
            body_material_id=pasta,
            body_unit_weight="500",
        )
        await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": 2},
            headers={"X-CSRF-Token": admin_csrf},
        )

        datos = await precio(api, cotizacion)
        esperada = Decimal(datos["subtotal"]) * Decimal("3.5") - Decimal(datos["real_cost"])
        assert Decimal(datos["estimated_profit"]) == esperada


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------
class TestSnapshot:
    async def test_una_emitida_no_admite_cambiar_el_factor(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        cotizacion, _ = await escenario(api, admin_csrf)
        await db_session.execute(
            text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": cotizacion},
        )
        await db_session.commit()

        respuesta = await poner_precio(api, admin_csrf, cotizacion, commercial_factor="2")

        assert respuesta.status_code == 409
        assert respuesta.json()["error"]["code"] == "V2_PRICING_QUOTATION_NOT_EDITABLE"

    async def test_una_emitida_no_cambia_de_precio_aunque_cambie_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Subir el costo del espacio no reescribe una oferta ya entregada."""
        cotizacion, _ = await escenario(api, admin_csrf)
        antes = await precio(api, cotizacion)

        await db_session.execute(
            text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": cotizacion},
        )
        await db_session.execute(
            text("UPDATE v2_commercial_settings SET space_service_cost_per_day = 500")
        )
        await db_session.commit()

        despues = await precio(api, cotizacion)
        assert Decimal(despues["space_cost"]) == Decimal(antes["space_cost"])
        assert Decimal(despues["total"]) == Decimal(antes["total"])

    async def test_el_borrador_si_se_actualiza_al_cambiar_una_linea(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un precio que no se recalcula al anadir una pieza es de otra cotizacion."""
        cotizacion, _ = await escenario(api, admin_csrf)
        antes = Decimal((await precio(api, cotizacion))["subtotal"])

        pasta = await crear_pasta(api, admin_csrf, "Arcilla extra")
        await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            product_name="Otra pieza",
            quantity=5,
            length_cm="8",
            width_cm="8",
            height_cm="8",
            body_material_id=pasta,
            body_unit_weight="400",
        )

        assert Decimal((await precio(api, cotizacion))["subtotal"]) > antes

    async def test_el_precio_queda_guardado_y_no_solo_devuelto(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Una emitida tiene que explicarse sin consultar ningun maestro."""
        cotizacion, _ = await escenario(api, admin_csrf)
        await poner_precio(api, admin_csrf, cotizacion, commercial_factor="2.5")

        fila = (
            await db_session.execute(
                text(
                    "SELECT production_cost_total, real_cost_total, negotiated_price,"
                    "       subtotal_amount, tax_amount, total_amount"
                    " FROM v2_quotations WHERE id = :id"
                ),
                {"id": cotizacion},
            )
        ).one()
        assert fila.production_cost_total > 0
        assert fila.real_cost_total > 0
        assert fila.negotiated_price == fila.production_cost_total * Decimal("2.5")
        assert fila.total_amount == fila.subtotal_amount + fila.tax_amount


# ---------------------------------------------------------------------------
# Legacy sigue intacto
# ---------------------------------------------------------------------------
class TestLegacy:
    async def test_poner_precio_no_toca_las_cotizaciones_de_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        antes = await db_session.scalar(text("SELECT count(*) FROM quotations"))
        cotizacion, _ = await escenario(api, admin_csrf)
        await poner_precio(api, admin_csrf, cotizacion, commercial_factor="2.5")

        assert await db_session.scalar(text("SELECT count(*) FROM quotations")) == antes

    async def test_poner_precio_no_consume_existencia(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Cotizar no mueve el almacen, y ponerle precio tampoco."""
        antes = await db_session.scalar(text("SELECT count(*) FROM stock_movements"))
        cotizacion, _ = await escenario(api, admin_csrf)
        await poner_precio(api, admin_csrf, cotizacion, commercial_factor="2.5")

        assert await db_session.scalar(text("SELECT count(*) FROM stock_movements")) == antes

    async def test_poner_precio_no_cambia_la_configuracion_de_la_casa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El precio de UNA cotizacion no puede mover el default del taller."""
        antes = (await api.get(SETTINGS)).json()["settings"]["commercial_factor_default"]
        cotizacion, _ = await escenario(api, admin_csrf)
        await poner_precio(api, admin_csrf, cotizacion, commercial_factor="2")

        despues = (await api.get(SETTINGS)).json()["settings"]["commercial_factor_default"]
        assert Decimal(despues) == Decimal(antes)
