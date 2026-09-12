"""Fase 010D — mano de obra del Cotizador V2 contra PostgreSQL real.

Lo que aqui se comprueba, por orden de gravedad:

1. **el costo sale del trabajador, no de la tecnica.** «Torno = S/110» seria
   una constante escondida que deja de ser cierta en cuanto cambia un jornal;
2. **se cobran horas, no jornadas.** Tres horas de torno son tres horas, y una
   persona que hace tres tecnicas para el mismo pedido trabaja UNA jornada;
3. **el sistema avisa y no decide.** Diez horas en una jornada de ocho no
   generan recargo ni parten el trabajo solas: lo elige una persona;
4. **anadir gente suma costo y no resta plazo;**
5. el snapshot sobrevive a que cambie el maestro, y el override de una
   cotizacion no toca el maestro.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

WORKERS = "/api/v1/quoter-v2/workers"
TECHNIQUES = "/api/v1/quoter-v2/techniques"
V2 = "/api/v1/quotations-v2"

#: La jornada del taller, que siembra la configuracion de 010B.
JORNADA = Decimal(8)


async def crear_trabajador(
    api: httpx.AsyncClient, csrf: str, nombre: str, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": nombre,
        "worker_type": "INTERNAL",
        "daily_rate": "120",
    }
    payload.update(overrides)
    response = await api.post(WORKERS, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def crear_tecnica(
    api: httpx.AsyncClient, csrf: str, code: str, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "name": code.replace("-", " ").title(),
        "default_capacity_per_workday": "50",
    }
    payload.update(overrides)
    response = await api.post(TECHNIQUES, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def crear_cotizacion(api: httpx.AsyncClient, csrf: str) -> int:
    response = await api.post(
        V2, json={"name": "Mano de obra 010D"}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def anadir_tarea(
    api: httpx.AsyncClient, csrf: str, quotation_id: int, **campos: Any
) -> dict[str, Any]:
    response = await api.post(
        f"{V2}/{quotation_id}/labor", json=campos, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


async def pagina(api: httpx.AsyncClient, quotation_id: int) -> dict[str, Any]:
    response = await api.get(f"{V2}/{quotation_id}/labor")
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    async def test_sin_sesion_no_se_leen_los_trabajadores(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(WORKERS)).status_code == 401

    async def test_el_taller_no_ve_los_jornales(self, api: httpx.AsyncClient) -> None:
        """Un jornal es informacion de la remuneracion de una persona.

        Todo el Cotizador V2 es de administracion desde 010A. Abrir este
        maestro «para poder seleccionar» expondria cuanto cobra cada companero,
        que es justo lo que no hace falta para cotizar.
        """
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        assert (await api.get(WORKERS)).status_code == 403
        respuesta = await api.post(
            WORKERS,
            json={"name": "X", "worker_type": "INTERNAL", "daily_rate": "120"},
            headers={"X-CSRF-Token": csrf},
        )
        assert respuesta.status_code == 403


# ---------------------------------------------------------------------------
# Trabajadores: la tarifa es una consecuencia
# ---------------------------------------------------------------------------
class TestTrabajadores:
    async def test_la_tarifa_por_hora_sale_del_jornal_y_la_jornada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """S/120 en la jornada de 8 h del taller son S/15 la hora."""
        worker = await crear_trabajador(api, admin_csrf, "Celso")

        assert Decimal(worker["hourly_rate"]) == Decimal(15)
        assert Decimal(worker["effective_workday_hours"]) == JORNADA
        assert worker["workday_hours"] is None

    async def test_ciento_diez_entre_ocho_son_trece_setenta_y_cinco(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Ilustradora", daily_rate="110")

        assert Decimal(worker["hourly_rate"]) == Decimal("13.75")

    async def test_un_trabajador_interno_no_cuesta_cero(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Tener sueldo no hace que su tiempo valga cero.

        Es la regla que mas veces se propone romper. No saber cuanto cuesta una
        hora propia es la forma de descubrir tarde que una linea daba perdidas.
        """
        worker = await crear_trabajador(api, admin_csrf, "Interno", worker_type="INTERNAL")
        tecnica = await crear_tecnica(api, admin_csrf, "torno")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            final_hours_override="4",
        )

        assert Decimal(tarea["labor_cost"]) == Decimal(60)

    async def test_una_jornada_propia_gana_sobre_la_del_taller(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Media jornada: S/60 en 4 horas son los mismos S/15 la hora."""
        worker = await crear_trabajador(
            api, admin_csrf, "Media jornada", daily_rate="60", workday_hours="4"
        )

        assert Decimal(worker["hourly_rate"]) == Decimal(15)
        assert Decimal(worker["effective_workday_hours"]) == Decimal(4)

    async def test_un_trabajador_dado_de_baja_no_se_puede_asignar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Retirado")
        baja = await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": worker["version"], "active": False},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text
        tecnica = await crear_tecnica(api, admin_csrf, "torno-baja")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.post(
            f"{V2}/{cotizacion}/labor",
            json={"worker_id": worker["id"], "technique_id": tecnica["id"], "quantity": "10"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "V2_LABOR_RESOURCE_INACTIVE"

    async def test_subir_el_jornal_no_cambia_lo_ya_cotizado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Con aumento")
        tecnica = await crear_tecnica(api, admin_csrf, "torno-aumento")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
        )
        assert Decimal(tarea["labor_cost"]) == Decimal(120)

        subida = await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": worker["version"], "daily_rate": "200"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert subida.status_code == 200, subida.text

        cuerpo = await pagina(api, cotizacion)
        assert Decimal(cuerpo["items"][0]["hourly_rate"]) == Decimal(15)
        assert Decimal(cuerpo["items"][0]["labor_cost"]) == Decimal(120)

    async def test_dos_administradores_no_se_pisan_el_jornal(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Disputado")
        primero = await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": 1, "daily_rate": "160"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert primero.status_code == 200, primero.text

        segundo = await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": 1, "daily_rate": "120"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert segundo.status_code == 409
        assert segundo.json()["error"]["code"] == "V2_LABOR_VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# Tecnicas: rendimiento estandar
# ---------------------------------------------------------------------------
class TestTecnicas:
    async def test_cincuenta_piezas_por_jornada_son_seis_con_veinticinco_por_hora(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        tecnica = await crear_tecnica(api, admin_csrf, "vidriado")

        assert Decimal(tecnica["units_per_hour"]) == Decimal("6.25")

    async def test_setenta_y_cinco_piezas_son_doce_horas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El ejemplo aprobado de la fase."""
        worker = await crear_trabajador(api, admin_csrf, "Vidriador")
        tecnica = await crear_tecnica(api, admin_csrf, "vidriado-75")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="75",
        )

        assert Decimal(tarea["calculated_hours"]) == Decimal(12)
        assert Decimal(tarea["final_hours"]) == Decimal(12)
        assert Decimal(tarea["labor_cost"]) == Decimal(180)

    async def test_la_tecnica_no_guarda_precio(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Si lo guardara, «torno = S/110» volveria por la puerta de atras."""
        tecnica = await crear_tecnica(api, admin_csrf, "sin-precio")

        for prohibido in ("unit_price", "price", "cost", "daily_rate", "hourly_rate"):
            assert prohibido not in tecnica, f"la tecnica expone {prohibido}"

    async def test_el_estandar_no_cambia_porque_alguien_rinda_mas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Acordar mas horas en un encargo no reescribe el catalogo.

        El sistema no aprende de la productividad de nadie: si un dia se hacen
        70 piezas donde el estandar dice 50, el estandar sigue diciendo 50
        hasta que una persona decida cambiarlo.
        """
        worker = await crear_trabajador(api, admin_csrf, "Rapido")
        tecnica = await crear_tecnica(api, admin_csrf, "aprendizaje")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="70",
            final_hours_override="6",
        )

        actual = (await api.get(TECHNIQUES)).json()["items"]
        fila = next(t for t in actual if t["id"] == tecnica["id"])
        assert Decimal(fila["default_capacity_per_workday"]) == Decimal(50)
        assert Decimal(fila["units_per_hour"]) == Decimal("6.25")

    async def test_una_tecnica_retirada_no_se_puede_asignar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        tecnica = await crear_tecnica(api, admin_csrf, "retirada")
        baja = await api.put(
            f"{TECHNIQUES}/{tecnica['id']}",
            json={"expected_version": tecnica["version"], "active": False},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text
        worker = await crear_trabajador(api, admin_csrf, "Sin tecnica")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.post(
            f"{V2}/{cotizacion}/labor",
            json={"worker_id": worker["id"], "technique_id": tecnica["id"], "quantity": "10"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Una persona, varias tecnicas: la jornada es una
# ---------------------------------------------------------------------------
class TestJornadaCompartida:
    async def test_la_misma_persona_puede_hacer_varias_tecnicas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Polivalente")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        for code, horas in (("torno-p", "3"), ("asas-p", "2"), ("vidriado-p", "3")):
            tecnica = await crear_tecnica(api, admin_csrf, code)
            await anadir_tarea(
                api,
                admin_csrf,
                cotizacion,
                worker_id=worker["id"],
                technique_id=tecnica["id"],
                final_hours_override=horas,
            )

        cuerpo = await pagina(api, cotizacion)

        assert len(cuerpo["items"]) == 3
        assert len(cuerpo["workday_load"]) == 1

    async def test_tres_tecnicas_en_una_jornada_no_son_tres_jornadas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El fallo caro de la fase: 360 en vez de 120, y el importe es creible.

        3 h de torno + 2 h de asas + 3 h de vidriado son 8 horas de una persona
        que cobra S/120 la jornada. Eso cuesta S/120, no S/360.
        """
        worker = await crear_trabajador(api, admin_csrf, "Jornada unica")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        for code, horas in (("torno-j", "3"), ("asas-j", "2"), ("vidriado-j", "3")):
            tecnica = await crear_tecnica(api, admin_csrf, code)
            await anadir_tarea(
                api,
                admin_csrf,
                cotizacion,
                worker_id=worker["id"],
                technique_id=tecnica["id"],
                final_hours_override=horas,
            )

        cuerpo = await pagina(api, cotizacion)

        assert Decimal(cuerpo["labor_cost"]) == Decimal(120)
        carga = cuerpo["workday_load"][0]
        assert Decimal(carga["assigned_hours"]) == JORNADA
        assert carga["exceeds_workday"] is False

    async def test_ocho_horas_exactas_no_alertan(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un aviso que salta en la jornada normal deja de leerse."""
        worker = await crear_trabajador(api, admin_csrf, "Justo")
        tecnica = await crear_tecnica(api, admin_csrf, "justo")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
        )

        assert Decimal(tarea["final_hours"]) == JORNADA
        assert "V2_LABOR_WORKDAY_EXCEEDED" not in tarea["warnings"]

    async def test_diez_horas_alertan_y_no_traen_recargo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Avisar no es decidir, y tampoco es cobrar mas.

        Diez horas cuestan diez horas: ni recargo nocturno, ni hora extra, ni
        multiplicador. La solucion la elige una persona.
        """
        worker = await crear_trabajador(api, admin_csrf, "Largo")
        tecnica = await crear_tecnica(api, admin_csrf, "largo")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            final_hours_override="10",
        )

        assert "V2_LABOR_WORKDAY_EXCEEDED" in tarea["warnings"]
        assert Decimal(tarea["labor_cost"]) == Decimal(150)

    async def test_el_aviso_mira_el_total_y_no_cada_tarea(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Tres tareas de tres horas no dicen nada por separado y juntas no caben."""
        worker = await crear_trabajador(api, admin_csrf, "Acumulado")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        ultima: dict[str, Any] = {}
        for code in ("a-tres", "b-tres", "c-tres"):
            tecnica = await crear_tecnica(api, admin_csrf, code)
            ultima = await anadir_tarea(
                api,
                admin_csrf,
                cotizacion,
                worker_id=worker["id"],
                technique_id=tecnica["id"],
                final_hours_override="3",
            )

        assert "V2_LABOR_WORKDAY_EXCEEDED" in ultima["warnings"]
        cuerpo = await pagina(api, cotizacion)
        assert Decimal(cuerpo["workday_load"][0]["assigned_hours"]) == Decimal(9)

    async def test_dos_personas_en_paralelo_no_suman_dias(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sumar las horas de todos y dividir daria el doble de dias.

        Dos personas trabajando ocho horas cada una son un dia, no dos: el
        minimo se toma por trabajador, porque trabajan a la vez.
        """
        cotizacion = await crear_cotizacion(api, admin_csrf)
        for nombre, code in (("Uno", "par-a"), ("Dos", "par-b")):
            worker = await crear_trabajador(api, admin_csrf, nombre)
            tecnica = await crear_tecnica(api, admin_csrf, code)
            await anadir_tarea(
                api,
                admin_csrf,
                cotizacion,
                worker_id=worker["id"],
                technique_id=tecnica["id"],
                final_hours_override="8",
            )

        cuerpo = await pagina(api, cotizacion)

        assert cuerpo["suggested_work_days"] == 1


# ---------------------------------------------------------------------------
# Dias efectivos: la decision es de una persona
# ---------------------------------------------------------------------------
class TestPlanificacion:
    async def test_diez_horas_sugieren_dos_dias(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Diez horas")
        tecnica = await crear_tecnica(api, admin_csrf, "diez")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            final_hours_override="10",
        )

        cuerpo = await pagina(api, cotizacion)

        assert cuerpo["suggested_work_days"] == 2
        # Y nadie ha decidido todavia: NULL no es cero.
        assert cuerpo["effective_work_days"] is None

    @pytest.mark.parametrize("dias", [1, 2])
    async def test_quien_planifica_decide_los_dias(
        self, api: httpx.AsyncClient, admin_csrf: str, dias: int
    ) -> None:
        """Diez horas caben en un dia largo o en dos dias. Las dos son validas.

        El sistema sugiere y no impone: 010F cobrara el espacio por lo que se
        decida aqui.
        """
        worker = await crear_trabajador(api, admin_csrf, f"Planificado {dias}")
        tecnica = await crear_tecnica(api, admin_csrf, f"plan-{dias}")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            final_hours_override="10",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/planning",
            json={"effective_work_days": dias},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        assert response.json()["effective_work_days"] == dias
        # La sugerencia no cambia porque alguien decida otra cosa.
        assert response.json()["suggested_work_days"] == 2


# ---------------------------------------------------------------------------
# Personal adicional
# ---------------------------------------------------------------------------
class TestPersonalAdicional:
    async def test_anadir_personal_suma_costo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Externo a S/160 la jornada son S/20 la hora; 6 horas, S/120."""
        titular = await crear_trabajador(api, admin_csrf, "Titular")
        externo = await crear_trabajador(
            api, admin_csrf, "Externo", worker_type="EXTERNAL", daily_rate="160"
        )
        assert Decimal(externo["hourly_rate"]) == Decimal(20)
        tecnica = await crear_tecnica(api, admin_csrf, "apoyo")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=titular["id"],
            technique_id=tecnica["id"],
            final_hours_override="8",
        )
        antes = Decimal((await pagina(api, cotizacion))["labor_cost"])

        extra = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=externo["id"],
            technique_id=tecnica["id"],
            final_hours_override="6",
            is_additional_personnel=True,
        )

        assert extra["is_additional_personnel"] is True
        assert Decimal(extra["labor_cost"]) == Decimal(120)
        assert Decimal((await pagina(api, cotizacion))["labor_cost"]) == antes + Decimal(120)

    async def test_anadir_personal_no_reduce_el_plazo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Que dos personas tarden la mitad es una decision, no una division.

        Suponerlo prometeria al cliente una fecha que el taller no acordo.
        """
        titular = await crear_trabajador(api, admin_csrf, "Titular plazo")
        tecnica = await crear_tecnica(api, admin_csrf, "plazo")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=titular["id"],
            technique_id=tecnica["id"],
            final_hours_override="16",
        )
        dias_antes = (await pagina(api, cotizacion))["suggested_work_days"]
        assert dias_antes == 2

        externo = await crear_trabajador(
            api, admin_csrf, "Refuerzo", worker_type="EXTERNAL", daily_rate="160"
        )
        await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=externo["id"],
            technique_id=tecnica["id"],
            final_hours_override="4",
            is_additional_personnel=True,
        )

        cuerpo = await pagina(api, cotizacion)
        # El titular sigue teniendo 16 horas por delante: nadie se las quito.
        assert cuerpo["suggested_work_days"] == 2


# ---------------------------------------------------------------------------
# Overrides: lo pactado no se borra solo
# ---------------------------------------------------------------------------
class TestOverrides:
    async def test_una_tarifa_acordada_no_toca_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Acordado")
        tecnica = await crear_tecnica(api, admin_csrf, "acuerdo")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            final_hours_override="8",
            hourly_rate_override="17.5",
        )

        assert tarea["rate_overridden"] is True
        assert Decimal(tarea["labor_cost"]) == Decimal(140)
        maestro = (await api.get(WORKERS)).json()["items"]
        fila = next(w for w in maestro if w["id"] == worker["id"])
        assert Decimal(fila["daily_rate"]) == Decimal(120)
        assert Decimal(fila["hourly_rate"]) == Decimal(15)

    async def test_el_acuerdo_sobrevive_a_cambiar_la_cantidad(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El aprendizaje caro de 010C, aplicado desde el principio.

        La pantalla manda solo lo que cambio. Leer el override con
        `data.get(...)` devolveria `None`, indistinguible de «quitalo», y la
        tarifa acordada con el cliente volveria a la del maestro en silencio.
        """
        worker = await crear_trabajador(api, admin_csrf, "Pactado")
        tecnica = await crear_tecnica(api, admin_csrf, "pacto")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
            hourly_rate_override="17.5",
        )
        assert Decimal(tarea["hourly_rate"]) == Decimal("17.5")

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={"quantity": "100"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["rate_overridden"] is True
        assert Decimal(cuerpo["hourly_rate"]) == Decimal("17.5")
        assert Decimal(cuerpo["final_hours"]) == Decimal(16)
        assert Decimal(cuerpo["labor_cost"]) == Decimal(280)

    async def test_retirar_el_acuerdo_es_una_decision_explicita(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Mandarlo a nulo SI lo quita: es la otra mitad de la regla."""
        worker = await crear_trabajador(api, admin_csrf, "Despactado")
        tecnica = await crear_tecnica(api, admin_csrf, "despacto")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
            hourly_rate_override="17.5",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={"hourly_rate_override": None},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["rate_overridden"] is False
        assert Decimal(cuerpo["hourly_rate"]) == Decimal(15)

    async def test_las_horas_acordadas_sobreviven_a_cambiar_la_cantidad(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Horas pactadas")
        tecnica = await crear_tecnica(api, admin_csrf, "horas-pacto")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
            final_hours_override="10",
        )
        assert Decimal(tarea["calculated_hours"]) == JORNADA
        assert Decimal(tarea["final_hours"]) == Decimal(10)

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={"quantity": "100"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        # El estandar se recalcula —la cantidad cambio— y lo acordado se queda.
        assert Decimal(cuerpo["calculated_hours"]) == Decimal(16)
        assert Decimal(cuerpo["final_hours"]) == Decimal(10)
        assert cuerpo["hours_overridden"] is True

    async def test_cambiar_de_persona_no_hereda_la_tarifa_acordada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El acuerdo se tomo con alguien concreto; arrastrarlo seria inventarlo."""
        primero = await crear_trabajador(api, admin_csrf, "Primero")
        segundo = await crear_trabajador(api, admin_csrf, "Segundo", daily_rate="160")
        tecnica = await crear_tecnica(api, admin_csrf, "cambio-persona")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=primero["id"],
            technique_id=tecnica["id"],
            quantity="50",
            hourly_rate_override="17.5",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={"worker_id": segundo["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        assert cuerpo["rate_overridden"] is False
        assert Decimal(cuerpo["hourly_rate"]) == Decimal(20)


# ---------------------------------------------------------------------------
# Ilustracion
# ---------------------------------------------------------------------------
class TestIlustracion:
    async def test_por_defecto_esta_apagada(self, api: httpx.AsyncClient, admin_csrf: str) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)

        cuerpo = (await api.get(f"{V2}/{cotizacion}/illustration")).json()

        assert cuerpo["enabled"] is False
        assert Decimal(cuerpo["hours"]) == Decimal(0)
        assert Decimal(cuerpo["cost"]) == Decimal(0)

    async def test_cincuenta_piezas_son_una_jornada_y_ciento_diez_soles(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La configuracion aprobada: S/110 por 8 h, 50 piezas por jornada."""
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": True, "illustration_quantity": "50"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert Decimal(cuerpo["hourly_rate"]) == Decimal("13.75")
        assert Decimal(cuerpo["hours"]) == JORNADA
        assert Decimal(cuerpo["cost"]) == Decimal(110)

    async def test_setenta_y_cinco_piezas_son_doce_horas_y_ciento_sesenta_y_cinco(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Proporcional por horas, NO `ceil(75/50) x 110`, que daria S/220."""
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": True, "illustration_quantity": "75"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        assert Decimal(cuerpo["hours"]) == Decimal(12)
        assert Decimal(cuerpo["cost"]) == Decimal(165)
        assert Decimal(cuerpo["cost"]) != Decimal(220)

    async def test_una_sola_pieza_no_cuesta_una_jornada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": True, "illustration_quantity": "1"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        assert Decimal(cuerpo["hours"]) == Decimal("0.16")
        assert Decimal(cuerpo["cost"]) == Decimal("2.2")

    async def test_apagarla_lo_deja_todo_en_cero(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": True, "illustration_quantity": "75"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": False},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        assert Decimal(cuerpo["hours"]) == Decimal(0)
        assert Decimal(cuerpo["cost"]) == Decimal(0)

    async def test_subir_la_tarifa_global_no_cambia_lo_ya_congelado(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Una CTZ que congelo 110 sigue con 110; la siguiente usa 130."""
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": True, "illustration_quantity": "50"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        await db_session.execute(
            text("UPDATE v2_commercial_settings SET illustration_daily_rate = 130 WHERE id = 1")
        )
        await db_session.commit()

        vieja = (await api.get(f"{V2}/{cotizacion}/illustration")).json()
        assert Decimal(vieja["daily_rate"]) == Decimal(110)
        assert Decimal(vieja["cost"]) == Decimal(110)

        nueva_ctz = await crear_cotizacion(api, admin_csrf)
        nueva = (
            await api.put(
                f"{V2}/{nueva_ctz}/illustration",
                json={"illustration_enabled": True, "illustration_quantity": "50"},
                headers={"X-CSRF-Token": admin_csrf},
            )
        ).json()
        assert Decimal(nueva["daily_rate"]) == Decimal(130)
        assert Decimal(nueva["cost"]) == Decimal(130)

    async def test_una_tarifa_acordada_para_esta_cotizacion(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={
                "illustration_enabled": True,
                "illustration_quantity": "50",
                "illustration_hourly_rate_override": "20",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        assert Decimal(cuerpo["hourly_rate"]) == Decimal(20)
        assert Decimal(cuerpo["cost"]) == Decimal(160)

    async def test_no_aparece_entre_las_tecnicas(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Es un concepto comercial aparte: ilustrar no es tornear.

        Si estuviera en el catalogo heredaria reglas que no son suyas —jornada
        compartida, rendimiento por trabajador— y dejaria de poder encenderse
        y apagarse como una decision de la cotizacion.
        """
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": True, "illustration_quantity": "50"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        tecnicas = (await api.get(TECHNIQUES)).json()["items"]
        assert tecnicas == []
        cuerpo = await pagina(api, cotizacion)
        assert cuerpo["items"] == []
        assert Decimal(cuerpo["labor_cost"]) == Decimal(0)


# ---------------------------------------------------------------------------
# Borrador y emitida
# ---------------------------------------------------------------------------
class TestBorradorYEmitida:
    async def test_una_cotizacion_emitida_no_admite_mas_trabajo(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Congelado")
        tecnica = await crear_tecnica(api, admin_csrf, "congelada")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await db_session.execute(
            text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": cotizacion},
        )
        await db_session.commit()

        response = await api.post(
            f"{V2}/{cotizacion}/labor",
            json={"worker_id": worker["id"], "technique_id": tecnica["id"], "quantity": "10"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "V2_QUOTATION_NOT_EDITABLE"

    async def test_una_emitida_tampoco_admite_ilustracion(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await db_session.execute(
            text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": cotizacion},
        )
        await db_session.commit()

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_enabled": True, "illustration_quantity": "50"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 409

    async def test_una_tarea_de_otra_cotizacion_no_se_puede_editar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Ajeno")
        tecnica = await crear_tecnica(api, admin_csrf, "ajena")
        propia = await crear_cotizacion(api, admin_csrf)
        ajena = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            propia,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="10",
        )

        response = await api.put(
            f"{V2}/{ajena}/labor/{tarea['id']}",
            json={"quantity": "20"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Bordes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cantidad", ["0", "1"])
async def test_una_cantidad_cero_no_revienta(
    api: httpx.AsyncClient, admin_csrf: str, cantidad: str
) -> None:
    """Una tarea a medio llenar es un estado legitimo de un borrador."""
    worker = await crear_trabajador(api, admin_csrf, f"Borde {cantidad}")
    tecnica = await crear_tecnica(api, admin_csrf, f"borde-{cantidad}")
    cotizacion = await crear_cotizacion(api, admin_csrf)

    tarea = await anadir_tarea(
        api,
        admin_csrf,
        cotizacion,
        worker_id=worker["id"],
        technique_id=tecnica["id"],
        quantity=cantidad,
    )

    esperado = Decimal(cantidad) * JORNADA / Decimal(50)
    assert Decimal(tarea["calculated_hours"]) == esperado


async def test_un_rendimiento_de_cero_no_se_puede_configurar(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Seria una division por cero en la formula de horas."""
    response = await api.post(
        TECHNIQUES,
        json={"code": "cero", "name": "Cero", "default_capacity_per_workday": "0"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 422


async def test_una_jornada_de_cero_horas_no_se_puede_configurar(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.post(
        WORKERS,
        json={
            "name": "Sin jornada",
            "worker_type": "INTERNAL",
            "daily_rate": "120",
            "workday_hours": "0",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 422


async def test_una_tarifa_negativa_no_se_puede_configurar(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.post(
        WORKERS,
        json={"name": "Negativo", "worker_type": "INTERNAL", "daily_rate": "-1"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 422


async def test_veinticinco_horas_de_jornada_no_caben_en_un_dia(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.post(
        WORKERS,
        json={
            "name": "Imposible",
            "worker_type": "INTERNAL",
            "daily_rate": "120",
            "workday_hours": "25",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert response.status_code == 422


async def test_el_navegador_no_puede_mandar_el_costo(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Lo unico que viaja de ida son decisiones, no resultados."""
    worker = await crear_trabajador(api, admin_csrf, "Sin costo de ida")
    tecnica = await crear_tecnica(api, admin_csrf, "sin-costo")
    cotizacion = await crear_cotizacion(api, admin_csrf)

    response = await api.post(
        f"{V2}/{cotizacion}/labor",
        json={
            "worker_id": worker["id"],
            "technique_id": tecnica["id"],
            "quantity": "50",
            "labor_cost": "1",
        },
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Legacy
# ---------------------------------------------------------------------------
async def test_las_tareas_v2_no_tocan_el_catalogo_de_legacy(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El Cotizador historico sigue cobrando exactamente igual."""
    antes = await db_session.scalar(text("SELECT count(*) FROM techniques"))
    worker = await crear_trabajador(api, admin_csrf, "Aislado")
    tecnica = await crear_tecnica(api, admin_csrf, "aislada")
    cotizacion = await crear_cotizacion(api, admin_csrf)

    await anadir_tarea(
        api,
        admin_csrf,
        cotizacion,
        worker_id=worker["id"],
        technique_id=tecnica["id"],
        quantity="50",
    )

    despues = await db_session.scalar(text("SELECT count(*) FROM techniques"))
    assert antes == despues
    legacy = await db_session.scalar(text("SELECT count(*) FROM quotation_techniques"))
    assert legacy == 0


class TestLoQueEncontroLaAuditoria:
    async def test_retirar_la_tarifa_de_ilustracion_vuelve_a_la_congelada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Presente y en nulo retira el acuerdo. Ausente lo conserva.

        Es el mismo fallo de 010C: `data.get(...)` devuelve `None` tanto si el
        campo no vino como si vino en nulo. Confundirlos deja cobrando una
        tarifa que alguien creyo haber quitado.

        Y vuelve a la tarifa que ESTA cotizacion congelo —S/110 entre 8 h—, no a
        la de hoy: retirar un acuerdo no es motivo para recotizar con otro
        jornal.
        """
        cotizacion = await crear_cotizacion(api, admin_csrf)
        con_acuerdo = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={
                "illustration_enabled": True,
                "illustration_quantity": "50",
                "illustration_hourly_rate_override": "20",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert Decimal(con_acuerdo.json()["cost"]) == Decimal(160)

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_hourly_rate_override": None},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert Decimal(cuerpo["hourly_rate"]) == Decimal("13.75")
        assert Decimal(cuerpo["cost"]) == Decimal(110)

    async def test_cambiar_la_cantidad_conserva_la_tarifa_acordada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La otra mitad de la regla: ausente no es lo mismo que nulo."""
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={
                "illustration_enabled": True,
                "illustration_quantity": "50",
                "illustration_hourly_rate_override": "20",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        response = await api.put(
            f"{V2}/{cotizacion}/illustration",
            json={"illustration_quantity": "75"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        assert Decimal(cuerpo["hourly_rate"]) == Decimal(20)
        assert Decimal(cuerpo["hours"]) == Decimal(12)
        assert Decimal(cuerpo["cost"]) == Decimal(240)

    async def test_una_division_inexacta_deja_la_fila_cuadrada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Horas x tarifa tiene que dar el costo con los numeros GUARDADOS.

        Una tecnica que rinde 3 piezas por jornada da 2,666666... horas para una
        pieza. Si el costo se calculara con el numero largo y la columna
        guardara el corto, la cotizacion mostraria un importe que no es el
        producto de lo que ensena, y nadie podria comprobarlo.
        """
        worker = await crear_trabajador(api, admin_csrf, "Cuadrado")
        tecnica = await crear_tecnica(api, admin_csrf, "inexacta", default_capacity_per_workday="3")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="1",
        )

        horas = Decimal(tarea["final_hours"])
        tarifa = Decimal(tarea["hourly_rate"])
        assert horas == Decimal("2.666667")
        assert Decimal(tarea["labor_cost"]) == horas * tarifa


class TestReenviarNoEsElegir:
    """Un cliente que manda el formulario entero reenvia los mismos ids.

    Tomarlo por una eleccion nueva tiene dos consecuencias caras, y las dos son
    silenciosas: encalla un borrador cuya persona se dio de baja despues, y
    borra una tarifa acordada como si se hubiera cambiado de persona.
    """

    async def test_reenviar_el_mismo_trabajador_no_borra_la_tarifa_acordada(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Reenviado")
        tecnica = await crear_tecnica(api, admin_csrf, "reenvio")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
            hourly_rate_override="17.5",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={
                "worker_id": worker["id"],
                "technique_id": tecnica["id"],
                "quantity": "100",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["rate_overridden"] is True
        assert Decimal(cuerpo["hourly_rate"]) == Decimal("17.5")

    async def test_un_borrador_con_alguien_dado_de_baja_sigue_editandose(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Bloquearlo encallaria la cotizacion por una decision de otra pantalla.

        La regla es: elegir HOY a alguien de baja se rechaza; seguir editando
        una tarea que ya lo tenia, se avisa. Y eso vale tambien cuando el
        cliente reenvia su id sin cambiarlo.
        """
        worker = await crear_trabajador(api, admin_csrf, "Se fue despues")
        tecnica = await crear_tecnica(api, admin_csrf, "se-fue")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
        )
        baja = await api.put(
            f"{WORKERS}/{worker['id']}",
            json={"expected_version": worker["version"], "active": False},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={"worker_id": worker["id"], "quantity": "100"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert "V2_LABOR_WORKER_UNAVAILABLE" in cuerpo["warnings"]
        # Y lo congelado se conserva: el costo sigue siendo el que se acordo.
        assert Decimal(cuerpo["hourly_rate"]) == Decimal(15)

    async def test_un_borrador_con_una_tecnica_retirada_sigue_editandose(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        worker = await crear_trabajador(api, admin_csrf, "Con tecnica retirada")
        tecnica = await crear_tecnica(api, admin_csrf, "se-retira")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=worker["id"],
            technique_id=tecnica["id"],
            quantity="50",
        )
        baja = await api.put(
            f"{TECHNIQUES}/{tecnica['id']}",
            json={"expected_version": tecnica["version"], "active": False},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={"technique_id": tecnica["id"], "quantity": "100"},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        assert "V2_LABOR_TECHNIQUE_UNAVAILABLE" in response.json()["warnings"]

    async def test_cambiar_de_verdad_de_persona_si_retira_el_acuerdo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La otra mitad: el acuerdo se tomo con alguien concreto."""
        primero = await crear_trabajador(api, admin_csrf, "Pactante")
        segundo = await crear_trabajador(api, admin_csrf, "Sustituto", daily_rate="160")
        tecnica = await crear_tecnica(api, admin_csrf, "sustitucion")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        tarea = await anadir_tarea(
            api,
            admin_csrf,
            cotizacion,
            worker_id=primero["id"],
            technique_id=tecnica["id"],
            quantity="50",
            hourly_rate_override="17.5",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/labor/{tarea['id']}",
            json={"worker_id": segundo["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )

        cuerpo = response.json()
        assert cuerpo["rate_overridden"] is False
        assert Decimal(cuerpo["hourly_rate"]) == Decimal(20)
