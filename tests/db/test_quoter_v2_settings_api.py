"""Fase 010B — la configuracion comercial del Cotizador V2 contra PostgreSQL.

Lo que aqui se comprueba, por orden de importancia:

1. **la regla central de la fase**: configuracion global y snapshot de
   cotizacion son dos entidades distintas. Mover un default no reescribe lo ya
   cotizado, y editar una cotizacion no mueve el default;
2. que el IGV que usa V2 es el canonico de la empresa, no uno propio;
3. que las tarifas de horno de V2 no se mezclan con las de Legacy;
4. permisos, validaciones y concurrencia.

El punto 1 no produce un error visible cuando se rompe: produce un precio
distinto del que se acordo con el cliente.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

SETTINGS = "/api/v1/quoter-v2/settings"
V2 = "/api/v1/quotations-v2"
KILNS = "/api/v1/kilns"


async def leer(api: httpx.AsyncClient) -> dict[str, Any]:
    response = await api.get(SETTINGS)
    assert response.status_code == 200, response.text
    return dict(response.json())


async def editar(api: httpx.AsyncClient, csrf: str, **campos: Any) -> httpx.Response:
    actual = await leer(api)
    payload = {"expected_version": actual["settings"]["version"], **campos}
    return await api.put(SETTINGS, json=payload, headers={"X-CSRF-Token": csrf})


async def crear_cotizacion(api: httpx.AsyncClient, csrf: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "Pedido 010B"}
    payload.update(overrides)
    response = await api.post(V2, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    async def test_sin_sesion_no_se_lee(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(SETTINGS)).status_code == 401

    async def test_configurar_es_administracion(self, api: httpx.AsyncClient) -> None:
        """Define los precios con los que cotiza todo el taller."""
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        assert (await api.get(SETTINGS)).status_code == 403
        response = await api.put(
            SETTINGS,
            json={"expected_version": 1, "workday_hours": "6"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Defaults aprobados
# ---------------------------------------------------------------------------
class TestDefaults:
    async def test_nace_con_los_valores_aprobados(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        config = (await leer(api))["settings"]

        assert Decimal(config["workday_hours"]) == Decimal(8)
        assert Decimal(config["space_service_cost_per_day"]) == Decimal(140)
        assert Decimal(config["administrative_cost_per_quote"]) == Decimal(200)
        assert Decimal(config["commercial_factor_default"]) == Decimal(3)
        assert Decimal(config["commercial_factor_min"]) == Decimal(2)
        assert config["quotation_validity_days"] == 20
        assert config["default_production_type"] == "RETAIL"
        assert config["default_customer_kind"] == "EXTERNAL"
        assert config["low_fire_enabled_default"] is True
        assert config["high_fire_enabled_default"] is True

    async def test_la_tarifa_hora_de_ilustracion_se_deriva(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """110 por 8 horas son 13.75 la hora. No se guarda: se calcula."""
        config = (await leer(api))["settings"]
        assert Decimal(config["illustration_hourly_rate"]) == Decimal("13.75")

    async def test_cambiar_la_jornada_mueve_la_tarifa_hora(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Porque es derivada. Si estuviera almacenada, quedaria desfasada."""
        assert (await editar(api, admin_csrf, workday_hours="10")).status_code == 200
        config = (await leer(api))["settings"]
        assert Decimal(config["illustration_hourly_rate"]) == Decimal("11")

    async def test_las_tarifas_de_horno_nacen_vacias(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El sistema no sabe cual de los hornos del taller es «el chico».

        Atribuirlo por capacidad pondria una tarifa de 200 soles en el horno
        equivocado, y eso se descubre cuando ya se envio la cotizacion.
        """
        pagina = await leer(api)
        assert pagina["kiln_rates"] == []

    async def test_los_valores_aprobados_se_ofrecen_como_referencia(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        referencia = (await leer(api))["reference_rates"]
        assert Decimal(referencia["SMALL"]["external_rate_low"]) == Decimal(200)
        assert Decimal(referencia["LARGE"]["student_rate_high"]) == Decimal(2000)


# ---------------------------------------------------------------------------
# El IGV es el canonico
# ---------------------------------------------------------------------------
class TestPoliticaCanonica:
    async def test_el_igv_se_lee_de_la_configuracion_de_la_empresa(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        await db_session.execute(text("UPDATE commercial_settings SET tax_percent = 18"))
        await db_session.commit()

        config = (await leer(api))["settings"]
        assert Decimal(config["tax_percent"]) == Decimal(18)
        assert config["canonical_source"] == "commercial_settings"

    async def test_cambiar_el_igv_de_la_empresa_lo_cambia_para_v2(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Porque es el mismo dato, no una copia.

        Si V2 tuviera el suyo, aqui seguiria viendose el valor viejo y alguien
        acabaria emitiendo con el impuesto equivocado.
        """
        await db_session.execute(text("UPDATE commercial_settings SET tax_percent = 18"))
        await db_session.commit()
        assert Decimal((await leer(api))["settings"]["tax_percent"]) == Decimal(18)

        await db_session.execute(text("UPDATE commercial_settings SET tax_percent = 21"))
        await db_session.commit()
        assert Decimal((await leer(api))["settings"]["tax_percent"]) == Decimal(21)

    async def test_el_igv_no_se_puede_editar_desde_la_configuracion_v2(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Una segunda puerta para el mismo dato es una segunda verdad."""
        response = await editar(api, admin_csrf, tax_percent="21")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Edicion y concurrencia
# ---------------------------------------------------------------------------
class TestEdicion:
    async def test_se_edita_y_se_persiste(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        assert (await editar(api, admin_csrf, space_service_cost_per_day="160")).status_code == 200
        config = (await leer(api))["settings"]
        assert Decimal(config["space_service_cost_per_day"]) == Decimal(160)
        assert config["version"] == 2

    async def test_una_escritura_con_version_vieja_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sin esto, la ultima escritura gana y la primera desaparece en silencio."""
        version_leida = (await leer(api))["settings"]["version"]
        assert (await editar(api, admin_csrf, workday_hours="9")).status_code == 200

        tarde = await api.put(
            SETTINGS,
            json={"expected_version": version_leida, "workday_hours": "7"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert tarde.status_code == 409
        assert tarde.json()["error"]["code"] == "V2_SETTINGS_VERSION_CONFLICT"

    async def test_un_factor_por_debajo_del_suelo_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        assert (await editar(api, admin_csrf, commercial_factor_min="1.5")).status_code == 422

    async def test_un_default_fuera_del_rango_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """x5 con maximo x3 no es una preferencia: es incoherente."""
        response = await editar(api, admin_csrf, commercial_factor_default="5")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "V2_FACTOR_DEFAULT_OUT_OF_RANGE"

    async def test_un_horno_inexistente_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await editar(api, admin_csrf, retail_kiln_id=9999)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "V2_KILN_NOT_FOUND"

    async def test_la_edicion_queda_auditada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        await editar(api, admin_csrf, administrative_cost_per_quote="250")

        total = await db_session.scalar(
            text("SELECT count(*) FROM audit_events WHERE entity_type = 'v2_commercial_settings'")
        )
        assert total == 1


# ---------------------------------------------------------------------------
# Tarifas de horno
# ---------------------------------------------------------------------------
class TestTarifasDeHorno:
    async def test_se_fijan_los_tres_numeros_de_un_horno(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await _crear_horno(api, admin_csrf, "Horno chico", 17000)

        response = await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/LOW",
            json={"gas_cost": "35", "external_rate": "200", "student_rate": "90"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 200, response.text

        tarifas = response.json()["kiln_rates"]
        assert len(tarifas) == 1
        assert Decimal(tarifas[0]["gas_cost"]) == Decimal(35)
        assert Decimal(tarifas[0]["external_rate"]) == Decimal(200)
        assert Decimal(tarifas[0]["student_rate"]) == Decimal(90)

    async def test_baja_y_alta_son_filas_distintas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await _crear_horno(api, admin_csrf, "Horno grande", 200000)

        for tipo, gas in (("LOW", "55"), ("HIGH", "110")):
            response = await api.put(
                f"{SETTINGS}/kiln-rates/{horno['id']}/{tipo}",
                json={"gas_cost": gas},
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert response.status_code == 200, response.text

        tarifas = {r["firing_type"]: r for r in response.json()["kiln_rates"]}
        assert Decimal(tarifas["LOW"]["gas_cost"]) == Decimal(55)
        assert Decimal(tarifas["HIGH"]["gas_cost"]) == Decimal(110)

    async def test_volver_a_fijarla_edita_en_vez_de_duplicar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await _crear_horno(api, admin_csrf, "Horno unico", 50000)
        ruta = f"{SETTINGS}/kiln-rates/{horno['id']}/LOW"

        await api.put(ruta, json={"gas_cost": "35"}, headers={"X-CSRF-Token": admin_csrf})
        response = await api.put(
            ruta, json={"gas_cost": "40"}, headers={"X-CSRF-Token": admin_csrf}
        )

        tarifas = response.json()["kiln_rates"]
        assert len(tarifas) == 1
        assert Decimal(tarifas[0]["gas_cost"]) == Decimal(40)

    async def test_las_tarifas_v2_no_tocan_las_de_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La comprobacion que justifica tener una tabla aparte.

        El costeo Legacy toma la primera tarifa de `kiln_rates` para cada
        `(horno, tipo)`. Si las de V2 aterrizaran ahi, cobraria una quema
        historica con una tarifa de alumno sin que nada avisara.
        """
        horno = await _crear_horno(api, admin_csrf, "Horno compartido", 17000)
        await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/LOW",
            json={"gas_cost": "35", "external_rate": "200", "student_rate": "90"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        legacy = await db_session.scalar(text("SELECT count(*) FROM kiln_rates"))
        assert legacy == 0, "una tarifa V2 se colo en la tabla de Legacy"

    async def test_una_tarifa_negativa_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        horno = await _crear_horno(api, admin_csrf, "Horno QA", 17000)
        response = await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/LOW",
            json={"gas_cost": "-1"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 422

    async def test_un_horno_inexistente_da_404(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await api.put(
            f"{SETTINGS}/kiln-rates/9999/LOW",
            json={"gas_cost": "35"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 404


async def _crear_horno(
    api: httpx.AsyncClient, csrf: str, nombre: str, capacidad: int
) -> dict[str, Any]:
    response = await api.post(
        KILNS,
        json={"name": nombre, "capacity_volume_cm3": str(capacidad)},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# LA regla de la fase: configuracion global != snapshot
# ---------------------------------------------------------------------------
class TestSnapshot:
    async def test_la_cotizacion_nace_con_los_valores_de_la_configuracion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear_cotizacion(api, admin_csrf)

        assert Decimal(creada["workday_hours"]) == Decimal(8)
        assert Decimal(creada["space_service_cost_per_day"]) == Decimal(140)
        assert Decimal(creada["administrative_cost"]) == Decimal(200)
        assert Decimal(creada["commercial_factor"]) == Decimal(3)
        assert creada["validity_days"] == 20
        assert creada["settings_version"] == 1

    async def test_mover_un_default_no_reescribe_lo_ya_cotizado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La prueba que justifica la fase entera.

        Subir el costo del taller de 140 a 200 no puede cambiar el precio de
        una cotizacion que ya se calculo —ni, mucho menos, de una que ya se
        envio al cliente—.
        """
        antes = await crear_cotizacion(api, admin_csrf)
        assert Decimal(antes["space_service_cost_per_day"]) == Decimal(140)

        assert (await editar(api, admin_csrf, space_service_cost_per_day="200")).status_code == 200

        releida = (await api.get(f"{V2}/{antes['id']}")).json()
        assert Decimal(releida["space_service_cost_per_day"]) == Decimal(140), (
            "la cotizacion cambio al mover la configuracion: el snapshot no es una copia"
        )

    async def test_la_siguiente_cotizacion_si_nace_con_el_valor_nuevo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La otra mitad: la configuracion manda sobre lo que todavia no existe."""
        await editar(api, admin_csrf, space_service_cost_per_day="200")
        nueva = await crear_cotizacion(api, admin_csrf)
        assert Decimal(nueva["space_service_cost_per_day"]) == Decimal(200)
        assert nueva["settings_version"] == 2

    async def test_lo_que_pide_el_alta_manda_sobre_el_default(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear_cotizacion(
            api, admin_csrf, production_type="WHOLESALE", customer_kind="STUDENT"
        )
        assert creada["production_type"] == "WHOLESALE"
        assert creada["customer_kind"] == "STUDENT"

    async def test_elegir_en_una_cotizacion_no_mueve_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Decidir dentro de una cotizacion es decidir para esa cotizacion."""
        await crear_cotizacion(api, admin_csrf, production_type="WHOLESALE")

        config = (await leer(api))["settings"]
        assert config["default_production_type"] == "RETAIL"
        assert config["version"] == 1

    async def test_en_moneda_base_no_se_guarda_tipo_de_cambio(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un 1 ahi seria un tipo de cambio inventado que alguien multiplicaria."""
        creada = await crear_cotizacion(api, admin_csrf, currency_code="PEN")
        assert creada["currency_code"] == "PEN"
        assert creada["exchange_rate"] is None

    async def test_en_moneda_extranjera_se_congela_el_tipo_de_cambio(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear_cotizacion(api, admin_csrf, currency_code="USD")
        assert creada["currency_code"] == "USD"
        assert Decimal(creada["exchange_rate"]) == Decimal("3.5")
        assert creada["currency_symbol"] == "US$"

    async def test_el_tipo_de_cambio_del_alta_manda(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Es MANUAL, como en Legacy: no hay proveedor automatico."""
        creada = await crear_cotizacion(api, admin_csrf, currency_code="USD", exchange_rate="3.8")
        assert Decimal(creada["exchange_rate"]) == Decimal("3.8")

    async def test_el_igv_vigente_queda_congelado_en_la_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        await db_session.execute(text("UPDATE commercial_settings SET tax_percent = 18"))
        await db_session.commit()
        creada = await crear_cotizacion(api, admin_csrf)
        assert Decimal(creada["tax_percent"]) == Decimal(18)

        await db_session.execute(text("UPDATE commercial_settings SET tax_percent = 21"))
        await db_session.commit()

        releida = (await api.get(f"{V2}/{creada['id']}")).json()
        assert Decimal(releida["tax_percent"]) == Decimal(18), (
            "subir el IGV reescribio una cotizacion ya emitida"
        )

    async def test_los_limites_del_factor_viajan_con_la_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Autorizan lo que la cotizacion contiene.

        Si manana el minimo sube, una emitida a x2 sigue siendo valida: se
        emitio cuando x2 estaba permitido, y debe poder explicarse sin
        consultar una configuracion que ya cambio.
        """
        creada = await crear_cotizacion(api, admin_csrf, commercial_factor="2.5")
        assert Decimal(creada["commercial_factor"]) == Decimal("2.5")
        assert Decimal(creada["commercial_factor_min"]) == Decimal(2)
        assert Decimal(creada["commercial_factor_max"]) == Decimal(3)

    async def test_un_factor_por_debajo_del_suelo_no_entra(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await api.post(
            V2, json={"commercial_factor": "1.5"}, headers={"X-CSRF-Token": admin_csrf}
        )
        assert response.status_code == 422

    async def test_la_base_tambien_rechaza_un_factor_por_debajo_del_suelo(
        self, db_session: AsyncSession
    ) -> None:
        """La regla no depende de que el servicio se acuerde de comprobarla."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await db_session.execute(
                text(
                    "INSERT INTO v2_quotations (code, pricing_engine_version, commercial_factor)"
                    " VALUES ('CTZ-V2-2026-009999', 'V2', 1.5)"
                )
            )
            await db_session.commit()
        await db_session.rollback()
