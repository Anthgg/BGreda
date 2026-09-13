"""Fase 010G — el caso del Excel, recorrido por la API que usa el flujo.

Las pruebas del Excel de 010F son de aritmetica pura: alimentan las funciones
del motor con los numeros de la hoja y comparan. Esta es distinta y por eso
existe: **arma la cotizacion pasando por las mismas rutas que el asistente de
siete pasos**, en el mismo orden en que una persona las recorre, y comprueba
que lo que sale es lo que el Excel aprobo.

La diferencia importa. Entre la funcion que multiplica y la pantalla que la
usa hay snapshots que se congelan, recalculos que se disparan, semantica de
PATCH y una cabecera que puede cambiar a mitad. Cada uno de esos pasos es un
sitio donde el numero correcto puede perderse, y ninguno lo ve una prueba de
aritmetica.

## La unica divergencia, y es la de 010D

El Excel carga los S/44 de ilustracion enteros sobre «Plato palta». El sistema
la reparte entre los tres productos, porque la ilustracion es UNA por
cotizacion y no una tecnica mas. Los totales coinciden; lo que cambia es a que
linea se le carga cada parte. Por eso aqui se comparan los TOTALES, que es
donde las dos formas de repartir tienen que dar lo mismo.

## Por que los importes se fuerzan con acuerdos

Los costos de material y de mano de obra del Excel son el resultado de pesos,
tarifas y rendimientos que no estan en la hoja. Reconstruirlos adivinando
seria inventar datos; lo que se hace es fijar el resultado con los acuerdos
que la propia cotizacion admite —`body_cost_per_unit_override`,
`hourly_rate_override`, `final_hours_override`—, que es exactamente el
mecanismo que el taller usa cuando pacta un precio.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from tests.fixtures.excel_v2_modelo import EXCEL_TOLERANCE, LINES, TOTALS

V2 = "/api/v1/quotations-v2"
KILNS = "/api/v1/kilns"
V2_SETTINGS = "/api/v1/quoter-v2/settings"
COMMERCIAL = "/api/v1/settings/commercial"
WORKERS = "/api/v1/quoter-v2/workers"
TECHNIQUES = "/api/v1/quoter-v2/techniques"

#: Horno chico del Excel. 26.010 cm3 dentro de el son dos hornadas, y de ahi
#: salen los S/900 de tarifa y los S/210 de gas.
CAPACIDAD = 17000

#: La ilustracion del caso: S/44. Se llega con cuatro horas a S/11, que es un
#: numero redondo y deja la prueba legible.
ILUSTRACION_HORAS = Decimal(4)
ILUSTRACION_TARIFA = Decimal(11)


#: Lo que se pierde al guardar una cifra que no termina.
#:
#: Las horas del Excel son 13,8666... y la columna del sistema tiene seis
#: decimales, asi que se guardan como 13,866667. Multiplicadas por la tarifa,
#: el costo se separa del Excel en unas millonesimas. No es un error de
#: calculo: es la precision de la columna, y redondear la hora al microsegundo
#: es mas resolucion de la que un taller puede planificar.
#:
#: El margen se deja en una diezmilesima de sol porque el factor comercial
#: multiplica esa diferencia por tres. Sigue siendo cien veces mas fino que el
#: centimo, que es la unidad mas pequena que alguien puede llegar a cobrar.
QUANTIZE_TOLERANCE = Decimal("0.0001")


def cerca(
    valor: Any, esperado: Decimal, etiqueta: str, tolerancia: Decimal = EXCEL_TOLERANCE
) -> None:
    """Compara contra el Excel con la tolerancia acordada en 010F.

    El Excel calcula en coma flotante de doble precision y el sistema en
    `Decimal`: lo que se compara es el mismo numero, no la misma
    representacion.
    """
    real = Decimal(str(valor))
    assert abs(real - esperado) <= tolerancia, (
        f"{etiqueta}: el sistema dice {real} y el Excel {esperado}"
    )


async def preparar_configuracion(api: httpx.AsyncClient, csrf: str) -> int:
    """Deja la casa con los numeros de la hoja «Configuracion»."""
    vigente = (await api.get(COMMERCIAL)).json()
    igv = await api.put(
        COMMERCIAL,
        json={
            # `version`, no `expected_version`: la concurrencia optimista de los
            # maestros nombra asi el campo que viaja de ida.
            "version": vigente["version"],
            "tax_percent": "18",
            "rounding_step": "0.5",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert igv.status_code == 200, igv.text

    horno = await api.post(
        KILNS,
        json={"name": "Horno chico Excel", "capacity_volume_cm3": str(CAPACIDAD)},
        headers={"X-CSRF-Token": csrf},
    )
    assert horno.status_code == 201, horno.text
    kiln_id = int(horno.json()["id"])
    for indice, tipo in enumerate(("LOW", "HIGH")):
        tarifa = await api.put(
            f"{V2_SETTINGS}/kiln-rates/{kiln_id}/{tipo}",
            json={
                "gas_cost": ("35", "70")[indice],
                "external_rate": ("200", "250")[indice],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert tarifa.status_code == 200, tarifa.text

    actual = (await api.get(V2_SETTINGS)).json()["settings"]
    ajustes = await api.put(
        V2_SETTINGS,
        json={
            "expected_version": actual["version"],
            "workday_hours": "8",
            "space_service_cost_per_day": "140",
            "administrative_cost_per_quote": "200",
            "commercial_factor_min": "2",
            "commercial_factor_default": "3",
            "commercial_factor_max": "3",
            "retail_kiln_id": kiln_id,
            "illustration_daily_rate": "88",
            "illustration_pieces_per_workday": "8",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert ajustes.status_code == 200, ajustes.text
    return kiln_id


async def preparar_maestros(api: httpx.AsyncClient, csrf: str) -> tuple[int, int, int]:
    """Una pasta, un trabajador y una tecnica: los minimos para cotizar."""
    categoria = await api.post(
        "/api/v1/categories",
        json={"name": "Cat Excel 010G", "parent_id": None},
        headers={"X-CSRF-Token": csrf},
    )
    assert categoria.status_code == 201, categoria.text
    pasta = await api.post(
        "/api/v1/products",
        json={
            "name": "Pasta Excel 010G",
            "product_type": "RAW_MATERIAL",
            "product_category_id": int(categoria.json()["id"]),
            "base_uom_code": "g",
            "purchasable": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert pasta.status_code == 201, pasta.text
    pasta_id = int(pasta.json()["id"])
    # El costo del maestro da igual: cada linea lo pisa con un acuerdo propio
    # para llegar al importe que el Excel declara.
    alta = await api.put(
        f"/api/v1/quoter-v2/materials/{pasta_id}",
        json={
            "material_kind": "BODY",
            "origin": "PURCHASE",
            "purchase_quantity": "100000",
            "purchase_cost": "100",
            "transport_cost": "30",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert alta.status_code == 200, alta.text

    trabajador = await api.post(
        WORKERS,
        json={"name": "Alfarero Excel", "worker_type": "INTERNAL", "daily_rate": "120"},
        headers={"X-CSRF-Token": csrf},
    )
    assert trabajador.status_code == 201, trabajador.text

    tecnica = await api.post(
        TECHNIQUES,
        json={
            "code": "TORNO-EXCEL-010G",
            "name": "Torno Excel",
            "default_capacity_per_workday": "50",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert tecnica.status_code == 201, tecnica.text

    return pasta_id, int(trabajador.json()["id"]), int(tecnica.json()["id"])


class TestElCasoDelExcelPorLaApi:
    async def test_los_totales_son_los_de_la_hoja_aprobada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El recorrido entero: de crear la cotizacion al total con IGV.

        Es el mismo orden de los siete pasos: cliente y cabecera, piezas,
        materiales, mano de obra, quema y precio.
        """
        await preparar_configuracion(api, admin_csrf)
        pasta_id, worker_id, technique_id = await preparar_maestros(api, admin_csrf)

        # --- paso 1: la cotizacion -------------------------------------
        creada = await api.post(
            V2,
            json={"name": "Caso Excel 010G", "production_type": "RETAIL"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert creada.status_code == 201, creada.text
        cotizacion = int(creada.json()["id"])

        # --- pasos 2 y 3: piezas, medidas y materiales -----------------
        #
        # Las medidas salen del volumen que el Excel declara: se reparte en
        # tres aristas cuyo producto es ese volumen y cuya division entre la
        # cantidad da el volumen unitario. Lo que importa para el reparto de
        # la quema es el volumen TOTAL, que es lo que se fija aqui.
        medidas = {
            "Plato palta": ("18", "12", "3"),  # 648 x 20 = 12.960
            "Tasa Buho": ("5", "3", "3"),  # 45 x 50 = 2.250
            "PLATOS HONDOS CHICOS": ("30", "15", "2"),  # 900 x 12 = 10.800
        }
        lineas: dict[str, int] = {}
        for linea in LINES:
            nombre = str(linea["name"])
            largo, ancho, alto = medidas[nombre]
            # El costo de material se fija por acuerdo de esta cotizacion: un
            # gramo que cuesta lo que haga falta para que la linea valga lo
            # que el Excel dice. Reconstruirlo con pesos y tarifas inventados
            # seria fabricar datos que la hoja no tiene.
            por_pieza = Decimal(str(linea["materials_cost"])) / Decimal(str(linea["quantity"]))
            alta = await api.post(
                f"{V2}/{cotizacion}/products",
                json={
                    "product_name": nombre,
                    "quantity": linea["quantity"],
                    "length_cm": largo,
                    "width_cm": ancho,
                    "height_cm": alto,
                    "body_material_id": pasta_id,
                    "body_unit_weight": "1",
                    "body_cost_per_unit_override": str(por_pieza),
                },
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert alta.status_code == 201, alta.text
            lineas[nombre] = int(alta.json()["id"])

        productos = (await api.get(f"{V2}/{cotizacion}/products")).json()
        cerca(productos["materials_cost"], TOTALS["materials"], "materiales")

        # --- paso 4: mano de obra e ilustracion ------------------------
        for linea in LINES:
            nombre = str(linea["name"])
            horas = Decimal(str(linea["labor_hours"]))
            costo = Decimal(str(linea["labor_cost"]))
            tarea = await api.post(
                f"{V2}/{cotizacion}/labor",
                json={
                    "v2_quotation_product_id": lineas[nombre],
                    "worker_id": worker_id,
                    "technique_id": technique_id,
                    "quantity": str(linea["quantity"]),
                    "final_hours_override": str(horas),
                    "hourly_rate_override": str(costo / horas),
                },
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert tarea.status_code == 201, tarea.text

        ilustracion = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={
                "illustration_enabled": True,
                "illustration_quantity": str(ILUSTRACION_HORAS),
                "illustration_hourly_rate_override": str(ILUSTRACION_TARIFA),
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert ilustracion.status_code == 200, ilustracion.text
        cerca(ilustracion.json()["cost"], TOTALS["illustration"], "ilustracion")

        # Los dias efectivos son una DECISION: sin ellos el espacio no entra.
        planificacion = await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": 4},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert planificacion.status_code == 200, planificacion.text

        mano_de_obra = (await api.get(f"{V2}/{cotizacion}/labor")).json()
        cerca(
            mano_de_obra["labor_cost"],
            TOTALS["labor"],
            "mano de obra",
            QUANTIZE_TOLERANCE,
        )

        # --- paso 5: la quema ------------------------------------------
        quema = (await api.get(f"{V2}/{cotizacion}/firing")).json()
        assert quema["firing_count"] == 2, "26.010 cm3 en un horno de 17.000 son dos hornadas"
        cerca(quema["commercial_total"], TOTALS["firing_commercial"], "tarifa de quema")
        cerca(quema["gas_total"], TOTALS["firing_gas"], "gas real")
        cerca(quema["difference"], TOTALS["firing_difference"], "diferencia de quema")

        # --- paso 6: el precio -----------------------------------------
        precio = (await api.get(f"{V2}/{cotizacion}/pricing")).json()

        cerca(precio["space_cost"], TOTALS["space"], "espacio")
        cerca(precio["administration_cost"], TOTALS["administration"], "administracion")
        cerca(
            precio["production_cost"],
            TOTALS["production_cost"],
            "costo de produccion",
            QUANTIZE_TOLERANCE,
        )
        cerca(precio["real_cost"], TOTALS["real_cost"], "costo real", QUANTIZE_TOLERANCE)
        cerca(precio["price_min"], TOTALS["price_min_x2"], "precio minimo x2", QUANTIZE_TOLERANCE)
        cerca(
            precio["price_target"],
            TOTALS["price_target_x3"],
            "precio objetivo x3",
            QUANTIZE_TOLERANCE,
        )
        # El subtotal del documento NO es el del Excel, y no es un fallo.
        #
        # Los dos parten del mismo precio sin redondear —S/7675,28, que si
        # coincide— y los dos redondean cada unitario hacia arriba al escalon
        # de S/0,50. Lo que cambia es DONDE esta la ilustracion: el Excel carga
        # sus S/44 enteros sobre «Plato palta» y el sistema los reparte entre
        # los tres, porque la ilustracion es una por cotizacion (regla de
        # 010D). Mover costo entre lineas cambia en que lado del escalon cae
        # cada unitario, y la suma de tres redondeos hacia arriba se mueve unos
        # soles.
        #
        # Lo que si tiene que cumplirse es que el documento cuadre consigo
        # mismo, y eso es lo que se comprueba.
        subtotal = Decimal(str(precio["subtotal"]))
        impuesto = Decimal(str(precio["tax"]))
        total = Decimal(str(precio["total"]))
        assert subtotal + impuesto == total, "el documento tiene que cuadrar al sumarlo a mano"
        cerca(impuesto, subtotal * Decimal("0.18"), "el IGV es el 18 % del subtotal")

        # Y el orden de magnitud sigue siendo el del Excel: la diferencia es la
        # de tres redondeos, no la de otro precio.
        assert abs(subtotal - TOTALS["subtotal"]) <= Decimal(5), (
            f"el subtotal se ha ido del Excel mas de lo que explican tres redondeos: "
            f"{subtotal} contra {TOTALS['subtotal']}"
        )
        # La ganancia arrastra la misma diferencia de redondeo que el subtotal,
        # porque es subtotal menos costo real. Se comprueba que sea justo eso.
        ganancia = Decimal(str(precio["estimated_profit"]))
        cerca(
            ganancia,
            subtotal - Decimal(str(precio["real_cost"])),
            "la ganancia es el subtotal menos el costo real",
            QUANTIZE_TOLERANCE,
        )
        assert abs(ganancia - TOTALS["estimated_profit"]) <= Decimal(5)

    async def test_el_reparto_por_linea_cuadra_con_el_total(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Lo que cada producto cuesta, sumado, es lo que cuesta la cotizacion.

        Los precios unitarios POR LINEA no son los del Excel, y no es un fallo:
        el Excel carga los S/44 de ilustracion enteros sobre «Plato palta» y el
        sistema los reparte entre los tres productos, porque la ilustracion es
        UNA por cotizacion. Esa es la regla de 010D y manda sobre la hoja.

        Lo que si tiene que cumplirse, y es lo que se comprueba aqui:

        - cada unitario es un multiplo del escalon comercial de S/0,50. Un
          precio de S/181,58 no se puede cobrar en un mostrador;
        - la suma de los subtotales de linea es EXACTAMENTE el subtotal de la
          cotizacion. Sin esto el documento no cuadra consigo mismo;
        - «Plato palta» sale mas barato que en el Excel, que es la direccion
          que la regla de 010D predice: ya no carga sola con la ilustracion.
        """
        await preparar_configuracion(api, admin_csrf)
        pasta_id, worker_id, technique_id = await preparar_maestros(api, admin_csrf)

        creada = await api.post(
            V2,
            json={"name": "Unitarios Excel 010G", "production_type": "RETAIL"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert creada.status_code == 201, creada.text
        cotizacion = int(creada.json()["id"])

        medidas = {
            "Plato palta": ("18", "12", "3"),
            "Tasa Buho": ("5", "3", "3"),
            "PLATOS HONDOS CHICOS": ("30", "15", "2"),
        }
        for linea in LINES:
            nombre = str(linea["name"])
            largo, ancho, alto = medidas[nombre]
            por_pieza = Decimal(str(linea["materials_cost"])) / Decimal(str(linea["quantity"]))
            alta = await api.post(
                f"{V2}/{cotizacion}/products",
                json={
                    "product_name": nombre,
                    "quantity": linea["quantity"],
                    "length_cm": largo,
                    "width_cm": ancho,
                    "height_cm": alto,
                    "body_material_id": pasta_id,
                    "body_unit_weight": "1",
                    "body_cost_per_unit_override": str(por_pieza),
                },
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert alta.status_code == 201, alta.text
            horas = Decimal(str(linea["labor_hours"]))
            costo = Decimal(str(linea["labor_cost"]))
            tarea = await api.post(
                f"{V2}/{cotizacion}/labor",
                json={
                    "v2_quotation_product_id": int(alta.json()["id"]),
                    "worker_id": worker_id,
                    "technique_id": technique_id,
                    "quantity": str(linea["quantity"]),
                    "final_hours_override": str(horas),
                    "hourly_rate_override": str(costo / horas),
                },
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert tarea.status_code == 201, tarea.text

        ilustracion = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={
                "illustration_enabled": True,
                "illustration_quantity": str(ILUSTRACION_HORAS),
                "illustration_hourly_rate_override": str(ILUSTRACION_TARIFA),
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert ilustracion.status_code == 200, ilustracion.text
        await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": 4},
            headers={"X-CSRF-Token": admin_csrf},
        )

        precio = (await api.get(f"{V2}/{cotizacion}/pricing")).json()
        por_nombre = {fila["product_name"]: fila for fila in precio["lines"]}
        assert len(por_nombre) == 3

        escalon = Decimal("0.5")
        suma = Decimal(0)
        for linea in LINES:
            fila = por_nombre[str(linea["name"])]
            unitario = Decimal(str(fila["unit_price"]))
            assert unitario % escalon == 0, (
                f"{linea['name']}: {unitario} no es un multiplo de S/0,50"
            )
            # El subtotal de la linea es el unitario COMERCIAL por la cantidad,
            # no el precio sin redondear: es lo que el cliente va a pagar.
            esperado = unitario * Decimal(str(linea["quantity"]))
            cerca(fila["line_subtotal"], esperado, f"subtotal {linea['name']}")
            suma += Decimal(str(fila["line_subtotal"]))

        assert suma == Decimal(str(precio["subtotal"])), (
            "la suma de las lineas tiene que ser el subtotal, sin un centimo de diferencia"
        )
        # No se compara con el subtotal del Excel: reparte la ilustracion de
        # otra forma y por eso sus redondeos caen en otro sitio. Lo que se
        # exige es que el documento cuadre consigo mismo, arriba y abajo.
        assert abs(suma - TOTALS["subtotal"]) <= Decimal(5), (
            "la diferencia con el Excel tiene que seguir siendo la de unos redondeos"
        )

        # La direccion que 010D predice: el producto que en el Excel cargaba
        # con toda la ilustracion aqui la comparte, y por eso sale mas barato.
        plato = por_nombre["Plato palta"]
        assert Decimal(str(plato["unit_price"])) < Decimal(str(LINES[0]["unit_price"])), (
            "repartir la ilustracion tiene que abaratar la linea que antes la llevaba entera"
        )
