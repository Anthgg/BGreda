"""Fase 010K — Solo Quema V2 contra PostgreSQL real, por sus rutas.

El caso de la hoja «Solo Quema» del Excel final, armado por la API:

    100 piezas de 8x8x8 cm, separacion 3, externo, baja + alta, COMPARTIDA,
    horno Chico, factor x1, sin vidriado
    -> 133 100 cm3, 782,941176 %, 8 hornadas fisicas
    -> quema 3523,235294, gas 822,088235
    -> SUBTOTAL 3523,50, IGV 634,23, TOTAL 4157,73, ganancia 2701,411765

Y lo que la fase exige comprobar: los dos modos, los dos hornos, los ciclos por
separado, el factor de x1 a x2, multiproducto, separacion, vidriado, redondeo,
snapshots, duplicacion, RBAC y un PDF sin datos internos.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

FQ = "/api/v1/firing-quotations-v2"
KILNS = "/api/v1/kilns"
V2_SETTINGS = "/api/v1/quoter-v2/settings"
COMMERCIAL = "/api/v1/settings/commercial"


def h(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf}


def dec(valor: Any) -> Decimal:
    return Decimal(str(valor))


async def _post(api: httpx.AsyncClient, csrf: str, url: str, datos: Any = None) -> Any:
    respuesta = await api.post(url, json=datos if datos is not None else {}, headers=h(csrf))
    assert respuesta.status_code in (200, 201), respuesta.text
    return respuesta.json()


async def _put(api: httpx.AsyncClient, csrf: str, url: str, datos: Any) -> Any:
    respuesta = await api.put(url, json=datos, headers=h(csrf))
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


async def preparar(api: httpx.AsyncClient, csrf: str) -> dict[str, int]:
    """Hornos con tarifas, configuracion, un esmalte y un cliente: la casa del Excel."""
    vigente = (await api.get(COMMERCIAL)).json()
    await _put(
        api,
        csrf,
        COMMERCIAL,
        {"version": vigente["version"], "tax_percent": "18", "rounding_step": "0.5"},
    )
    hornos: dict[str, int] = {}
    for nombre, capacidad, gas, externo, alumno in (
        ("Chico", "17000", ("35", "70"), ("200", "250"), ("90", "180")),
        ("Grande", "200000", ("55", "110"), ("700", "1200"), ("1000", "2000")),
    ):
        horno = await _post(
            api, csrf, KILNS, {"name": f"Horno {nombre} 010K", "capacity_volume_cm3": capacidad}
        )
        hornos[nombre] = int(horno["id"])
        for indice, tipo in enumerate(("LOW", "HIGH")):
            await _put(
                api,
                csrf,
                f"{V2_SETTINGS}/kiln-rates/{hornos[nombre]}/{tipo}",
                {
                    "gas_cost": gas[indice],
                    "external_rate": externo[indice],
                    "student_rate": alumno[indice],
                },
            )
    ajustes = (await api.get(V2_SETTINGS)).json()["settings"]
    await _put(
        api,
        csrf,
        V2_SETTINGS,
        {
            "expected_version": ajustes["version"],
            "retail_kiln_id": hornos["Chico"],
            "wholesale_kiln_id": hornos["Grande"],
            "piece_separation_cm": "3",
            "quotation_validity_days": 20,
            "default_customer_kind": "EXTERNAL",
        },
    )
    categoria = await _post(
        api, csrf, "/api/v1/categories", {"name": "Cat 010K", "parent_id": None}
    )
    esmalte = await _post(
        api,
        csrf,
        "/api/v1/products",
        {
            "name": "Esmalte premium 010K",
            "product_type": "RAW_MATERIAL",
            "product_category_id": int(categoria["id"]),
            "base_uom_code": "g",
            "purchasable": True,
        },
    )
    await _put(
        api,
        csrf,
        f"/api/v1/quoter-v2/materials/{int(esmalte['id'])}",
        {
            "material_kind": "GLAZE",
            "origin": "PURCHASE",
            "purchase_quantity": "1000",
            "purchase_cost": "120",
            "transport_cost": "0",
        },
    )
    cliente = await _post(
        api, csrf, "/api/v1/partners", {"name": "Cliente Solo Quema", "role": "CLIENT"}
    )
    return {
        "chico": hornos["Chico"],
        "grande": hornos["Grande"],
        "esmalte": int(esmalte["id"]),
        "cliente": int(cliente["id"]),
    }


async def cotizacion_del_excel(
    api: httpx.AsyncClient, csrf: str, ids: dict[str, int], *, piezas: int = 100
) -> dict[str, Any]:
    """El caso de la hoja: una linea de `piezas` cubos de 8 cm."""
    creada = await _post(api, csrf, FQ, {"name": "Solo Quema 010K", "customer_id": ids["cliente"]})
    await _post(
        api,
        csrf,
        f"{FQ}/{creada['id']}/lines",
        {
            "product_name": "Pieza demo",
            "quantity": piezas,
            "length_cm": "8",
            "width_cm": "8",
            "height_cm": "8",
        },
    )
    respuesta = await api.get(f"{FQ}/{creada['id']}")
    assert respuesta.status_code == 200, respuesta.text
    return dict(respuesta.json())


class TestCasoDelExcel:
    async def test_el_caso_de_la_hoja_sale_al_centimo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)

        assert ctz["code"].startswith("Q-V2-")
        assert ctz["firing_mode"] == "SHARED"
        assert dec(ctz["piece_separation_cm"]) == Decimal(3)
        assert dec(ctz["factor"]) == Decimal(1)
        assert ctz["kiln_id"] == ids["chico"], "por menor nace en el horno chico"
        assert dec(ctz["total_volume_cm3"]) == Decimal(133100)
        assert dec(ctz["occupancy_percent"]) == Decimal("782.941176")
        assert ctz["firing_count"] == 8
        assert dec(ctz["billed_load"]) == Decimal("7.829411764706")
        assert dec(ctz["firing_commercial_total"]) == Decimal("3523.235294")
        assert dec(ctz["firing_gas_total"]) == Decimal("822.088235")
        assert dec(ctz["subtotal_amount"]) == Decimal("3523.5")
        assert dec(ctz["tax_amount"]) == Decimal("634.23")
        assert dec(ctz["total_amount"]) == Decimal("4157.73")
        assert dec(ctz["real_cost_total"]) == Decimal("822.088235")
        assert dec(ctz["estimated_profit"]) == Decimal("2701.411765")
        # La ultima hornada va al 82,94 %: informacion de operacion.
        assert dec(ctz["batch_loads"][-1]) == Decimal("82.941176")

    async def test_compara_los_dos_hornos_y_sugiere_sin_cambiar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)

        por_horno = {horno["kiln_id"]: horno for horno in ctz["kilns"]}
        grande = por_horno[ids["grande"]]
        assert dec(grande["occupancy_percent"]) == Decimal("66.55")
        assert grande["firing_count"] == 1
        assert dec(grande["shared"]["commercial"]) == Decimal("1264.45")
        assert dec(grande["shared"]["gas"]) == Decimal("109.8075")
        # Los dos modos viajan siempre, para poder compararlos.
        assert dec(grande["exclusive"]["commercial"]) == Decimal(1900)
        chico = por_horno[ids["chico"]]
        assert chico["selected"] is True
        assert dec(chico["exclusive"]["commercial"]) == Decimal(3600)

        assert ctz["suggestion"]["kiln_id"] == ids["grande"]
        assert dec(ctz["suggestion"]["savings"]) == Decimal("2258.785294")
        # Sugerir no es cambiar: el horno elegido sigue siendo el chico.
        assert ctz["kiln_id"] == ids["chico"]

    async def test_exclusiva_cobra_hornadas_enteras_y_avisa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        exclusiva = await _put(api, admin_csrf, f"{FQ}/{ctz['id']}", {"firing_mode": "EXCLUSIVE"})
        assert dec(exclusiva["billed_load"]) == Decimal(8)
        assert dec(exclusiva["firing_commercial_total"]) == Decimal(3600)
        assert dec(exclusiva["firing_gas_total"]) == Decimal(840)
        assert dec(exclusiva["subtotal_amount"]) == Decimal(3600)
        assert "V2_FQ_EXCLUSIVE_RAISES_PRICE" in exclusiva["warnings"]

        compartida = await _put(api, admin_csrf, f"{FQ}/{ctz['id']}", {"firing_mode": "SHARED"})
        assert dec(compartida["firing_commercial_total"]) == Decimal("3523.235294")

    @pytest.mark.parametrize(
        ("baja", "alta", "comercial", "gas"),
        [
            (True, False, "1565.882353", "274.029412"),
            (False, True, "1957.352941", "548.058824"),
        ],
    )
    async def test_solo_baja_y_solo_alta(
        self,
        api: httpx.AsyncClient,
        admin_csrf: str,
        baja: bool,
        alta: bool,
        comercial: str,
        gas: str,
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        actualizada = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {"low_fire_enabled": baja, "high_fire_enabled": alta},
        )
        assert dec(actualizada["firing_commercial_total"]) == Decimal(comercial)
        assert dec(actualizada["firing_gas_total"]) == Decimal(gas)

    async def test_sin_ningun_ciclo_no_hay_precio_y_se_avisa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        apagada = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {"low_fire_enabled": False, "high_fire_enabled": False},
        )
        assert dec(apagada["firing_commercial_total"]) == Decimal(0)
        assert dec(apagada["subtotal_amount"]) == Decimal(0)
        assert "V2_FQ_NO_CYCLE" in apagada["warnings"]

    async def test_el_alumno_paga_otra_tarifa_y_el_gas_es_el_mismo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        alumno = await _put(api, admin_csrf, f"{FQ}/{ctz['id']}", {"customer_kind": "STUDENT"})
        # (90 + 180) x 7,829412 = 2113,94
        assert dec(alumno["firing_commercial_total"]) == Decimal("2113.941176")
        assert dec(alumno["firing_gas_total"]) == dec(ctz["firing_gas_total"])


class TestFactor:
    @pytest.mark.parametrize(
        ("factor", "subtotal"),
        [("1", "3523.5"), ("1.10", "3876"), ("1.17", "4122.5"), ("2", "7046.5")],
    )
    async def test_factores_validos(
        self, api: httpx.AsyncClient, admin_csrf: str, factor: str, subtotal: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        actualizada = await _put(api, admin_csrf, f"{FQ}/{ctz['id']}", {"factor": factor})
        assert dec(actualizada["factor"]) == Decimal(factor)
        assert dec(actualizada["subtotal_amount"]) == Decimal(subtotal)

    @pytest.mark.parametrize("factor", ["0.99", "2.01", "0", "3"])
    async def test_factores_fuera_de_rango(
        self, api: httpx.AsyncClient, admin_csrf: str, factor: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        respuesta = await api.put(
            f"{FQ}/{ctz['id']}", json={"factor": factor}, headers=h(admin_csrf)
        )
        assert respuesta.status_code == 422, respuesta.text

    async def test_el_factor_se_aplica_una_sola_vez(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        doble = await _put(api, admin_csrf, f"{FQ}/{ctz['id']}", {"factor": "2"})
        assert dec(doble["base_amount"]) == Decimal("3523.235294")
        assert dec(doble["commercial_price"]) == Decimal("7046.470588")
        # El costo real NO lleva factor: es lo que sale del bolsillo.
        assert dec(doble["real_cost_total"]) == Decimal("822.088235")


class TestPiezas:
    async def test_multiproducto_suma_volumenes_y_reparte_participacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        creada = await _post(api, admin_csrf, FQ, {"customer_id": ids["cliente"]})
        for nombre, cantidad, medidas in (
            ("Plato palta", 20, ("18", "12", "3")),
            ("Tasa Buho", 50, ("1", "15", "3")),
            ("Platos hondos", 12, ("15", "12", "5")),
        ):
            await _post(
                api,
                admin_csrf,
                f"{FQ}/{creada['id']}/lines",
                {
                    "product_name": nombre,
                    "quantity": cantidad,
                    "length_cm": medidas[0],
                    "width_cm": medidas[1],
                    "height_cm": medidas[2],
                },
            )
        ctz = (await api.get(f"{FQ}/{creada['id']}")).json()
        volumenes = sorted(dec(linea["total_volume_cm3"]) for linea in ctz["lines"])
        assert volumenes == [Decimal(21600), Decimal(25920), Decimal(37800)]
        assert dec(ctz["total_volume_cm3"]) == Decimal(85320)
        assert sum(dec(linea["volume_share_percent"]) for linea in ctz["lines"]) == pytest.approx(
            Decimal(100), abs=Decimal("0.0001")
        )
        assert ctz["firing_count"] == 6
        assert dec(ctz["firing_commercial_total"]) == Decimal("2258.470588")

    @pytest.mark.parametrize(
        ("separacion", "volumen"), [("0", "51200"), ("3", "133100"), ("5", "219700")]
    )
    async def test_separacion(
        self, api: httpx.AsyncClient, admin_csrf: str, separacion: str, volumen: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        actualizada = await _put(
            api, admin_csrf, f"{FQ}/{ctz['id']}", {"piece_separation_cm": separacion}
        )
        assert dec(actualizada["total_volume_cm3"]) == Decimal(volumen)

    async def test_una_pieza_sin_medidas_no_ocupa_horno_y_avisa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        creada = await _post(api, admin_csrf, FQ, {"customer_id": ids["cliente"]})
        ctz = await _post(
            api,
            admin_csrf,
            f"{FQ}/{creada['id']}/lines",
            {"product_name": "Sin medir", "quantity": 5},
        )
        assert dec(ctz["total_volume_cm3"]) == Decimal(0)
        assert "V2_FQ_LINE_WITHOUT_DIMENSIONS" in ctz["warnings"]

    async def test_borrar_una_pieza_devuelve_el_volumen_a_lo_que_queda(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        linea = ctz["lines"][0]["id"]
        respuesta = await api.delete(f"{FQ}/{ctz['id']}/lines/{linea}", headers=h(admin_csrf))
        assert respuesta.status_code == 200, respuesta.text
        assert dec(respuesta.json()["total_volume_cm3"]) == Decimal(0)


class TestLimitesYTarifas:
    """Revision de Codex en el PR: tres casos que devolvian el error equivocado."""

    async def test_un_pedido_que_no_cabe_en_la_columna_se_rechaza_con_422(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un volumen imposible es una entrada invalida, no un 500.

        El volumen se guarda en NUMERIC(18, 6): doce digitos enteros. Una pieza
        de 10 m de lado por un millon de unidades pasa de largo ese techo, y
        antes el desbordamiento lo levantaba PostgreSQL al vaciar la sesion: un
        500 sin explicacion sobre una peticion que el contrato habia aceptado.
        """
        ids = await preparar(api, admin_csrf)
        creada = await _post(api, admin_csrf, FQ, {"customer_id": ids["cliente"]})
        respuesta = await api.post(
            f"{FQ}/{creada['id']}/lines",
            json={
                "product_name": "Imposible",
                "quantity": 1_000_000,
                "length_cm": "1000",
                "width_cm": "1000",
                "height_cm": "1000",
            },
            headers=h(admin_csrf),
        )
        assert respuesta.status_code == 422, respuesta.text
        assert "demasiado grande" in respuesta.text

    async def test_una_tarifa_puesta_en_cero_no_es_una_tarifa_que_falta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Una quema de cortesia con vidriado cobrado SI se emite.

        El contrato de tarifas por horno admite el cero, asi que un total de
        quema en cero puede significar dos cosas muy distintas: que no hay
        tarifas configuradas o que la casa decidio no cobrar la quema. Deducirlo
        del importe confundia una con otra y bloqueaba la emision aunque todo
        estuviera puesto y el vidriado se cobrara aparte.
        """
        ids = await preparar(api, admin_csrf)
        for tipo in ("LOW", "HIGH"):
            await _put(
                api,
                admin_csrf,
                f"{V2_SETTINGS}/kiln-rates/{ids['chico']}/{tipo}",
                {"gas_cost": "35", "external_rate": "0", "student_rate": "0"},
            )
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        ctz = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {"kiln_id": ids["chico"], "glaze_enabled": True, "glaze_grams": "500"},
        )
        assert dec(ctz["firing_commercial_total"]) == Decimal(0)
        assert dec(ctz["glaze_material_cost"]) > Decimal(0)
        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        codigos = {bloqueo["code"] for bloqueo in resumen["blockers"]}
        assert "V2_FQ_RATES_MISSING" not in codigos, resumen["blockers"]
        assert resumen["can_confirm"] is True, resumen["blockers"]

    async def test_el_factor_por_defecto_de_quema_se_lee_y_se_cambia_por_la_api(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sin esto la casa se quedaba clavada en x1,00 salvo tocando la base."""
        ids = await preparar(api, admin_csrf)
        ajustes = (await api.get(V2_SETTINGS)).json()["settings"]
        assert dec(ajustes["firing_service_factor_default"]) == Decimal("1.00")
        await _put(
            api,
            admin_csrf,
            V2_SETTINGS,
            {"expected_version": ajustes["version"], "firing_service_factor_default": "1.40"},
        )
        vigentes = (await api.get(V2_SETTINGS)).json()["settings"]
        assert dec(vigentes["firing_service_factor_default"]) == Decimal("1.40")
        # Y un servicio nuevo nace con el, no con el x1,00 de la migracion.
        creada = await _post(api, admin_csrf, FQ, {"customer_id": ids["cliente"]})
        assert dec(creada["factor"]) == Decimal("1.40")
        # El rango sigue siendo el del servicio: el x3 de fabricacion no entra.
        rechazo = await api.put(
            V2_SETTINGS,
            json={"expected_version": vigentes["version"], "firing_service_factor_default": "3"},
            headers=h(admin_csrf),
        )
        assert rechazo.status_code == 422, rechazo.text


class TestVidriado:
    async def test_apagado_no_cuesta_nada(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        assert ctz["glaze_enabled"] is False
        assert dec(ctz["glaze_material_cost"]) == Decimal(0)

    async def test_encendido_cobra_gramos_por_el_esmalte_mas_caro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        con = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {"glaze_enabled": True, "glaze_grams": "500"},
        )
        assert dec(con["glaze_cost_per_gram"]) == Decimal("0.12")
        assert con["glaze_material_name"] == "Esmalte premium 010K"
        assert dec(con["glaze_material_cost"]) == Decimal(60)
        # La base lleva el vidriado; el gas nunca.
        assert dec(con["base_amount"]) == Decimal("3583.235294")
        assert dec(con["real_cost_total"]) == Decimal("882.088235")

    async def test_costo_manual_no_toca_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        manual = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {
                "glaze_enabled": True,
                "glaze_grams": "500",
                "glaze_cost_source": "MANUAL",
                "glaze_manual_cost_per_gram": "0.30",
            },
        )
        assert manual["glaze_cost_source"] == "MANUAL"
        assert dec(manual["glaze_material_cost"]) == Decimal(150)
        assert manual["glaze_material_name"] is None
        maestros = (await api.get("/api/v1/quoter-v2/materials")).json()["items"]
        esmalte = next(m for m in maestros if m["product_id"] == ids["esmalte"])
        assert dec(esmalte["effective_cost_per_unit"]) == Decimal("0.12")

    async def test_un_costo_manual_en_cero_cae_al_maestro_y_avisa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El Excel (E10) usa el manual SOLO si es > 0. Un cero no regala el vidriado."""
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        en_cero = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {
                "glaze_enabled": True,
                "glaze_grams": "500",
                "glaze_cost_source": "MANUAL",
                "glaze_manual_cost_per_gram": "0",
            },
        )
        assert dec(en_cero["glaze_cost_per_gram"]) == Decimal("0.12"), "cae al esmalte del maestro"
        assert dec(en_cero["glaze_material_cost"]) == Decimal(60)
        assert "V2_FQ_GLAZE_MANUAL_COST_MISSING" in en_cero["warnings"]

    async def test_mano_de_obra_de_vidriado_interna_no_suma_y_externa_si(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        tecnica = await _post(
            api,
            admin_csrf,
            "/api/v1/quoter-v2/techniques",
            {
                "code": "VIDRIADO-010K",
                "name": "Vidriado 010K",
                "default_capacity_per_workday": "50",
            },
        )
        interno = await _post(
            api,
            admin_csrf,
            "/api/v1/quoter-v2/workers",
            {"name": "Taller 010K", "worker_type": "INTERNAL", "daily_rate": "220"},
        )
        externo = await _post(
            api,
            admin_csrf,
            "/api/v1/quoter-v2/workers",
            {"name": "Externo 010K", "worker_type": "EXTERNAL", "daily_rate": "220"},
        )
        con_interno = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {
                "glaze_enabled": True,
                "glaze_grams": "100",
                "glaze_labor_enabled": True,
                "glaze_labor_worker_id": int(interno["id"]),
                "glaze_labor_technique_id": int(tecnica["id"]),
                "glaze_labor_quantity": "100",
            },
        )
        # 100 piezas / 50 por jornada x 8 h = 16 h; interno no suma costo.
        assert dec(con_interno["glaze_labor_hours"]) == Decimal(16)
        assert dec(con_interno["glaze_labor_cost"]) == Decimal(0)

        con_externo = await _put(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}",
            {"glaze_labor_worker_id": int(externo["id"])},
        )
        # 16 h x (220 / 8) = 440.
        assert dec(con_externo["glaze_labor_cost"]) == Decimal(440)
        assert dec(con_externo["base_amount"]) == Decimal("3975.235294")


class TestEmision:
    async def test_borrador_resumen_emitir_y_pdf(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)

        borrador_pdf = await api.get(f"{FQ}/{ctz['id']}/pdf")
        assert borrador_pdf.status_code == 409, "un borrador no tiene documento"

        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        assert resumen["can_confirm"], resumen["blockers"]
        assert resumen["service_label"] == "Quema baja + alta"
        assert dec(resumen["total_amount"]) == Decimal("4157.73")
        # El resumen del cliente no lleva ni ocupacion ni gas.
        assert "occupancy_percent" not in resumen
        assert "firing_gas_total" not in resumen

        emitida = await _post(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        assert emitida["status"] == "CONFIRMED"
        assert emitida["valid_until"]
        assert dec(emitida["total_amount"]) == Decimal("4157.73")

        pdf = await api.get(f"{FQ}/{ctz['id']}/pdf")
        assert pdf.status_code == 200, pdf.text
        texto = "".join(
            (pagina.extract_text() or "") for pagina in PdfReader(io.BytesIO(pdf.content)).pages
        )
        plano = texto.replace(" ", "").replace(chr(10), "")
        assert "S/4,157.73" in plano
        assert "Piezademo" in plano
        for prohibido in (
            "Gasreal",
            "822.08",
            "Ocupaci",
            "782",
            "Ganancia",
            "2701",
            "Factor",
            "Costoreal",
        ):
            assert prohibido not in plano, prohibido

    async def test_el_resumen_del_cliente_no_lleva_avisos_internos(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Vender por debajo del costo es cosa del taller, no del documento."""
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        # Una tarifa por debajo del gas: el servicio se vende a perdida.
        await _put(
            api,
            admin_csrf,
            f"{V2_SETTINGS}/kiln-rates/{ids['chico']}/LOW",
            {"gas_cost": "300", "external_rate": "10", "student_rate": "10"},
        )
        await _put(
            api,
            admin_csrf,
            f"{V2_SETTINGS}/kiln-rates/{ids['chico']}/HIGH",
            {"gas_cost": "300", "external_rate": "10", "student_rate": "10"},
        )
        bajo_costo = await _put(api, admin_csrf, f"{FQ}/{ctz['id']}", {"factor": "1"})
        assert dec(bajo_costo["estimated_profit"]) < 0
        assert "V2_FQ_SELLING_BELOW_COST" in bajo_costo["warnings"]
        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        assert "V2_FQ_SELLING_BELOW_COST" not in resumen["warnings"]

    async def test_el_doble_clic_no_reemite_y_un_cambio_da_conflicto(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        primera = await _post(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        segunda = await _post(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        assert primera["issued_at"] == segunda["issued_at"]

        otra = await cotizacion_del_excel(api, admin_csrf, ids)
        vieja = (await api.get(f"{FQ}/{otra['id']}/preview")).json()["fingerprint"]
        await _put(api, admin_csrf, f"{FQ}/{otra['id']}", {"factor": "1.5"})
        conflicto = await api.post(
            f"{FQ}/{otra['id']}/confirm",
            json={"expected_fingerprint": vieja},
            headers=h(admin_csrf),
        )
        assert conflicto.status_code == 409, conflicto.text

    async def test_una_emitida_no_se_edita(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        await _post(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        respuesta = await api.put(f"{FQ}/{ctz['id']}", json={"factor": "2"}, headers=h(admin_csrf))
        assert respuesta.status_code == 409, respuesta.text

    async def test_sin_cliente_o_sin_piezas_no_se_emite(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        vacia = await _post(api, admin_csrf, FQ, {"name": "Vacia"})
        resumen = (await api.get(f"{FQ}/{vacia['id']}/preview")).json()
        codigos = {bloqueo["code"] for bloqueo in resumen["blockers"]}
        assert not resumen["can_confirm"]
        assert "V2_FQ_NO_CUSTOMER" in codigos
        assert "V2_FQ_NO_LINES" in codigos
        respuesta = await api.post(
            f"{FQ}/{vacia['id']}/confirm",
            json={"expected_fingerprint": resumen["fingerprint"]},
            headers=h(admin_csrf),
        )
        assert respuesta.status_code == 422, respuesta.text
        assert ids["chico"] > 0

    async def test_lo_emitido_no_cambia_aunque_cambie_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        emitida = await _post(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        await _put(
            api,
            admin_csrf,
            f"{V2_SETTINGS}/kiln-rates/{ids['chico']}/LOW",
            {"gas_cost": "99", "external_rate": "999", "student_rate": "90"},
        )
        releida = (await api.get(f"{FQ}/{ctz['id']}")).json()
        assert dec(releida["total_amount"]) == dec(emitida["total_amount"])
        assert dec(releida["commercial_rate_low"]) == Decimal(200)


class TestAnularYDuplicar:
    async def test_anular_es_idempotente_y_conserva_las_cifras(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        await _post(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        anulada = await _post(
            api, admin_csrf, f"{FQ}/{ctz['id']}/cancel", {"reason": "El cliente desistio"}
        )
        assert anulada["status"] == "CANCELLED"
        assert dec(anulada["total_amount"]) == Decimal("4157.73")
        otra_vez = await _post(api, admin_csrf, f"{FQ}/{ctz['id']}/cancel", {})
        assert otra_vez["cancelled_at"] == anulada["cancelled_at"]

    async def test_una_vigente_no_se_duplica_y_una_vencida_si(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        ids = await preparar(api, admin_csrf)
        ctz = await cotizacion_del_excel(api, admin_csrf, ids)
        resumen = (await api.get(f"{FQ}/{ctz['id']}/preview")).json()
        await _post(
            api,
            admin_csrf,
            f"{FQ}/{ctz['id']}/confirm",
            {"expected_fingerprint": resumen["fingerprint"]},
        )
        vigente = await api.post(f"{FQ}/{ctz['id']}/duplicate", headers=h(admin_csrf))
        assert vigente.status_code == 409, vigente.text

        await db_session.execute(
            text(
                "UPDATE v2_firing_quotations SET"
                " issued_at = issued_at - make_interval(days => 40),"
                " valid_until = valid_until - 40,"
                " expires_at = expires_at - make_interval(days => 40)"
                " WHERE id = :id"
            ),
            {"id": ctz["id"]},
        )
        await db_session.commit()

        duplicada = await _post(api, admin_csrf, f"{FQ}/{ctz['id']}/duplicate")
        assert duplicada["created"] is True
        nueva = duplicada["quotation"]
        assert nueva["status"] == "DRAFT"
        assert nueva["duplicated_from_id"] == ctz["id"]
        assert dec(nueva["total_volume_cm3"]) == Decimal(133100)
        assert dec(nueva["factor"]) == Decimal(1)
        # El doble clic devuelve el mismo borrador.
        repetida = await _post(api, admin_csrf, f"{FQ}/{ctz['id']}/duplicate")
        assert repetida["created"] is False
        assert repetida["quotation"]["id"] == nueva["id"]
        # La original conserva lo suyo.
        original = (await api.get(f"{FQ}/{ctz['id']}")).json()
        assert original["status"] == "CONFIRMED"
        assert dec(original["total_amount"]) == Decimal("4157.73")


class TestAutorizacion:
    async def test_sin_sesion_no_se_lee(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(f"{FQ}/1")).status_code == 401

    async def test_el_operador_no_entra(self, api: httpx.AsyncClient) -> None:
        """Expone gas, costo real y margen: es informacion de administracion."""
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        assert (await api.get(FQ)).status_code == 403
        assert (await api.get(f"{FQ}/1")).status_code == 403
        assert (await api.get(f"{FQ}/1/pdf")).status_code == 403
        creada = await api.post(FQ, json={"name": "Del operador"}, headers=h(csrf))
        assert creada.status_code == 403
