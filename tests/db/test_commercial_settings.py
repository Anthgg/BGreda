"""Parametros comerciales: IGV, moneda, vigencia y datos bancarios."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import CommercialSettings

COMMERCIAL = "/api/v1/settings/commercial"


def _payload(version: int, **campos: object) -> dict[str, object]:
    base: dict[str, object] = {"version": version}
    base.update(campos)
    return base


# ---------------------------------------------------------------------------
# Lectura y permisos
# ---------------------------------------------------------------------------
async def test_sin_sesion_no_se_puede_leer(api: httpx.AsyncClient) -> None:
    assert (await api.get(COMMERCIAL)).status_code == 401


async def test_no_hay_valores_comerciales_precargados(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Ni moneda ni IGV se inventan: los define el usuario."""
    body = (await api.get(COMMERCIAL)).json()

    assert body["currency_code"] is None
    assert body["tax_percent"] is None
    assert body["quote_validity_days"] is None
    assert body["bank_accounts"] == []


async def test_operator_puede_consultar(api: httpx.AsyncClient, operator_csrf: str) -> None:
    assert (await api.get(COMMERCIAL)).status_code == 200


async def test_operator_no_puede_modificar(api: httpx.AsyncClient, operator_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL, json=_payload(1, tax_percent=18), headers={"X-CSRF-Token": operator_csrf}
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# IGV y precision decimal
# ---------------------------------------------------------------------------
async def test_admin_configura_el_igv(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL,
        json=_payload(1, tax_percent=18, currency_code="PEN", currency_symbol="S/"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(str(body["tax_percent"])) == Decimal("18")
    assert body["currency_code"] == "PEN"


async def test_el_igv_se_guarda_como_numeric_no_como_float(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Un valor con decimales debe conservarse exacto en la base de datos."""
    await api.put(
        COMMERCIAL, json=_payload(1, tax_percent=18.5), headers={"X-CSRF-Token": admin_csrf}
    )

    almacenado = (
        await db_session.execute(text("SELECT tax_percent FROM commercial_settings WHERE id = 1"))
    ).scalar_one()

    assert isinstance(almacenado, Decimal)
    assert almacenado == Decimal("18.5")


async def test_un_igv_negativo_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL, json=_payload(1, tax_percent=-1), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 422


async def test_un_igv_fuera_de_rango_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL, json=_payload(1, tax_percent=1800), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 422


async def test_la_vigencia_se_persiste(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL, json=_payload(1, quote_validity_days=15), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.json()["quote_validity_days"] == 15


async def test_una_vigencia_invalida_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL, json=_payload(1, quote_validity_days=0), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Datos bancarios
# ---------------------------------------------------------------------------
async def test_se_crea_la_cuenta_bancaria_principal(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.put(
        COMMERCIAL,
        json=_payload(
            1,
            bank_account={
                "bank_name": "Banco de prueba",
                "account_holder": "Taller Greda SAC",
                "account_number": "1234567890",
                "cci": "00219300123456789015",
                "notes": "Transferencia en soles",
            },
        ),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.text
    cuentas = response.json()["bank_accounts"]
    assert len(cuentas) == 1
    assert cuentas[0]["is_primary"] is True
    assert cuentas[0]["cci"] == "00219300123456789015"


async def test_actualizar_la_cuenta_no_crea_una_segunda(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    await api.put(
        COMMERCIAL,
        json=_payload(1, bank_account={"bank_name": "Primero"}),
        headers={"X-CSRF-Token": admin_csrf},
    )
    version = (await api.get(COMMERCIAL)).json()["version"]

    response = await api.put(
        COMMERCIAL,
        json=_payload(version, bank_account={"bank_name": "Segundo"}),
        headers={"X-CSRF-Token": admin_csrf},
    )

    cuentas = response.json()["bank_accounts"]
    assert len(cuentas) == 1
    assert cuentas[0]["bank_name"] == "Segundo"


async def test_un_cci_invalido_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMMERCIAL,
        json=_payload(1, bank_account={"cci": "123"}),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Textos
# ---------------------------------------------------------------------------
async def test_los_textos_comerciales_se_guardan_como_texto_plano(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.put(
        COMMERCIAL,
        json=_payload(
            1,
            general_conditions="Precios sujetos a cambio sin previo aviso.",
            payment_notes="50 % adelanto, 50 % contra entrega.",
            document_footer="Gracias por su preferencia.",
        ),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200
    assert response.json()["general_conditions"].startswith("Precios")


async def test_no_se_admite_html_en_las_condiciones(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.put(
        COMMERCIAL,
        json=_payload(1, general_conditions="<script>alert('xss')</script>"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Concurrencia
# ---------------------------------------------------------------------------
async def test_version_desfasada_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    await api.put(
        COMMERCIAL, json=_payload(1, tax_percent=18), headers={"X-CSRF-Token": admin_csrf}
    )

    response = await api.put(
        COMMERCIAL, json=_payload(1, tax_percent=10), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Porcentaje de esmalte estimado (Fase 009D)
# ---------------------------------------------------------------------------
async def test_el_porcentaje_de_esmalte_se_lee_y_vale_quince(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """GET_RETURNS_15.

    Es el unico valor comercial que SI viene precargado, y a proposito: la
    columna es NOT NULL y el Cotizador necesita siempre un porcentaje con el
    que estimar. La migracion 0015 lo inicializa en 15.
    """
    body = (await api.get(COMMERCIAL)).json()

    assert Decimal(str(body["estimated_glaze_percent"])) == Decimal("15")


async def test_el_porcentaje_de_esmalte_se_puede_cambiar_y_persiste(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """UPDATE_15_TO_20 y RELOAD_RETURNS_20."""
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, estimated_glaze_percent="20"),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 200, respuesta.text
    assert Decimal(str(respuesta.json()["estimated_glaze_percent"])) == Decimal("20")

    # Releido desde la API...
    recargado = (await api.get(COMMERCIAL)).json()
    assert Decimal(str(recargado["estimated_glaze_percent"])) == Decimal("20")

    # ...y desde la base, para que no valga un valor que solo vive en memoria.
    almacenado = (
        await db_session.execute(
            text("SELECT estimated_glaze_percent FROM commercial_settings WHERE id = 1")
        )
    ).scalar_one()
    assert Decimal(str(almacenado)) == Decimal("20")


async def test_el_porcentaje_de_esmalte_rechaza_cero(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """INVALID_0.

    Cero no es "sin esmalte": es una estimacion que siempre da cero gramos y
    hace desaparecer el material del costo sin avisar.
    """
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, estimated_glaze_percent="0"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert respuesta.status_code == 422, respuesta.text


async def test_el_porcentaje_de_esmalte_rechaza_mas_de_cien(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """INVALID_GT_100: mas esmalte que pieza es un error de captura."""
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, estimated_glaze_percent="101"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert respuesta.status_code == 422, respuesta.text


async def test_un_valor_invalido_no_deja_rastro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """RESTORE_15: un rechazo no debe dejar el valor a medias.

    Se comprueba contra la base y no contra la respuesta: un 422 que hubiera
    escrito antes de validar seguiria devolviendo 422 y habria corrompido la
    configuracion igual.
    """
    for invalido in ("0", "101", "-5"):
        respuesta = await api.put(
            COMMERCIAL,
            json=_payload(1, estimated_glaze_percent=invalido),
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert respuesta.status_code == 422, f"{invalido}: {respuesta.text}"

    almacenado = (
        await db_session.execute(
            text("SELECT estimated_glaze_percent FROM commercial_settings WHERE id = 1")
        )
    ).scalar_one()
    assert Decimal(str(almacenado)) == Decimal("15")

    # Y la version no se ha movido: un rechazo no consume el bloqueo optimista.
    assert (await api.get(COMMERCIAL)).json()["version"] == 1


# ---------------------------------------------------------------------------
# Politica comercial: factor de produccion y paso de redondeo (Fase 009E)
# ---------------------------------------------------------------------------
async def test_el_factor_de_produccion_por_defecto_es_tres(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """GET_DEFAULT_PRODUCTION_FACTOR_3."""
    body = (await api.get(COMMERCIAL)).json()

    assert Decimal(str(body["production_factor_default"])) == Decimal("3")
    # Y NO es el mismo campo que el factor comercial heredado, que vale 2.
    assert Decimal(str(body["default_quotation_factor"])) == Decimal("2")


async def test_el_factor_de_produccion_se_cambia_y_persiste(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """UPDATE_FACTOR_3_TO_4 + RELOAD_FACTOR_4."""
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, production_factor_default="4"),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 200, respuesta.text

    assert Decimal(str((await api.get(COMMERCIAL)).json()["production_factor_default"])) == (
        Decimal("4")
    )
    almacenado = (
        await db_session.execute(
            text("SELECT production_factor_default FROM commercial_settings WHERE id = 1")
        )
    ).scalar_one()
    assert Decimal(str(almacenado)) == Decimal("4")


async def test_el_paso_de_redondeo_se_cambia_y_persiste(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """GET_ROUNDING_STEP_050 + UPDATE_ROUNDING_050_TO_100 + RELOAD_ROUNDING_100."""
    assert Decimal(str((await api.get(COMMERCIAL)).json()["rounding_step"])) == Decimal("0.50")

    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, rounding_step="1.00"),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 200, respuesta.text
    assert Decimal(str((await api.get(COMMERCIAL)).json()["rounding_step"])) == Decimal("1.00")


async def test_una_politica_invalida_se_rechaza_y_no_deja_rastro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """INVALID_FACTOR_* + INVALID_ROUNDING_* + RESTORE.

    Un rechazo no puede consumir el bloqueo optimista: si `version` subiera,
    el siguiente intento legitimo del usuario chocaria con un 409 sin que
    nadie hubiera guardado nada.
    """
    invalidos = (
        {"production_factor_default": "0"},
        {"production_factor_default": "-3"},
        {"rounding_step": "0.25"},
        {"rounding_step": "0.75"},
    )
    for campos in invalidos:
        respuesta = await api.put(
            COMMERCIAL, json=_payload(1, **campos), headers={"X-CSRF-Token": admin_csrf}
        )
        assert respuesta.status_code == 422, f"{campos}: {respuesta.text}"

    fila = (
        await db_session.execute(
            text(
                "SELECT production_factor_default, rounding_step "
                "FROM commercial_settings WHERE id = 1"
            )
        )
    ).one()
    assert Decimal(str(fila[0])) == Decimal("3")
    assert Decimal(str(fila[1])) == Decimal("0.50")
    assert (await api.get(COMMERCIAL)).json()["version"] == 1


# ---------------------------------------------------------------------------
# TARIFAS DE PROTOTIPO
#
# Las cinco columnas existian desde 0023 pero no las exponia ningun esquema:
# solo se podian cambiar por SQL. Eso no es un flujo: es una nota al pie que
# alguien tiene que recordar. Y como nacen en cero —a proposito, para no
# sembrar como precios reales los ejemplos del Excel—, un taller sin forma de
# configurarlas cotizaria a cero sin enterarse.
#
# No hacen falta servicio ni endpoint nuevos: `update_commercial` deriva los
# campos editables del propio esquema y `_commercial_out` los devuelve
# recorriendo `model_fields`. Exponerlos ES anadirlos al esquema.
# ---------------------------------------------------------------------------
TARIFAS = (
    "prototype_design_rate",
    "prototype_artist_rate",
    "prototype_mold_maker_price",
    "prototype_mold_maker_days",
    "prototype_fixed_cost",
)


async def test_la_lectura_devuelve_las_cinco_tarifas_de_prototipo(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """PROTOTYPE_SETTINGS_API_EXPOSED: PASS.

    Y todas en cero: el taller no hereda las tarifas de nadie.
    """
    cuerpo = (await api.get(COMMERCIAL)).json()

    for campo in TARIFAS:
        assert campo in cuerpo, campo
        assert Decimal(str(cuerpo[campo])) == Decimal(0), campo


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("prototype_design_rate", "200"),
        ("prototype_artist_rate", "150.50"),
        ("prototype_mold_maker_price", "100"),
        ("prototype_mold_maker_days", "1.5"),
        ("prototype_fixed_cost", "30"),
    ],
)
async def test_cada_tarifa_de_prototipo_se_configura_y_persiste(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    campo: str,
    valor: str,
) -> None:
    """PROTOTYPE_*_CONFIGURABLE: PASS, uno por parametro.

    Se relee de la BASE y no solo de la respuesta: una respuesta puede
    devolver lo que se le mando en vez de lo que se guardo.
    """
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, **{campo: valor}),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 200, respuesta.text
    assert Decimal(str(respuesta.json()[campo])) == Decimal(valor)

    recargado = (await api.get(COMMERCIAL)).json()
    assert Decimal(str(recargado[campo])) == Decimal(valor)

    # Por el ORM y no con SQL compuesto: `expire_all` obliga a un SELECT nuevo
    # igual, asi que se sigue leyendo de la base y no del identity map, y el
    # nombre del campo no se interpola en una consulta.
    db_session.expire_all()
    fila = await db_session.get(CommercialSettings, 1)
    assert fila is not None
    assert Decimal(str(getattr(fila, campo))) == Decimal(valor)


async def test_las_cinco_se_pueden_configurar_de_una_vez(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El caso real: alguien rellena el formulario entero y guarda una vez."""
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(
            1,
            prototype_design_rate="200",
            prototype_artist_rate="150",
            prototype_mold_maker_price="100",
            prototype_mold_maker_days="1",
            prototype_fixed_cost="30",
        ),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 200, respuesta.text

    db_session.expire_all()
    guardada = await db_session.get(CommercialSettings, 1)
    assert guardada is not None
    assert [Decimal(str(getattr(guardada, campo))) for campo in TARIFAS] == [
        Decimal(200),
        Decimal(150),
        Decimal(100),
        Decimal(1),
        Decimal(30),
    ]


async def test_cero_es_un_valor_legitimo_y_no_se_corrige_solo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_SETTINGS_ZERO_ALLOWED: PASS.

    Cero significa «el taller todavia no ha fijado esa tarifa». Convertirlo en
    80, 100 o 350 —los numeros del Excel, marcados alli como EJEMPLO— pondria
    un precio inventado en un documento que alguien firma.
    """
    puesta = await api.put(
        COMMERCIAL,
        json=_payload(1, prototype_design_rate="200"),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert puesta.status_code == 200, puesta.text

    vuelta = await api.put(
        COMMERCIAL,
        json=_payload(puesta.json()["version"], prototype_design_rate="0"),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert vuelta.status_code == 200, vuelta.text
    assert Decimal(str(vuelta.json()["prototype_design_rate"])) == Decimal(0)

    db_session.expire_all()
    almacenado = (
        await db_session.execute(
            text("SELECT prototype_design_rate FROM commercial_settings WHERE id = 1")
        )
    ).scalar_one()
    assert Decimal(str(almacenado)) == Decimal(0)


@pytest.mark.parametrize("campo", TARIFAS)
async def test_una_tarifa_negativa_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str, campo: str
) -> None:
    """PROTOTYPE_SETTINGS_NEGATIVE_REJECTED: PASS.

    Un dia negativo no existe y una tarifa negativa le pagaria al cliente.
    """
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, **{campo: "-1"}),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 422, respuesta.text


async def test_un_operario_no_puede_cambiar_las_tarifas_de_prototipo(
    api: httpx.AsyncClient, operator_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_SETTINGS_RBAC: PASS.

    La autoridad es del backend, no de que el frontend esconda el formulario.
    Y despues del 403 la base sigue igual: un rechazo que hubiera escrito algo
    seria peor que no tener permisos.
    """
    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, prototype_design_rate="999"),
        headers={"X-CSRF-Token": operator_csrf},
    )
    assert respuesta.status_code == 403

    db_session.expire_all()
    almacenado = (
        await db_session.execute(
            text("SELECT prototype_design_rate FROM commercial_settings WHERE id = 1")
        )
    ).scalar_one()
    assert Decimal(str(almacenado)) == Decimal(0)


async def test_cambiar_una_tarifa_queda_auditado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_SETTINGS_AUDIT: PASS.

    Mismo mecanismo que el resto de la configuracion comercial: quien, cuando,
    de que a que.
    """
    from app.models.audit import AuditEvent

    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, prototype_artist_rate="150"),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 200, respuesta.text

    eventos = (
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.field == "prototype_artist_rate")
            )
        )
        .scalars()
        .all()
    )
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.entity_type == "commercial_settings"
    assert Decimal(str(evento.new_value)) == Decimal(150)
    assert evento.user_id is not None


async def test_un_update_rechazado_no_deja_auditoria_de_exito(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Delta de auditoria 0 cuando el cambio no llego a ocurrir.

    Se cuenta ANTES y DESPUES en vez de mirar si la tabla esta vacia: el
    endpoint audita otras cosas, y una tabla vacia probaria menos.
    """
    from app.models.audit import AuditEvent

    antes = await db_session.scalar(select(func.count()).select_from(AuditEvent))

    respuesta = await api.put(
        COMMERCIAL,
        json=_payload(1, prototype_design_rate="-5"),
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert respuesta.status_code == 422, respuesta.text

    db_session.expire_all()
    despues = await db_session.scalar(select(func.count()).select_from(AuditEvent))
    assert despues == antes
