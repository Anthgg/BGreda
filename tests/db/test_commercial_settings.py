"""Parametros comerciales: IGV, moneda, vigencia y datos bancarios."""

from __future__ import annotations

from decimal import Decimal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
