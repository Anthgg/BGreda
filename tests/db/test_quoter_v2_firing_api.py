"""Fase 010E — motor de quema del Cotizador V2 contra PostgreSQL real.

Lo que aqui se comprueba, por orden de gravedad:

1. **la hornada se cobra entera.** Una segunda hornada al 60 % de carga cuesta
   una tarifa completa y consume un gas completo: el horno se enciende entero;
2. **no existe el factor por ocupacion.** Ocupar poco horno no encarece la
   pieza. La ocupacion dice cuanto cabe y cuantas veces hay que encender;
3. **el sistema recomienda y no decide.** Que una produccion por menor no quepa
   en su horno avisa; no la convierte en por mayor ni cambia el horno;
4. **gas real y tarifa de quema son dos numeros distintos** y su diferencia es
   la ganancia propia de la quema;
5. **la quema es global y se reparte por volumen.** Dos productos al 20 % y al
   30 % comparten UNA hornada y se la reparten 40/60;
6. reenviar el mismo horno no retira lo pactado, y una emitida no cambia.

Los numeros son los del Excel aprobado: horno chico 17.000 cm3, gas 35/70,
externo 200/250, alumno 90/180.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

V2 = "/api/v1/quotations-v2"
KILNS = "/api/v1/kilns"
SETTINGS = "/api/v1/quoter-v2/settings"

#: Capacidad del horno chico del Excel. Se usa tal cual para que los
#: porcentajes de las pruebas signifiquen lo mismo que en la hoja.
CHICO = 17000
GRANDE = 200000


async def crear_horno(
    api: httpx.AsyncClient,
    csrf: str,
    nombre: str,
    capacidad: int,
    *,
    gas: tuple[str, str] | None = ("35", "70"),
    externo: tuple[str, str] | None = ("200", "250"),
    alumno: tuple[str, str] | None = ("90", "180"),
) -> dict[str, Any]:
    """Un horno con sus tres numeros por tipo de quema, o sin tarifas."""
    response = await api.post(
        KILNS,
        json={"name": nombre, "capacity_volume_cm3": str(capacidad)},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    horno = dict(response.json())

    for indice, tipo in enumerate(("LOW", "HIGH")):
        payload: dict[str, Any] = {}
        if gas is not None:
            payload["gas_cost"] = gas[indice]
        if externo is not None:
            payload["external_rate"] = externo[indice]
        if alumno is not None:
            payload["student_rate"] = alumno[indice]
        if not payload:
            continue
        tarifa = await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/{tipo}",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert tarifa.status_code == 200, tarifa.text
    return horno


async def crear_cotizacion(api: httpx.AsyncClient, csrf: str, **overrides: Any) -> int:
    payload: dict[str, Any] = {"name": "Quema 010E"}
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


async def quema(api: httpx.AsyncClient, quotation_id: int) -> dict[str, Any]:
    response = await api.get(f"{V2}/{quotation_id}/firing")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def poner_quema(
    api: httpx.AsyncClient, csrf: str, quotation_id: int, **campos: Any
) -> httpx.Response:
    return await api.put(f"{V2}/{quotation_id}/firing", json=campos, headers={"X-CSRF-Token": csrf})


async def con_ocupacion(
    api: httpx.AsyncClient,
    csrf: str,
    quotation_id: int,
    capacidad: int,
    porcentaje: str,
    *,
    nombre: str = "Pieza",
) -> dict[str, Any]:
    """Una linea cuyo volumen ocupa exactamente ese porcentaje del horno.

    La pieza mide `capacidad / 100` cm3 —el uno por ciento del horno— y la
    cantidad es el porcentaje. Asi el volumen sale exacto sin necesidad de una
    medida enorme: meter los 27.200 cm3 del 160 % en una sola pieza chocaria
    con el tope de medida, que existe para frenar un cero de mas al teclear.
    """
    unitario = Decimal(capacidad) / Decimal(100)
    assert unitario == unitario.to_integral_value(), capacidad
    return await anadir_linea(
        api,
        csrf,
        quotation_id,
        product_name=nombre,
        quantity=int(porcentaje),
        length_cm=str(unitario),
        width_cm="1",
        height_cm="1",
    )


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    async def test_sin_sesion_no_se_lee_la_quema(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(f"{V2}/1/firing")).status_code == 401

    async def test_la_quema_es_de_administracion(self, api: httpx.AsyncClient) -> None:
        """Expone el costo real del gas y la diferencia con lo que se cobra.

        Eso es informacion de margen, y el Cotizador V2 entero es de
        administracion desde 010A.
        """
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        assert (await api.get(f"{V2}/1/firing")).status_code == 403
        assert (await poner_quema(api, csrf, 1, kiln_id=1)).status_code == 403


# ---------------------------------------------------------------------------
# Defaults: el horno lo sugiere el tipo de produccion
# ---------------------------------------------------------------------------
class TestDefaults:
    async def test_por_menor_nace_con_el_horno_configurado_para_por_menor(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        chico = await crear_horno(api, admin_csrf, "Chico", CHICO)
        actual = (await api.get(SETTINGS)).json()["settings"]
        await api.put(
            SETTINGS,
            json={"expected_version": actual["version"], "retail_kiln_id": chico["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cotizacion = await crear_cotizacion(api, admin_csrf, production_type="RETAIL")

        estado = await quema(api, cotizacion)
        assert estado["kiln_id"] == chico["id"]
        assert estado["kiln_name"] == "Chico"

    async def test_por_mayor_nace_con_el_suyo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        chico = await crear_horno(api, admin_csrf, "Chico", CHICO)
        grande = await crear_horno(api, admin_csrf, "Grande", GRANDE)
        actual = (await api.get(SETTINGS)).json()["settings"]
        await api.put(
            SETTINGS,
            json={
                "expected_version": actual["version"],
                "retail_kiln_id": chico["id"],
                "wholesale_kiln_id": grande["id"],
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        cotizacion = await crear_cotizacion(api, admin_csrf, production_type="WHOLESALE")

        assert (await quema(api, cotizacion))["kiln_id"] == grande["id"]

    async def test_sin_hornos_configurados_nace_sin_horno_y_avisa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Una instalacion recien creada no tiene hornos, y aun asi se cotiza.

        Impedir abrir un borrador por eso dejaria el sistema sin forma de
        empezar a usarse.
        """
        cotizacion = await crear_cotizacion(api, admin_csrf)

        estado = await quema(api, cotizacion)
        assert estado["kiln_id"] is None
        assert "V2_FIRING_KILN_NOT_SELECTED" in estado["warnings"]
        assert Decimal(estado["commercial_total"]) == Decimal(0)

    async def test_no_se_puede_sugerir_un_horno_dado_de_baja(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La misma regla que dentro de la cotizacion, en la otra superficie.

        Sin esto, configurar un horno retirado se guardaba sin protestar y
        despues las cotizaciones nuevas nacian sin horno sin que nada dijera
        por que.
        """
        horno = await crear_horno(api, admin_csrf, "Retirado", CHICO)
        await db_session.execute(
            text("UPDATE kilns SET active = false WHERE id = :id"), {"id": horno["id"]}
        )
        await db_session.commit()
        actual = (await api.get(SETTINGS)).json()["settings"]

        respuesta = await api.put(
            SETTINGS,
            json={"expected_version": actual["version"], "retail_kiln_id": horno["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert respuesta.status_code == 422
        assert respuesta.json()["error"]["code"] == "V2_KILN_INACTIVE"

    async def test_las_dos_quemas_nacen_encendidas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)

        estado = await quema(api, cotizacion)
        assert estado["low_fire_enabled"] is True
        assert estado["high_fire_enabled"] is True


# ---------------------------------------------------------------------------
# Hornadas: la tabla aprobada, contra la base de verdad
# ---------------------------------------------------------------------------
class TestHornadas:
    @pytest.mark.parametrize(
        ("porcentaje", "hornadas"),
        [("1", 1), ("99", 1), ("100", 1), ("101", 2), ("160", 2), ("200", 2), ("201", 3)],
    )
    async def test_la_tabla_de_hornadas(
        self, api: httpx.AsyncClient, admin_csrf: str, porcentaje: str, hornadas: int
    ) -> None:
        """100 % exacto es UNA hornada; 200 % exacto son DOS, no tres."""
        horno = await crear_horno(api, admin_csrf, f"Horno {porcentaje}", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, porcentaje)

        estado = await quema(api, cotizacion)
        assert estado["firing_count"] == hornadas
        assert estado["low_fire_count"] == hornadas
        assert estado["high_fire_count"] == hornadas

    async def test_sin_volumen_no_hay_hornadas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Vacio", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])

        estado = await quema(api, cotizacion)
        assert estado["firing_count"] == 0
        assert Decimal(estado["commercial_total"]) == Decimal(0)
        assert Decimal(estado["gas_total"]) == Decimal(0)
        assert "V2_FIRING_NO_VOLUME" in estado["warnings"]

    async def test_una_linea_sin_medidas_no_ocupa_y_avisa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un borrador a medio llenar se guarda: la linea avisa, no bloquea."""
        horno = await crear_horno(api, admin_csrf, "A medias", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        linea = await anadir_linea(
            api, admin_csrf, cotizacion, product_name="Sin medir", quantity=10
        )

        assert Decimal(linea["total_volume_cm3"]) == Decimal(0)
        assert "V2_FIRING_LINE_WITHOUT_DIMENSIONS" in (await quema(api, cotizacion))["warnings"]

    async def test_la_carga_de_cada_hornada_se_puede_ver(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """160 % son dos hornadas: una llena y otra al 60 %."""
        horno = await crear_horno(api, admin_csrf, "Dos hornadas", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        cargas = [Decimal(c) for c in (await quema(api, cotizacion))["batch_loads"]]
        assert cargas == [Decimal(100), Decimal(60)]


# ---------------------------------------------------------------------------
# El caso canonico y sus variantes
# ---------------------------------------------------------------------------
class TestCasoCanonico:
    async def test_externo_chico_ciento_sesenta_baja_y_alta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """2 x 200 + 2 x 250 = 900 de tarifa. 2 x 35 + 2 x 70 = 210 de gas.

        Diferencia: 690. Es el ejemplo obligatorio de la fase.
        """
        horno = await crear_horno(api, admin_csrf, "Canonico", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api, admin_csrf, cotizacion, kiln_id=horno["id"], customer_kind="EXTERNAL"
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        assert estado["firing_count"] == 2
        assert Decimal(estado["commercial_total"]) == Decimal(900)
        assert Decimal(estado["gas_total"]) == Decimal(210)
        assert Decimal(estado["difference"]) == Decimal(690)

    async def test_la_hornada_parcial_cobra_tarifa_completa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Prorratear el 60 % de la segunda daria 720, y seria creible.

        No lo es: el horno se enciende entero. La comprobacion se escribe
        contra el numero equivocado a proposito.
        """
        horno = await crear_horno(api, admin_csrf, "Parcial", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        prorrateado = (Decimal(200) + Decimal(250)) * Decimal("1.6")
        assert Decimal(estado["commercial_total"]) == Decimal(900)
        assert Decimal(estado["commercial_total"]) != prorrateado

    async def test_la_hornada_parcial_consume_gas_completo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Gas entero", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        assert Decimal(estado["gas_total"]) == Decimal(210)
        assert Decimal(estado["gas_total"]) != Decimal(105) * Decimal("1.6")

    async def test_solo_baja(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        """2 x 200 = 400 de tarifa, 2 x 35 = 70 de gas, 330 de diferencia."""
        horno = await crear_horno(api, admin_csrf, "Solo baja", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"], high_fire_enabled=False)
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        assert estado["low_fire_count"] == 2
        assert estado["high_fire_count"] == 0
        assert Decimal(estado["commercial_total"]) == Decimal(400)
        assert Decimal(estado["gas_total"]) == Decimal(70)
        assert Decimal(estado["difference"]) == Decimal(330)

    async def test_solo_alta(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        """2 x 250 = 500 de tarifa, 2 x 70 = 140 de gas."""
        horno = await crear_horno(api, admin_csrf, "Solo alta", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"], low_fire_enabled=False)
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        assert estado["low_fire_count"] == 0
        assert estado["high_fire_count"] == 2
        assert Decimal(estado["commercial_total"]) == Decimal(500)
        assert Decimal(estado["gas_total"]) == Decimal(140)

    async def test_apagar_alta_no_deja_costos_escondidos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Apagada es apagada, y la base lo vuelve a exigir con un CHECK."""
        horno = await crear_horno(api, admin_csrf, "Apagar alta", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")
        assert Decimal((await quema(api, cotizacion))["commercial_total"]) == Decimal(900)

        await poner_quema(api, admin_csrf, cotizacion, high_fire_enabled=False)

        estado = await quema(api, cotizacion)
        assert estado["high_fire_count"] == 0
        assert Decimal(estado["commercial_total"]) == Decimal(400)
        guardado = await db_session.scalar(
            text("SELECT high_fire_count FROM v2_quotations WHERE id = :id"),
            {"id": cotizacion},
        )
        assert guardado == 0

    async def test_las_dos_apagadas_avisan_y_no_inventan_costo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Sin quema", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api,
            admin_csrf,
            cotizacion,
            kiln_id=horno["id"],
            low_fire_enabled=False,
            high_fire_enabled=False,
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        assert "V2_FIRING_NO_PROCESS_SELECTED" in estado["warnings"]
        assert Decimal(estado["commercial_total"]) == Decimal(0)
        assert Decimal(estado["gas_total"]) == Decimal(0)


# ---------------------------------------------------------------------------
# Tipo de cliente
# ---------------------------------------------------------------------------
class TestTipoDeCliente:
    async def test_alumno_chico_ochenta_por_ciento(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """1 baja a 90 + 1 alta a 180 = 270. Gas: 35 + 70 = 105."""
        horno = await crear_horno(api, admin_csrf, "Alumno", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"], customer_kind="STUDENT")
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "80")

        estado = await quema(api, cotizacion)
        assert estado["firing_count"] == 1
        assert Decimal(estado["commercial_total"]) == Decimal(270)
        assert Decimal(estado["gas_total"]) == Decimal(105)

    async def test_cambiar_de_externo_a_alumno_reevalua_la_tarifa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Cambia cliente", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api, admin_csrf, cotizacion, kiln_id=horno["id"], customer_kind="EXTERNAL"
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "80")
        assert Decimal((await quema(api, cotizacion))["commercial_total"]) == Decimal(450)

        await poner_quema(api, admin_csrf, cotizacion, customer_kind="STUDENT")

        assert Decimal((await quema(api, cotizacion))["commercial_total"]) == Decimal(270)

    async def test_cambiar_de_cliente_no_cambia_el_gas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un alumno y un externo queman el mismo gas.

        El gas es un COSTO fisico. Que dependiera del cliente significaria que
        el horno consume distinto segun a quien se le factura.
        """
        horno = await crear_horno(api, admin_csrf, "Gas igual", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api, admin_csrf, cotizacion, kiln_id=horno["id"], customer_kind="EXTERNAL"
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "80")
        antes = Decimal((await quema(api, cotizacion))["gas_total"])

        await poner_quema(api, admin_csrf, cotizacion, customer_kind="STUDENT")

        assert Decimal((await quema(api, cotizacion))["gas_total"]) == antes == Decimal(105)


# ---------------------------------------------------------------------------
# Reparto multiproducto
# ---------------------------------------------------------------------------
class TestReparto:
    async def test_veinte_mas_treinta_es_una_sola_quema_repartida_cuarenta_sesenta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Dos productos al 20 % y al 30 % ocupan el 50 %: UNA hornada.

        El reparto es 20/50 y 30/50, es decir 40 % y 60 %.
        """
        horno = await crear_horno(api, admin_csrf, "Reparto", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "20", nombre="A")
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "30", nombre="B")

        estado = await quema(api, cotizacion)
        assert estado["firing_count"] == 1
        assert Decimal(estado["occupancy_percent"]) == Decimal(50)

        a, b = estado["lines"]
        assert Decimal(a["volume_share_percent"]) == Decimal(40)
        assert Decimal(b["volume_share_percent"]) == Decimal(60)
        # 450 de quema: 200 de baja + 250 de alta, una hornada de cada.
        assert Decimal(estado["commercial_total"]) == Decimal(450)
        assert Decimal(a["commercial_cost"]) == Decimal(180)
        assert Decimal(b["commercial_cost"]) == Decimal(270)

    async def test_la_suma_repartida_es_exactamente_el_total(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Tres partes iguales no caben exactas en ningun decimal finito.

        Sin politica de resto quedaria una millonesima sin asignar a nadie.
        """
        horno = await crear_horno(api, admin_csrf, "Tercios", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        for nombre in ("A", "B", "C"):
            await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "10", nombre=nombre)

        estado = await quema(api, cotizacion)
        repartido = sum(Decimal(linea["commercial_cost"]) for linea in estado["lines"])
        gas = sum(Decimal(linea["gas_cost"]) for linea in estado["lines"])
        assert repartido == Decimal(estado["commercial_total"])
        assert gas == Decimal(estado["gas_total"])

    async def test_con_varias_hornadas_el_reparto_es_sobre_el_volumen_total(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """A ocupa 100 % y B 60 %: el reparto es 100/160 y 60/160.

        NO «A es la hornada 1 y B la hornada 2»: el motor reparte
        economicamente por proporcion y no simula como se colocan las piezas.
        """
        horno = await crear_horno(api, admin_csrf, "Dos cargas", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "100", nombre="A")
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "60", nombre="B")

        estado = await quema(api, cotizacion)
        a, b = estado["lines"]
        assert Decimal(a["volume_share_percent"]) == Decimal("62.5")
        assert Decimal(b["volume_share_percent"]) == Decimal("37.5")
        assert Decimal(a["commercial_cost"]) == Decimal(900) * Decimal("0.625")
        assert Decimal(b["commercial_cost"]) == Decimal(900) * Decimal("0.375")

    async def test_ocupar_poco_horno_no_encarece_la_pieza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La regla que 010E elimina, comprobada por su consecuencia.

        En Legacy una pieza que ocupa el 1 % del horno paga un multiplicador
        de hasta x3. Aqui, una pieza sola al 1 % y la misma pieza al 50 %
        absorben EXACTAMENTE la misma quema: una hornada, entera, para ella.
        """
        horno = await crear_horno(api, admin_csrf, "Sin factor", CHICO)
        poca = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, poca, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, poca, CHICO, "1")

        mucha = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, mucha, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, mucha, CHICO, "50")

        assert (
            Decimal((await quema(api, poca))["commercial_total"])
            == Decimal((await quema(api, mucha))["commercial_total"])
            == Decimal(450)
        )


# ---------------------------------------------------------------------------
# Recomendaciones: avisan, no actuan
# ---------------------------------------------------------------------------
class TestRecomendaciones:
    async def test_superar_la_capacidad_avisa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Lleno", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        assert "V2_FIRING_OVER_CAPACITY" in (await quema(api, cotizacion))["warnings"]

    async def test_por_menor_que_no_cabe_avisa_y_no_se_convierte_en_por_mayor(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La decision es humana. El sistema dice lo que ve y se detiene ahi."""
        horno = await crear_horno(api, admin_csrf, "Menor lleno", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf, production_type="RETAIL")
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        assert "V2_FIRING_RETAIL_OVER_CAPACITY" in estado["warnings"]
        assert estado["production_type"] == "RETAIL"

    async def test_una_carga_pequena_en_horno_grande_recomienda_el_chico(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        chico = await crear_horno(api, admin_csrf, "A chico", CHICO)
        grande = await crear_horno(api, admin_csrf, "B grande", GRANDE)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=grande["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")

        estado = await quema(api, cotizacion)
        assert estado["recommended_kiln_id"] == chico["id"]
        assert "V2_FIRING_SMALLER_KILN_FITS" in estado["warnings"]
        # Recomienda y NO cambia: el horno sigue siendo el que se eligio.
        assert estado["kiln_id"] == grande["id"]

    async def test_una_carga_que_no_cabe_en_el_chico_recomienda_el_grande(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        chico = await crear_horno(api, admin_csrf, "A chico", CHICO)
        grande = await crear_horno(api, admin_csrf, "B grande", GRANDE)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=chico["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        estado = await quema(api, cotizacion)
        assert estado["recommended_kiln_id"] == grande["id"]
        assert "V2_FIRING_LARGER_KILN_SUGGESTED" in estado["warnings"]
        assert estado["kiln_id"] == chico["id"]

    async def test_la_lista_de_hornos_dice_que_pasaria_con_cada_uno(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        chico = await crear_horno(api, admin_csrf, "A chico", CHICO)
        await crear_horno(api, admin_csrf, "B grande", GRANDE)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=chico["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        hornos = {h["name"]: h for h in (await quema(api, cotizacion))["kilns"]}
        assert hornos["A chico"]["firing_count"] == 2
        assert hornos["B grande"]["firing_count"] == 1
        assert all(h["has_rates"] for h in hornos.values())

    async def test_un_horno_sin_tarifas_se_marca(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sin tarifas no se puede costear, y hay que poder decirlo antes."""
        horno = await crear_horno(
            api, admin_csrf, "Sin tarifas", CHICO, gas=None, externo=None, alumno=None
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")

        estado = await quema(api, cotizacion)
        assert "V2_FIRING_RATES_MISSING" in estado["warnings"]
        assert Decimal(estado["commercial_total"]) == Decimal(0)
        assert next(h for h in estado["kilns"] if h["kiln_id"] == horno["id"])["has_rates"] is False

    async def test_un_horno_a_medias_depende_de_lo_que_pida_la_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Con SOLO la baja configurada, el mismo horno sirve o no segun el caso.

        Sirve para una cotizacion que solo hace baja; no sirve para una que
        ademas hace alta, porque la mitad del costeo saldria en cero. Un unico
        «tiene tarifas» lo habria ofrecido igual en los dos casos.
        """
        respuesta = await api.post(
            KILNS,
            json={"name": "Solo con baja", "capacity_volume_cm3": str(CHICO)},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert respuesta.status_code == 201, respuesta.text
        horno = dict(respuesta.json())
        tarifa = await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/LOW",
            json={"gas_cost": "35", "external_rate": "200", "student_rate": "90"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert tarifa.status_code == 200, tarifa.text

        async def marca(quotation_id: int) -> bool:
            fila = next(
                h for h in (await quema(api, quotation_id))["kilns"] if h["kiln_id"] == horno["id"]
            )
            return bool(fila["has_rates"])

        con_alta = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, con_alta, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, con_alta, CHICO, "50")

        solo_baja = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, solo_baja, kiln_id=horno["id"], high_fire_enabled=False)
        await con_ocupacion(api, admin_csrf, solo_baja, CHICO, "50")

        assert await marca(con_alta) is False
        assert await marca(solo_baja) is True


# ---------------------------------------------------------------------------
# Overrides por cotizacion
# ---------------------------------------------------------------------------
class TestOverrides:
    async def test_el_override_no_toca_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """CTZ-001 usa 40 de gas; el maestro sigue en 35 y CTZ-002 tambien."""
        horno = await crear_horno(api, admin_csrf, "Override", CHICO)
        primera = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, primera, kiln_id=horno["id"], gas_cost_low_override="40")
        await con_ocupacion(api, admin_csrf, primera, CHICO, "50")

        segunda = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, segunda, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, segunda, CHICO, "50")

        assert Decimal((await quema(api, primera))["gas_cost_low"]) == Decimal(40)
        assert Decimal((await quema(api, segunda))["gas_cost_low"]) == Decimal(35)

        tarifas = (await api.get(SETTINGS)).json()["kiln_rates"]
        baja = next(r for r in tarifas if r["kiln_id"] == horno["id"] and r["firing_type"] == "LOW")
        assert Decimal(baja["gas_cost"]) == Decimal(35)

    async def test_reenviar_el_mismo_horno_conserva_lo_pactado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La presencia de una clave NO es un cambio.

        Es el error que 010C y 010D pagaron caro: una pantalla que reenvia el
        formulario entero mandaria el mismo horno de siempre, y leerlo como
        «eligio horno hoy» borraria el precio acordado con el cliente.
        """
        horno = await crear_horno(api, admin_csrf, "Reenvio", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api,
            admin_csrf,
            cotizacion,
            kiln_id=horno["id"],
            commercial_rate_low_override="300",
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")

        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])

        estado = await quema(api, cotizacion)
        assert Decimal(estado["commercial_rate_low"]) == Decimal(300)
        assert estado["commercial_low_is_override"] is True

    async def test_cambiar_de_horno_de_verdad_reevalua_las_tarifas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Lo pactado se pacto sobre OTRO horno y no se hereda."""
        chico = await crear_horno(api, admin_csrf, "A chico", CHICO)
        grande = await crear_horno(
            api,
            admin_csrf,
            "B grande",
            GRANDE,
            gas=("55", "110"),
            externo=("700", "1200"),
            alumno=("1000", "2000"),
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api, admin_csrf, cotizacion, kiln_id=chico["id"], gas_cost_low_override="40"
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")

        await poner_quema(api, admin_csrf, cotizacion, kiln_id=grande["id"])

        estado = await quema(api, cotizacion)
        assert Decimal(estado["gas_cost_low"]) == Decimal(55)
        assert estado["gas_low_is_override"] is False
        assert Decimal(estado["commercial_rate_low"]) == Decimal(700)

    async def test_un_override_en_cero_es_un_valor_elegido(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un horno prestado con el gas incluido se cotiza con gas cero."""
        horno = await crear_horno(api, admin_csrf, "Gas cero", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api,
            admin_csrf,
            cotizacion,
            kiln_id=horno["id"],
            gas_cost_low_override="0",
            gas_cost_high_override="0",
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")

        estado = await quema(api, cotizacion)
        assert Decimal(estado["gas_total"]) == Decimal(0)
        assert estado["gas_low_is_override"] is True
        assert Decimal(estado["commercial_total"]) == Decimal(450)

    async def test_un_nulo_explicito_retira_el_acuerdo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Retirar", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api, admin_csrf, cotizacion, kiln_id=horno["id"], gas_cost_low_override="40"
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")

        await poner_quema(api, admin_csrf, cotizacion, gas_cost_low_override=None)

        estado = await quema(api, cotizacion)
        assert Decimal(estado["gas_cost_low"]) == Decimal(35)
        assert estado["gas_low_is_override"] is False

    async def test_cambiar_el_cliente_no_borra_el_acuerdo_de_gas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El gas no depende del cliente, asi que su acuerdo tampoco."""
        horno = await crear_horno(api, admin_csrf, "Gas pactado", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(
            api,
            admin_csrf,
            cotizacion,
            kiln_id=horno["id"],
            customer_kind="EXTERNAL",
            gas_cost_low_override="40",
        )
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")

        await poner_quema(api, admin_csrf, cotizacion, customer_kind="STUDENT")

        estado = await quema(api, cotizacion)
        assert Decimal(estado["gas_cost_low"]) == Decimal(40)
        assert estado["gas_low_is_override"] is True
        assert Decimal(estado["commercial_rate_low"]) == Decimal(90)


# ---------------------------------------------------------------------------
# Snapshots: el maestro cambia, la cotizacion no
# ---------------------------------------------------------------------------
class TestSnapshots:
    async def test_subir_la_tarifa_no_reescribe_lo_ya_cotizado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Sube", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "50")
        assert Decimal((await quema(api, cotizacion))["commercial_total"]) == Decimal(450)

        await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/LOW",
            json={"external_rate": "500"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert Decimal((await quema(api, cotizacion))["commercial_rate_low"]) == Decimal(200)

    async def test_remedir_el_horno_no_cambia_las_hornadas_ya_calculadas(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La capacidad se congela: 160 % siguen siendo dos hornadas."""
        horno = await crear_horno(api, admin_csrf, "Remedido", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        await db_session.execute(
            text("UPDATE kilns SET capacity_volume_cm3 = :cap WHERE id = :id"),
            {"cap": GRANDE, "id": horno["id"]},
        )
        await db_session.commit()

        estado = await quema(api, cotizacion)
        assert Decimal(estado["kiln_capacity_cm3"]) == Decimal(CHICO)
        assert estado["firing_count"] == 2

    async def test_una_emitida_no_cambia_aunque_cambie_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Emitida", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")
        antes = await quema(api, cotizacion)

        await db_session.execute(
            text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": cotizacion},
        )
        await db_session.execute(
            text("UPDATE kilns SET capacity_volume_cm3 = :cap WHERE id = :id"),
            {"cap": GRANDE, "id": horno["id"]},
        )
        await db_session.commit()

        despues = await quema(api, cotizacion)
        assert despues["firing_count"] == antes["firing_count"] == 2
        assert Decimal(despues["commercial_total"]) == Decimal(antes["commercial_total"])

    async def test_una_emitida_no_admite_cambiar_de_horno(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        chico = await crear_horno(api, admin_csrf, "A chico", CHICO)
        grande = await crear_horno(api, admin_csrf, "B grande", GRANDE)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=chico["id"])
        await db_session.execute(
            text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": cotizacion},
        )
        await db_session.commit()

        response = await poner_quema(api, admin_csrf, cotizacion, kiln_id=grande["id"])

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "V2_FIRING_QUOTATION_NOT_EDITABLE"


# ---------------------------------------------------------------------------
# Hornos retirados
# ---------------------------------------------------------------------------
class TestHornoRetirado:
    async def test_elegir_hoy_un_horno_dado_de_baja_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "De baja", CHICO)
        await db_session.execute(
            text("UPDATE kilns SET active = false WHERE id = :id"), {"id": horno["id"]}
        )
        await db_session.commit()
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "V2_FIRING_KILN_INACTIVE"

    async def test_un_horno_retirado_despues_avisa_y_no_encalla_el_borrador(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Bloquear ahi dejaria un borrador muerto por una decision de otra pantalla."""
        horno = await crear_horno(api, admin_csrf, "Se retira", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        await db_session.execute(
            text("UPDATE kilns SET active = false WHERE id = :id"), {"id": horno["id"]}
        )
        await db_session.commit()

        estado = await quema(api, cotizacion)
        assert "V2_FIRING_KILN_UNAVAILABLE" in estado["warnings"]
        assert Decimal(estado["commercial_total"]) == Decimal(900)

    async def test_retirar_el_horno_de_la_cotizacion_deja_la_quema_en_cero(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Quitar", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        await poner_quema(api, admin_csrf, cotizacion, kiln_id=None)

        estado = await quema(api, cotizacion)
        assert estado["kiln_id"] is None
        assert estado["firing_count"] == 0
        assert Decimal(estado["commercial_total"]) == Decimal(0)
        assert "V2_FIRING_KILN_NOT_SELECTED" in estado["warnings"]

    async def test_un_horno_inexistente_da_404(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)
        response = await poner_quema(api, admin_csrf, cotizacion, kiln_id=999999)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Las lineas y su geometria
# ---------------------------------------------------------------------------
class TestLineas:
    async def test_cambiar_la_cantidad_recalcula_las_hornadas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Una quema que no se recalcula al editar una linea miente."""
        horno = await crear_horno(api, admin_csrf, "Recalcula", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        # Una pieza que llena el horno entero: una hornada por unidad.
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            product_name="Plato",
            quantity=1,
            length_cm=str(CHICO // 100),
            width_cm="10",
            height_cm="10",
        )
        assert (await quema(api, cotizacion))["firing_count"] == 1

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"quantity": 2},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 200, response.text

        assert (await quema(api, cotizacion))["firing_count"] == 2

    async def test_borrar_una_linea_devuelve_la_quema_a_lo_que_queda(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "Borrar", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        a = await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "100", nombre="A")
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "60", nombre="B")
        assert (await quema(api, cotizacion))["firing_count"] == 2

        borrado = await api.delete(
            f"{V2}/{cotizacion}/products/{a['id']}", headers={"X-CSRF-Token": admin_csrf}
        )
        assert borrado.status_code == 204

        estado = await quema(api, cotizacion)
        assert estado["firing_count"] == 1
        assert Decimal(estado["occupancy_percent"]) == Decimal(60)
        assert Decimal(estado["lines"][0]["commercial_cost"]) == Decimal(450)

    async def test_el_volumen_de_la_linea_se_calcula_y_se_devuelve(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """18 x 12 x 3 son 648 cm3; veinte piezas, 12.960."""
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            product_name="Plato palta",
            quantity=20,
            length_cm="18",
            width_cm="12",
            height_cm="3",
        )

        assert Decimal(linea["unit_volume_cm3"]) == Decimal(648)
        assert Decimal(linea["total_volume_cm3"]) == Decimal(12960)

    async def test_una_medida_en_cero_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Una pieza de alto cero no existe; NULL es «todavia sin medir»."""
        cotizacion = await crear_cotizacion(api, admin_csrf)
        response = await api.post(
            f"{V2}/{cotizacion}/products",
            json={"product_name": "Plana", "quantity": 1, "height_cm": "0"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Legacy sigue intacto
# ---------------------------------------------------------------------------
class TestLegacy:
    async def test_cotizar_la_quema_no_mueve_el_factor_de_ocupacion(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """V2 no escribe en la tabla del multiplicador de Legacy.

        Ni la crea, ni la borra, ni le anade filas: el Cotizador historico
        sigue multiplicando exactamente igual que antes de esta fase.
        """
        antes = await db_session.scalar(text("SELECT count(*) FROM kiln_occupancy_factors"))
        horno = await crear_horno(api, admin_csrf, "Legacy intacto", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        despues = await db_session.scalar(text("SELECT count(*) FROM kiln_occupancy_factors"))
        assert despues == antes

    async def test_cotizar_la_quema_no_toca_las_tarifas_de_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """`kiln_rates` es de Legacy; V2 usa `v2_kiln_rates`."""
        antes = await db_session.scalar(text("SELECT count(*) FROM kiln_rates"))
        horno = await crear_horno(api, admin_csrf, "Tarifas aparte", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])

        assert await db_session.scalar(text("SELECT count(*) FROM kiln_rates")) == antes

    async def test_cotizar_la_quema_no_consume_existencia(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Encender un horno en una cotizacion no gasta nada del almacen."""
        antes = await db_session.scalar(text("SELECT count(*) FROM stock_movements"))
        horno = await crear_horno(api, admin_csrf, "Sin stock", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        assert await db_session.scalar(text("SELECT count(*) FROM stock_movements")) == antes


# ---------------------------------------------------------------------------
# La diferencia
# ---------------------------------------------------------------------------
class TestDiferencia:
    async def test_la_columna_generada_cuadra_con_sus_dos_sumandos(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La base la calcula, asi que no puede contradecirlos."""
        horno = await crear_horno(api, admin_csrf, "Diferencia", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])
        await con_ocupacion(api, admin_csrf, cotizacion, CHICO, "160")

        fila = (
            await db_session.execute(
                text(
                    "SELECT firing_commercial_total, firing_gas_total, firing_difference"
                    " FROM v2_quotations WHERE id = :id"
                ),
                {"id": cotizacion},
            )
        ).one()
        assert fila.firing_difference == fila.firing_commercial_total - fila.firing_gas_total
        assert fila.firing_difference == Decimal(690)

    async def test_la_diferencia_no_se_puede_escribir_a_mano(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        horno = await crear_horno(api, admin_csrf, "No escribible", CHICO)
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await poner_quema(api, admin_csrf, cotizacion, kiln_id=horno["id"])

        with pytest.raises(DBAPIError):
            await db_session.execute(
                text("UPDATE v2_quotations SET firing_difference = 1 WHERE id = :id"),
                {"id": cotizacion},
            )
        await db_session.rollback()
