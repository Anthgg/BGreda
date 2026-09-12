"""Fase 010G — cambiar la CABECERA de un borrador V2 contra PostgreSQL real.

Esta ruta existe porque el flujo de siete pasos deja volver atras: quien esta
eligiendo el horno puede darse cuenta de que el cliente esta mal y regresar al
primer paso. Sin ella el unico remedio era abrir otra cotizacion.

Lo que se comprueba, por orden de gravedad:

1. **navegar no es editar.** La semantica es parcial: lo que no viaja en el
   cuerpo se conserva. Abrir el paso uno para mirar no puede reescribir la
   moneda ni el tipo de produccion;
2. **el tipo de produccion mueve el horno SOLO si nadie lo eligio.** Cambiar de
   por menor a por mayor reemplaza el horno sugerido; si alguien ya habia
   elegido otro a mano, esa decision manda. Es la regla de 010E: el sistema
   recomienda y no decide;
3. **la moneda se recongela entera.** Codigo, simbolo y tipo de cambio son un
   solo dato en tres columnas, y la base exige que sean coherentes;
4. **el cliente se valida igual que en el alta.** Un proveedor puro o un
   tercero archivado no entran por la puerta de atras;
5. **una emitida no cambia de cabecera.** Ya comprometio un precio con un
   cliente concreto y en una moneda concreta;
6. **cambiar la cabecera recalcula.** La moneda y el tipo de produccion mueven
   el horno y, con el, cada precio unitario: el resumen no puede quedarse
   ensenando cifras de antes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

V2 = "/api/v1/quotations-v2"
PARTNERS = "/api/v1/partners"
KILNS = "/api/v1/kilns"
SETTINGS = "/api/v1/quoter-v2/settings"

#: Los dos hornos del Excel aprobado. El chico es el que sugiere Por menor y el
#: grande el de Por mayor, que es justo lo que estas pruebas mueven.
CHICO = 17000
GRANDE = 200000


async def crear_horno(api: httpx.AsyncClient, csrf: str, nombre: str, capacidad: int) -> int:
    response = await api.post(
        KILNS,
        json={"name": nombre, "capacity_volume_cm3": str(capacidad)},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    horno = dict(response.json())
    for indice, tipo in enumerate(("LOW", "HIGH")):
        tarifa = await api.put(
            f"{SETTINGS}/kiln-rates/{horno['id']}/{tipo}",
            json={
                "gas_cost": ("35", "70")[indice],
                "external_rate": ("200", "250")[indice],
                "student_rate": ("90", "180")[indice],
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert tarifa.status_code == 200, tarifa.text
    return int(horno["id"])


async def configurar_hornos(
    api: httpx.AsyncClient, csrf: str, *, retail: int | None = None, wholesale: int | None = None
) -> None:
    """Deja dicho que horno sugiere cada tipo de produccion.

    Lo dice la configuracion y no una heuristica sobre el nombre o sobre un
    umbral de capacidad que nadie definio.
    """
    actual = (await api.get(SETTINGS)).json()["settings"]
    payload: dict[str, Any] = {"expected_version": actual["version"]}
    if retail is not None:
        payload["retail_kiln_id"] = retail
    if wholesale is not None:
        payload["wholesale_kiln_id"] = wholesale
    respuesta = await api.put(SETTINGS, json=payload, headers={"X-CSRF-Token": csrf})
    assert respuesta.status_code == 200, respuesta.text


async def crear_tercero(api: httpx.AsyncClient, csrf: str, nombre: str, rol: str) -> int:
    response = await api.post(
        PARTNERS, json={"name": nombre, "role": rol}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def crear(api: httpx.AsyncClient, csrf: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "Cabecera 010G"}
    payload.update(overrides)
    response = await api.post(V2, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def actualizar(
    api: httpx.AsyncClient, csrf: str, quotation_id: int, **campos: Any
) -> httpx.Response:
    return await api.put(f"{V2}/{quotation_id}", json=campos, headers={"X-CSRF-Token": csrf})


async def emitir(db_session: AsyncSession, quotation_id: int) -> None:
    await db_session.execute(
        text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
        {"id": quotation_id},
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    async def test_sin_sesion_no_se_cambia(self, api: httpx.AsyncClient) -> None:
        """403 y no 401: sin sesion tampoco hay token CSRF, y ese filtro va antes.

        Lo que importa es que no entre, no cual de las dos puertas lo para.
        """
        assert (await api.put(f"{V2}/1", json={"name": "x"})).status_code == 403
        assert (await api.get(f"{V2}/1")).status_code == 401

    async def test_cambiar_la_cabecera_es_administracion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Elegir a quien se cotiza no es ejecutar el trabajo."""
        creada = await crear(api, admin_csrf)
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        respuesta = await actualizar(api, csrf, creada["id"], name="otro")

        assert respuesta.status_code == 403


# ---------------------------------------------------------------------------
# Semantica parcial: navegar no es editar
# ---------------------------------------------------------------------------
class TestSemanticaParcial:
    async def test_un_cuerpo_vacio_no_cambia_nada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Abrir el paso uno para mirar no puede reescribir la cotizacion."""
        creada = await crear(api, admin_csrf, production_type="WHOLESALE")

        respuesta = await actualizar(api, admin_csrf, creada["id"])

        assert respuesta.status_code == 200, respuesta.text
        despues = respuesta.json()
        assert despues["production_type"] == "WHOLESALE"
        assert despues["name"] == creada["name"]
        assert despues["currency_code"] == creada["currency_code"]

    async def test_cambiar_el_nombre_no_toca_el_tipo_de_produccion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear(api, admin_csrf, production_type="WHOLESALE")

        respuesta = await actualizar(api, admin_csrf, creada["id"], name="Pedido de la feria")

        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["name"] == "Pedido de la feria"
        assert respuesta.json()["production_type"] == "WHOLESALE"

    async def test_un_nombre_en_blanco_lo_retira(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """A diferencia de un importe, un texto vacio no se confunde con cero."""
        creada = await crear(api, admin_csrf, name="Con nombre")

        respuesta = await actualizar(api, admin_csrf, creada["id"], name="   ")

        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["name"] is None


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------
class TestCliente:
    async def test_se_asigna_y_se_congela_su_nombre(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear(api, admin_csrf)
        cliente = await crear_tercero(api, admin_csrf, "Cliente de prueba", "CLIENT")

        respuesta = await actualizar(api, admin_csrf, creada["id"], customer_id=cliente)

        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["customer_id"] == cliente
        assert respuesta.json()["customer_name"] == "Cliente de prueba"

    async def test_en_nulo_lo_retira(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        """Un borrador puede volver a quedarse sin cliente: se esta armando."""
        cliente = await crear_tercero(api, admin_csrf, "Cliente que se va", "CLIENT")
        creada = await crear(api, admin_csrf, customer_id=cliente)

        respuesta = await actualizar(api, admin_csrf, creada["id"], customer_id=None)

        assert respuesta.status_code == 200, respuesta.text
        assert respuesta.json()["customer_id"] is None
        assert respuesta.json()["customer_name"] is None

    async def test_un_proveedor_puro_no_es_cliente(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Acabaria en el PDF que se envia al cliente."""
        creada = await crear(api, admin_csrf)
        proveedor = await crear_tercero(api, admin_csrf, "Solo proveedor", "SUPPLIER")

        respuesta = await actualizar(api, admin_csrf, creada["id"], customer_id=proveedor)

        assert respuesta.status_code == 422
        assert respuesta.json()["error"]["code"] == "V2_CUSTOMER_ROLE_REQUIRED"

    async def test_un_tercero_que_no_existe_no_entra(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear(api, admin_csrf)

        respuesta = await actualizar(api, admin_csrf, creada["id"], customer_id=999_999)

        assert respuesta.status_code == 404
        assert respuesta.json()["error"]["code"] == "V2_CUSTOMER_NOT_FOUND"


# ---------------------------------------------------------------------------
# El tipo de produccion mueve el horno, pero solo el sugerido
# ---------------------------------------------------------------------------
class TestTipoDeProduccion:
    async def test_cambiarlo_mueve_el_horno_sugerido(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        chico = await crear_horno(api, admin_csrf, "Chico 010G a", CHICO)
        grande = await crear_horno(api, admin_csrf, "Grande 010G a", GRANDE)
        await configurar_hornos(api, admin_csrf, retail=chico, wholesale=grande)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        assert (await api.get(f"{V2}/{creada['id']}/firing")).json()["kiln_id"] == chico

        respuesta = await actualizar(api, admin_csrf, creada["id"], production_type="WHOLESALE")

        assert respuesta.status_code == 200, respuesta.text
        quema = (await api.get(f"{V2}/{creada['id']}/firing")).json()
        assert quema["kiln_id"] == grande

    async def test_no_pisa_un_horno_elegido_a_mano(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La regla de 010E: el sistema recomienda y no decide.

        Quien eligio el horno grande para una produccion por menor lo hizo a
        proposito. Cambiar el tipo no puede deshacerlo sin que nadie lo pida.
        """
        chico = await crear_horno(api, admin_csrf, "Chico 010G b", CHICO)
        grande = await crear_horno(api, admin_csrf, "Grande 010G b", GRANDE)
        await configurar_hornos(api, admin_csrf, retail=chico, wholesale=grande)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        elegido = await api.put(
            f"{V2}/{creada['id']}/firing",
            json={"kiln_id": grande},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert elegido.status_code == 200, elegido.text

        await actualizar(api, admin_csrf, creada["id"], production_type="WHOLESALE")

        quema = (await api.get(f"{V2}/{creada['id']}/firing")).json()
        assert quema["kiln_id"] == grande, "el horno elegido a mano manda sobre el sugerido"

    async def test_reenviar_el_mismo_tipo_no_mueve_el_horno(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La presencia de una clave no es un cambio."""
        chico = await crear_horno(api, admin_csrf, "Chico 010G c", CHICO)
        grande = await crear_horno(api, admin_csrf, "Grande 010G c", GRANDE)
        await configurar_hornos(api, admin_csrf, retail=chico, wholesale=grande)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        await api.put(
            f"{V2}/{creada['id']}/firing",
            json={"kiln_id": grande},
            headers={"X-CSRF-Token": admin_csrf},
        )

        await actualizar(api, admin_csrf, creada["id"], production_type="RETAIL")

        assert (await api.get(f"{V2}/{creada['id']}/firing")).json()["kiln_id"] == grande

    async def test_la_cantidad_de_piezas_no_cambia_el_tipo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La confusion que cerro 010E: pasarse de capacidad avisa, no convierte."""
        chico = await crear_horno(api, admin_csrf, "Chico 010G d", CHICO)
        await configurar_hornos(api, admin_csrf, retail=chico)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        alta = await api.post(
            f"{V2}/{creada['id']}/products",
            json={
                "product_name": "Muchas piezas",
                "quantity": 400,
                "length_cm": "20",
                "width_cm": "20",
                "height_cm": "20",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert alta.status_code == 201, alta.text

        leida = (await api.get(f"{V2}/{creada['id']}")).json()

        assert leida["production_type"] == "RETAIL"
        quema = (await api.get(f"{V2}/{creada['id']}/firing")).json()
        assert "V2_FIRING_RETAIL_OVER_CAPACITY" in quema["warnings"]

    async def test_un_solo_horno_no_pierde_las_tarifas_pactadas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El taller con un horno cambia de tipo sin perder lo acordado.

        Si la configuracion sugiere el MISMO horno para por menor y por mayor
        —un taller con una sola maquina—, cambiar el tipo no mueve nada. Tirar
        ahi las tarifas pactadas seria destruir un acuerdo sin que el horno
        hubiera cambiado siquiera.
        """
        unico = await crear_horno(api, admin_csrf, "Unico 010G", CHICO)
        await configurar_hornos(api, admin_csrf, retail=unico, wholesale=unico)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        pactada = await api.put(
            f"{V2}/{creada['id']}/firing",
            json={"commercial_rate_low_override": "333"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert pactada.status_code == 200, pactada.text
        assert pactada.json()["commercial_low_is_override"] is True

        await actualizar(api, admin_csrf, creada["id"], production_type="WHOLESALE")

        quema = (await api.get(f"{V2}/{creada['id']}/firing")).json()
        assert quema["kiln_id"] == unico
        assert Decimal(quema["commercial_rate_low"]) == Decimal(333), (
            "la tarifa pactada tiene que sobrevivir a un cambio que no movio el horno"
        )
        assert quema["commercial_low_is_override"] is True

    async def test_sin_horno_sugerido_para_el_tipo_nuevo_se_conserva_el_que_habia(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Quedarse sin horno costearia la quema en cero.

        Si la configuracion no nombra horno para por mayor, cambiar a por mayor
        no puede dejar la cotizacion huerfana: es peor que conservar un default
        que al menos existe y que quien cotiza puede cambiar a mano.
        """
        chico = await crear_horno(api, admin_csrf, "Chico 010G g", CHICO)
        await configurar_hornos(api, admin_csrf, retail=chico)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        assert (await api.get(f"{V2}/{creada['id']}/firing")).json()["kiln_id"] == chico

        await actualizar(api, admin_csrf, creada["id"], production_type="WHOLESALE")

        assert (await api.get(f"{V2}/{creada['id']}/firing")).json()["kiln_id"] == chico


# ---------------------------------------------------------------------------
# Moneda
# ---------------------------------------------------------------------------
class TestMoneda:
    async def test_cambiarla_recongela_las_tres_columnas(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Codigo, simbolo y tipo de cambio son un dato en tres columnas."""
        creada = await crear(api, admin_csrf)

        respuesta = await actualizar(
            api, admin_csrf, creada["id"], currency_code="USD", exchange_rate="3.75"
        )

        assert respuesta.status_code == 200, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["currency_code"] == "USD"
        assert cuerpo["currency_symbol"] is not None
        assert Decimal(cuerpo["exchange_rate"]) == Decimal("3.75")

        fila = (
            await db_session.execute(
                text(
                    "SELECT currency_code_snapshot, currency_symbol_snapshot, "
                    "exchange_rate_snapshot FROM v2_quotations WHERE id = :id"
                ),
                {"id": creada["id"]},
            )
        ).one()
        assert fila[0] == "USD"
        assert fila[1] is not None
        assert Decimal(fila[2]) == Decimal("3.75")

    async def test_el_tipo_de_cambio_en_nulo_vuelve_al_de_la_casa(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Presente y en nulo RETIRA el acuerdo, como en toda la familia V2.

        Sin esto no habia forma de deshacer un tipo de cambio pactado y volver
        al de la configuracion: el `or` de Python confundia el nulo explicito
        con «no lo mandaron» y el acuerdo se quedaba puesto para siempre.
        """
        creada = await crear(api, admin_csrf)
        pactado = await actualizar(
            api, admin_csrf, creada["id"], currency_code="USD", exchange_rate="4.20"
        )
        assert pactado.status_code == 200, pactado.text
        assert Decimal(pactado.json()["exchange_rate"]) == Decimal("4.20")

        retirado = await actualizar(api, admin_csrf, creada["id"], exchange_rate=None)

        assert retirado.status_code == 200, retirado.text
        tasa = retirado.json()["exchange_rate"]
        assert tasa is not None, "en moneda extranjera siempre tiene que haber una tasa"
        assert Decimal(tasa) != Decimal("4.20"), "el acuerdo tenia que retirarse"

    async def test_no_mandar_el_tipo_de_cambio_lo_conserva(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Ausente conserva. La otra mitad de la regla, y la que evita que
        cambiar el nombre borre una tasa pactada."""
        creada = await crear(api, admin_csrf)
        await actualizar(api, admin_csrf, creada["id"], currency_code="USD", exchange_rate="4.20")

        respuesta = await actualizar(api, admin_csrf, creada["id"], name="otro nombre")

        assert respuesta.status_code == 200, respuesta.text
        assert Decimal(respuesta.json()["exchange_rate"]) == Decimal("4.20")

    async def test_un_tipo_de_cambio_no_positivo_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        creada = await crear(api, admin_csrf)

        respuesta = await actualizar(
            api, admin_csrf, creada["id"], currency_code="USD", exchange_rate="0"
        )

        assert respuesta.status_code == 422


# ---------------------------------------------------------------------------
# Solo borradores
# ---------------------------------------------------------------------------
class TestSoloBorradores:
    async def test_una_emitida_no_cambia_de_cliente(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Ya comprometio un precio con un cliente concreto."""
        creada = await crear(api, admin_csrf)
        cliente = await crear_tercero(api, admin_csrf, "Cliente tardio", "CLIENT")
        await emitir(db_session, creada["id"])

        respuesta = await actualizar(api, admin_csrf, creada["id"], customer_id=cliente)

        assert respuesta.status_code == 409
        assert respuesta.json()["error"]["code"] == "V2_QUOTATION_NOT_EDITABLE"

    async def test_una_que_no_existe_da_404(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        assert (await actualizar(api, admin_csrf, 999_999, name="x")).status_code == 404


# ---------------------------------------------------------------------------
# Cambiar la cabecera recalcula
# ---------------------------------------------------------------------------
class TestRecalculo:
    async def test_mover_el_horno_cambia_el_costo_de_la_quema(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sin esto el resumen ensenaria el precio del horno anterior."""
        chico = await crear_horno(api, admin_csrf, "Chico 010G e", CHICO)
        grande = await crear_horno(api, admin_csrf, "Grande 010G e", GRANDE)
        await configurar_hornos(api, admin_csrf, retail=chico, wholesale=grande)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        alta = await api.post(
            f"{V2}/{creada['id']}/products",
            json={
                "product_name": "Plato",
                "quantity": 100,
                "length_cm": "20",
                "width_cm": "20",
                "height_cm": "20",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert alta.status_code == 201, alta.text
        antes = (await api.get(f"{V2}/{creada['id']}/firing")).json()
        # 800.000 cm3 en un horno de 17.000 son muchas hornadas.
        assert antes["firing_count"] > 1

        await actualizar(api, admin_csrf, creada["id"], production_type="WHOLESALE")

        despues = (await api.get(f"{V2}/{creada['id']}/firing")).json()
        assert despues["firing_count"] < antes["firing_count"]
        assert Decimal(despues["commercial_total"]) < Decimal(antes["commercial_total"])

    async def test_cambiar_el_tipo_de_cliente_no_cambia_el_gas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El gas que se quema no depende de a quien se le cobre."""
        chico = await crear_horno(api, admin_csrf, "Chico 010G f", CHICO)
        await configurar_hornos(api, admin_csrf, retail=chico)
        creada = await crear(api, admin_csrf, production_type="RETAIL")
        alta = await api.post(
            f"{V2}/{creada['id']}/products",
            json={
                "product_name": "Taza",
                "quantity": 10,
                "length_cm": "10",
                "width_cm": "10",
                "height_cm": "10",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert alta.status_code == 201, alta.text
        antes = (await api.get(f"{V2}/{creada['id']}/firing")).json()

        await actualizar(api, admin_csrf, creada["id"], customer_kind="STUDENT")

        despues = (await api.get(f"{V2}/{creada['id']}/firing")).json()
        assert Decimal(despues["gas_total"]) == Decimal(antes["gas_total"])
        assert Decimal(despues["commercial_total"]) < Decimal(antes["commercial_total"])

    async def test_queda_registrado_en_la_auditoria(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Cambiar de cliente sobre un precio en curso tiene que poder mirarse."""
        creada = await crear(api, admin_csrf)
        cliente = await crear_tercero(api, admin_csrf, "Cliente auditado", "CLIENT")

        await actualizar(api, admin_csrf, creada["id"], customer_id=cliente)

        registros = await db_session.scalar(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE entity_type = 'v2_quotation' AND entity_id = :id AND action = 'UPDATE'"
            ),
            {"id": str(creada["id"])},
        )
        assert registros and registros >= 1
