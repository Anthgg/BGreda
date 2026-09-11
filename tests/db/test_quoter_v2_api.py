"""Fase 010A — el Cotizador V2 contra PostgreSQL real.

Dos cosas se comprueban aqui y ninguna es el calculo, porque en 010A todavia no
hay calculo:

1. que una cotizacion V2 nace, se lee y se lista con identidad propia:
   correlativo `CTZ-V2`, motor `V2` persistido en la fila y ruta separada;
2. que **Legacy y V2 no se ven**. Ni en los listados, ni por id, ni por la
   base: los CHECK de las dos tablas muerden de verdad.

El punto 2 es el que justifica la fase entera. Si un dia una cotizacion Legacy
pudiera colarse por la ruta V2 —o al reves— el resultado no seria un error
visible: seria un precio calculado con el motor que no era.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

V2 = "/api/v1/quotations-v2"
LEGACY = "/api/v1/quotations"
PARTNERS = "/api/v1/partners"


#: Columnas obligatorias de la cabecera Legacy. Se insertan a mano porque lo
#: que estas pruebas comprueban es la FRONTERA de datos entre motores, no el
#: flujo de alta del Cotizador historico: hacerlo pasar por su API traeria
#: recetas, quemas y productos a una prueba que no habla de ninguna de esas
#: cosas.
LEGACY_INSERT = (
    "INSERT INTO quotations "
    "(code, status, source_fingerprint, pricing_engine_version, "
    " commercial_factor_default_snapshot, commercial_factor) "
    "VALUES (:code, 'DRAFT', :code, :engine, 3, 3)"
)


async def crear_tercero(api: httpx.AsyncClient, csrf: str, nombre: str, rol: str) -> dict[str, Any]:
    response = await api.post(
        PARTNERS, json={"name": nombre, "role": rol}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def crear(api: httpx.AsyncClient, csrf: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "Pedido demo V2"}
    payload.update(overrides)
    response = await api.post(V2, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    async def test_sin_sesion_no_se_lee(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(V2)).status_code == 401

    async def test_cotizar_es_administracion(self, api: httpx.AsyncClient) -> None:
        """Poner un precio no es ejecutar el trabajo: el taller no cotiza."""
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        response = await api.post(V2, json={"name": "x"}, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Identidad
# ---------------------------------------------------------------------------
class TestIdentidad:
    async def test_nace_con_motor_v2_persistido(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        creada = await crear(api, admin_csrf)

        assert creada["pricing_engine_version"] == "V2"
        guardado = await db_session.scalar(
            text("SELECT pricing_engine_version FROM v2_quotations WHERE id = :id"),
            {"id": creada["id"]},
        )
        assert guardado == "V2", "el motor tiene que estar en la fila, no solo en la respuesta"

    async def test_el_correlativo_lleva_prefijo_propio(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear(api, admin_csrf)
        assert creada["code"].startswith("CTZ-V2-")

    async def test_la_respuesta_del_alta_trae_las_marcas_de_tiempo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """`created_at` y `updated_at` los pone el servidor, y llegan en el 201.

        Los dos son `server_default=now()`, asi que el valor no existe en
        Python hasta que la base lo devuelve. Esta prueba fija que el alta NO
        necesita un `refresh()` explicito —SQLAlchemy 2 los recupera con
        RETURNING en el propio INSERT— y que por tanto la respuesta nunca sale
        con nulos ni dispara una carga perezosa fuera del contexto asincrono.
        """
        creada = await crear(api, admin_csrf)

        assert creada["created_at"], "el alta devolvio created_at vacio"
        assert creada["updated_at"], "el alta devolvio updated_at vacio"

    async def test_el_talonario_v2_no_mueve_el_de_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Agotar correlativos de un motor no puede gastar los del otro."""
        antes = await db_session.scalar(
            text("SELECT current_value FROM document_sequences WHERE sequence_type = 'QUOTE'")
        )
        for _ in range(3):
            await crear(api, admin_csrf)
        despues = await db_session.scalar(
            text("SELECT current_value FROM document_sequences WHERE sequence_type = 'QUOTE'")
        )
        v2 = await db_session.scalar(
            text("SELECT current_value FROM document_sequences WHERE sequence_type = 'QUOTE_V2'")
        )
        assert antes == despues == 0
        assert v2 == 3

    async def test_dos_cotizaciones_no_comparten_codigo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        primera = await crear(api, admin_csrf)
        segunda = await crear(api, admin_csrf)
        assert primera["code"] != segunda["code"]

    async def test_por_menor_es_el_valor_por_defecto(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El tipo de produccion lo elige la persona; el sistema no lo cambia."""
        assert (await crear(api, admin_csrf))["production_type"] == "RETAIL"

    async def test_se_puede_declarar_por_mayor(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear(api, admin_csrf, production_type="WHOLESALE")
        assert creada["production_type"] == "WHOLESALE"

    async def test_el_cliente_se_congela_al_crear(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cliente = await crear_tercero(api, admin_csrf, "Cliente V2", "CLIENT")

        creada = await crear(api, admin_csrf, customer_id=cliente["id"])
        assert creada["customer_name"] == "Cliente V2"

    async def test_un_proveedor_no_puede_ser_el_cliente(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La misma exigencia que el Cotizador historico, no una mas laxa.

        Un proveedor puro colado como cliente termina impreso en el PDF que se
        envia al cliente. Que V2 sea un motor nuevo no lo autoriza a aceptar lo
        que el viejo rechaza.
        """
        proveedor = await crear_tercero(api, admin_csrf, "Proveedor V2", "SUPPLIER")

        response = await api.post(
            V2,
            json={"customer_id": proveedor["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "V2_CUSTOMER_ROLE_REQUIRED"

    async def test_un_tercero_con_rol_mixto_si_puede_ser_el_cliente(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        mixto = await crear_tercero(api, admin_csrf, "Cliente y proveedor", "BOTH")

        creada = await crear(api, admin_csrf, customer_id=mixto["id"])
        assert creada["customer_name"] == "Cliente y proveedor"

    async def test_un_cliente_archivado_no_revive_por_la_puerta_de_atras(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cliente = await crear_tercero(api, admin_csrf, "Cliente archivado", "CLIENT")
        baja = await api.put(
            f"{PARTNERS}/{cliente['id']}",
            json={"name": "Cliente archivado", "role": "CLIENT", "active": False},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text

        response = await api.post(
            V2,
            json={"customer_id": cliente["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "V2_CUSTOMER_NOT_FOUND"

    async def test_el_alta_queda_auditada(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Un documento comercial sin rastro de quien lo creo no es auditable.

        La entidad es propia —`v2_quotation`, no `quotation`—: son documentos
        de motores distintos y mezclarlas haria que el historial de uno
        apareciera dentro del otro.
        """
        creada = await crear(api, admin_csrf)

        fila = (
            await db_session.execute(
                text(
                    "SELECT entity_type, action, user_display_name FROM audit_events "
                    "WHERE entity_type = 'v2_quotation' AND entity_id = :id"
                ),
                {"id": str(creada["id"])},
            )
        ).one()
        assert fila.action == "CREATE"
        assert fila.user_display_name

    async def test_un_cliente_inexistente_da_404(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await api.post(
            V2, json={"customer_id": 99999}, headers={"X-CSRF-Token": admin_csrf}
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "V2_CUSTOMER_NOT_FOUND"


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------
class TestLectura:
    async def test_se_lee_por_id(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        creada = await crear(api, admin_csrf)
        leida = await api.get(f"{V2}/{creada['id']}")
        assert leida.status_code == 200
        assert leida.json()["code"] == creada["code"]

    async def test_el_listado_devuelve_lo_creado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        await crear(api, admin_csrf)
        await crear(api, admin_csrf)
        pagina = (await api.get(V2)).json()
        assert pagina["total"] == 2
        assert {item["pricing_engine_version"] for item in pagina["items"]} == {"V2"}

    async def test_el_listado_filtra_por_estado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        await crear(api, admin_csrf)
        assert (await api.get(V2, params={"status": "DRAFT"})).json()["total"] == 1
        assert (await api.get(V2, params={"status": "CONFIRMED"})).json()["total"] == 0

    async def test_un_id_que_no_existe_da_404(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        response = await api.get(f"{V2}/4242")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "V2_QUOTATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Aislamiento entre motores
# ---------------------------------------------------------------------------
class TestAislamiento:
    async def test_una_cotizacion_v2_no_aparece_en_el_listado_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        await crear(api, admin_csrf)
        legacy = await api.get(LEGACY)
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["total"] == 0

    async def test_la_ruta_v2_no_resuelve_ids_de_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Mismo id, tablas distintas: pedir por V2 nunca trae un documento viejo.

        Se inserta la fila Legacy directamente porque lo que se comprueba es la
        frontera de datos, no el flujo de alta del Cotizador historico.
        """
        await db_session.execute(
            text(
                "INSERT INTO quotations "
                "(id, code, status, source_fingerprint, "
                " commercial_factor_default_snapshot, commercial_factor) "
                "VALUES (1, 'CTZ-2026-000001', 'DRAFT', 'huella', 3, 3)"
            )
        )
        await db_session.commit()

        creada = await crear(api, admin_csrf)
        assert creada["id"] == 1, "los ids de las dos tablas son independientes"

        v2 = (await api.get(f"{V2}/1")).json()
        assert v2["code"] == creada["code"]
        assert v2["code"] != "CTZ-2026-000001"

    async def test_las_cotizaciones_historicas_quedan_selladas_como_legacy(
        self, db_session: AsyncSession
    ) -> None:
        # Sin nombrar `pricing_engine_version`: lo que se comprueba es que el
        # sello lo pone la base, igual que se lo puso a las filas que ya
        # existian cuando corrio la migracion.
        await db_session.execute(
            text(
                "INSERT INTO quotations "
                "(code, status, source_fingerprint, "
                " commercial_factor_default_snapshot, commercial_factor) "
                "VALUES ('CTZ-2026-000002', 'DRAFT', 'huella', 3, 3)"
            )
        )
        await db_session.commit()
        motor = await db_session.scalar(
            text("SELECT pricing_engine_version FROM quotations WHERE code = 'CTZ-2026-000002'")
        )
        assert motor == "LEGACY"

    async def test_la_tabla_legacy_rechaza_el_motor_v2(self, db_session: AsyncSession) -> None:
        with pytest.raises(IntegrityError):
            await db_session.execute(
                text(LEGACY_INSERT), {"code": "CTZ-2026-000003", "engine": "V2"}
            )
            await db_session.commit()
        await db_session.rollback()

    async def test_la_tabla_v2_rechaza_el_motor_legacy(self, db_session: AsyncSession) -> None:
        with pytest.raises(IntegrityError):
            await db_session.execute(
                text(
                    "INSERT INTO v2_quotations (code, pricing_engine_version) "
                    "VALUES ('CTZ-V2-2026-000001', 'LEGACY')"
                )
            )
            await db_session.commit()
        await db_session.rollback()

    async def test_crear_en_v2_no_escribe_en_la_tabla_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Ninguna peticion V2 puede acabar ejecutando un servicio Legacy."""
        await crear(api, admin_csrf)
        assert await db_session.scalar(text("SELECT count(*) FROM quotations")) == 0
        assert await db_session.scalar(text("SELECT count(*) FROM quotation_items")) == 0
