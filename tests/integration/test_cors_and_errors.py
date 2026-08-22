"""CORS y contrato uniforme de errores."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

ORIGEN_PERMITIDO = "http://localhost:5173"
ORIGEN_NO_PERMITIDO = "https://sitio-malicioso.example"


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
async def test_origen_permitido_recibe_cabeceras_con_credenciales(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/live", headers={"Origin": ORIGEN_PERMITIDO})

    assert response.headers["access-control-allow-origin"] == ORIGEN_PERMITIDO
    assert response.headers["access-control-allow-credentials"] == "true"


async def test_nunca_se_responde_con_comodin(client: httpx.AsyncClient) -> None:
    response = await client.get("/live", headers={"Origin": ORIGEN_PERMITIDO})

    assert response.headers["access-control-allow-origin"] != "*"


async def test_origen_no_permitido_no_recibe_autorizacion_cors(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/live", headers={"Origin": ORIGEN_NO_PERMITIDO})

    assert "access-control-allow-origin" not in response.headers


async def test_preflight_autoriza_la_cabecera_csrf(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": ORIGEN_PERMITIDO,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )

    assert response.status_code == 200
    permitidas = response.headers["access-control-allow-headers"].lower()
    assert "x-csrf-token" in permitidas
    assert response.headers["access-control-allow-credentials"] == "true"


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
async def test_ruta_inexistente_usa_el_formato_uniforme(client: httpx.AsyncClient) -> None:
    response = await client.get("/no-existe")

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "NOT_FOUND"
    assert isinstance(body["error"]["message"], str)


async def test_excepcion_no_controlada_no_expone_detalles_internos(
    app: FastAPI,
) -> None:
    @app.get("/_test/boom")
    async def _boom() -> None:
        raise RuntimeError("detalle interno con informacion sensible")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as local:
        response = await local.get("/_test/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "detalle interno" not in response.text
    assert "Traceback" not in response.text
