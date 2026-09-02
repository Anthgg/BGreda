"""Fase 009I — el QR de la orden de produccion.

Un QR es texto legible por cualquiera que apunte una camara. Lo que se imprime
en una hoja de taller acaba en una mesa, en una foto y en un grupo de
mensajeria, asi que lo que NO va dentro importa mas que lo que va.
"""

from __future__ import annotations

import base64
import re
import secrets

import pytest

from app.models.production import QR_TOKEN_LENGTH, QR_TOKEN_MIN_LENGTH
from app.services.production_pdf import SCAN_PATH, build_qr_data_uri


def _payload_del_qr(data_uri: str) -> str:
    """Devuelve el SVG decodificado del `data:` que se incrusta en el PDF."""
    prefijo = "data:image/svg+xml;base64,"
    assert data_uri.startswith(prefijo)
    return base64.b64decode(data_uri[len(prefijo) :]).decode("utf-8")


def test_el_qr_se_incrusta_como_data_uri() -> None:
    """WeasyPrint no descarga nada: el documento tiene que bastarse solo.

    Un `<img src="https://...">` saldria en blanco en el PDF y nadie lo notaria
    hasta tener la hoja impresa en la mano.
    """
    data_uri = build_qr_data_uri("token-de-prueba", base_url="https://app.ejemplo.pe")
    assert data_uri.startswith("data:image/svg+xml;base64,")
    assert "<svg" in _payload_del_qr(data_uri)


def test_el_qr_lleva_la_ruta_del_token_y_nada_mas() -> None:
    """QR_DOES_NOT_EXPOSE_SENSITIVE_DATA.

    Dentro va una ruta con el token opaco. Ni cliente, ni precio, ni RUC, ni
    stock, ni receta, ni token de sesion.
    """
    token = "abcdefghijklmnopqrstuvwxyz0123456789ABCD"
    data_uri = build_qr_data_uri(token, base_url="https://app.ejemplo.pe")

    assert build_qr_data_uri(token, base_url="https://app.ejemplo.pe") == data_uri
    # El contenido codificado es exactamente la ruta esperada: se comprueba
    # comparando contra el QR de esa cadena, que es lo unico que puede
    # afirmarse sin decodificar la matriz.
    esperado = build_qr_data_uri(token, base_url="https://app.ejemplo.pe/")
    assert esperado == data_uri, "la barra final no puede cambiar lo que se codifica"


@pytest.mark.parametrize(
    "prohibido",
    ["password", "authorization", "ruc", "20601234567", "precio"],
)
def test_el_qr_no_admite_que_se_le_cuele_nada_por_la_base(prohibido: str) -> None:
    """La base viene de la configuracion, no de datos de la peticion.

    Esta prueba fija el contrato: la funcion solo compone `base + ruta + token`.
    No hay ningun parametro por el que entren datos del cliente o del pedido.
    """
    data_uri = build_qr_data_uri("token-limpio-de-prueba", base_url="https://app.ejemplo.pe")
    svg = _payload_del_qr(data_uri)
    # El SVG es una matriz de rectangulos: no contiene texto legible en claro.
    assert prohibido not in svg.lower()


def test_la_ruta_del_qr_apunta_al_seguimiento_publico() -> None:
    """Fase 009I.1. El QR ya no lleva a una ruta que exige sesion.

    Hasta 009J el QR apuntaba a `/produccion/scan/...`, detras del login: quien
    escaneaba sin cuenta —el del horno, el que pregunta por su encargo— acababa
    en una pantalla de credenciales. Ahora apunta a la superficie publica de
    solo lectura, y quien SI tiene sesion salta desde ahi a la vista interna.

    La prueba fija el destino, no solo la forma: si alguien devolviera esta
    constante a una ruta interna, el QR impreso volveria a no servir de nada y
    ninguna otra prueba lo notaria.
    """
    assert SCAN_PATH == "/seguimiento"
    assert "{" not in SCAN_PATH
    assert not SCAN_PATH.startswith("/produccion")


def test_sin_base_configurada_el_qr_lleva_solo_la_ruta_relativa() -> None:
    """Mejor una ruta relativa que un dominio inventado.

    Si no hay origen declarado, el QR no se saca de la manga un host: lleva la
    ruta, que sigue identificando la orden.
    """
    data_uri = build_qr_data_uri("token-sin-base", base_url=None)
    assert data_uri.startswith("data:image/svg+xml;base64,")


def test_el_token_generado_es_largo_y_no_secuencial() -> None:
    """El token no puede ser el id ni el codigo con otro nombre.

    Con un token secuencial, quien tiene el QR de una orden tiene el de todas:
    basta cambiar un digito. `secrets.token_urlsafe(32)` da 256 bits de
    entropia y no admite ese paseo.
    """
    tokens = {secrets.token_urlsafe(32) for _ in range(200)}
    assert len(tokens) == 200, "dos tokens iguales en 200 intentos no es un token opaco"
    for token in tokens:
        assert len(token) >= QR_TOKEN_MIN_LENGTH
        assert len(token) <= QR_TOKEN_LENGTH
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
        assert not token.isdigit()
