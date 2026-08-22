"""Configuracion de empresa: lectura, escritura, permisos y concurrencia."""

from __future__ import annotations

import httpx

COMPANY = "/api/v1/settings/company"


def _payload(version: int, **campos: object) -> dict[str, object]:
    base: dict[str, object] = {"version": version}
    base.update(campos)
    return base


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------
async def test_sin_sesion_no_se_puede_leer(api: httpx.AsyncClient) -> None:
    response = await api.get(COMPANY)

    assert response.status_code == 401


async def test_admin_lee_la_configuracion(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.get(COMPANY)

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["logo"] is None


async def test_no_hay_datos_de_empresa_precargados(api: httpx.AsyncClient, admin_csrf: str) -> None:
    """Las fuentes del proyecto no aportan estos datos y no se inventan."""
    body = (await api.get(COMPANY)).json()

    assert body["legal_name"] is None
    assert body["tax_id"] is None
    assert body["email"] is None


async def test_operator_puede_consultar(api: httpx.AsyncClient, operator_csrf: str) -> None:
    """Necesita conocer los datos de empresa para trabajar."""
    assert (await api.get(COMPANY)).status_code == 200


# ---------------------------------------------------------------------------
# Escritura y permisos
# ---------------------------------------------------------------------------
async def test_admin_actualiza_la_configuracion(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMPANY,
        json=_payload(1, legal_name="Taller Greda SAC", tax_id="20123456789"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legal_name"] == "Taller Greda SAC"
    assert body["tax_id"] == "20123456789"
    assert body["version"] == 2


async def test_el_cambio_persiste(api: httpx.AsyncClient, admin_csrf: str) -> None:
    await api.put(
        COMPANY,
        json=_payload(1, legal_name="Taller Greda SAC"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert (await api.get(COMPANY)).json()["legal_name"] == "Taller Greda SAC"


async def test_operator_no_puede_modificar(api: httpx.AsyncClient, operator_csrf: str) -> None:
    """La restriccion la impone el backend, no la interfaz."""
    response = await api.put(
        COMPANY,
        json=_payload(1, legal_name="Intento no autorizado"),
        headers={"X-CSRF-Token": operator_csrf},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_INSUFFICIENT_ROLE"


async def test_operator_no_deja_rastro_del_intento(
    api: httpx.AsyncClient, operator_csrf: str
) -> None:
    await api.put(
        COMPANY,
        json=_payload(1, legal_name="Intento no autorizado"),
        headers={"X-CSRF-Token": operator_csrf},
    )

    assert (await api.get(COMPANY)).json()["legal_name"] is None


async def test_sin_sesion_no_se_puede_modificar(api: httpx.AsyncClient) -> None:
    token = (await api.get("/api/v1/auth/csrf")).json()["csrf_token"]

    response = await api.put(
        COMPANY, json=_payload(1, legal_name="X"), headers={"X-CSRF-Token": token}
    )

    assert response.status_code == 401


async def test_una_mutacion_sin_csrf_falla(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(COMPANY, json=_payload(1, legal_name="X"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_TOKEN_MISSING"


# ---------------------------------------------------------------------------
# Validacion
# ---------------------------------------------------------------------------
async def test_un_ruc_invalido_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMPANY, json=_payload(1, tax_id="123"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_no_se_admite_escribir_un_campo_no_expuesto(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Mass assignment: la ruta del logo solo la fija el backend."""
    response = await api.put(
        COMPANY,
        json=_payload(1, logo_object_path="company/ajeno.png"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422


async def test_no_se_admite_html_en_los_textos(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        COMPANY,
        json=_payload(1, legal_name="<script>alert(1)</script>"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Concurrencia optimista
# ---------------------------------------------------------------------------
async def test_una_version_desfasada_no_pisa_el_cambio_ajeno(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Dos administradores editan a la vez: el segundo debe enterarse."""
    await api.put(
        COMPANY,
        json=_payload(1, legal_name="Primer cambio"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    # El segundo sigue creyendo que la version es 1.
    response = await api.put(
        COMPANY,
        json=_payload(1, legal_name="Segundo cambio"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SETTINGS_VERSION_CONFLICT"
    assert (await api.get(COMPANY)).json()["legal_name"] == "Primer cambio"


async def test_con_la_version_correcta_el_segundo_cambio_entra(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    await api.put(
        COMPANY, json=_payload(1, legal_name="Primero"), headers={"X-CSRF-Token": admin_csrf}
    )

    response = await api.put(
        COMPANY, json=_payload(2, legal_name="Segundo"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 200
    assert response.json()["legal_name"] == "Segundo"


async def test_guardar_sin_cambios_no_incrementa_la_version(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    response = await api.put(COMPANY, json=_payload(1), headers={"X-CSRF-Token": admin_csrf})

    assert response.status_code == 200
    assert response.json()["version"] == 1
