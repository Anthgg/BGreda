"""Fase 009I — el inventario de rutas del modulo de produccion.

Esta prueba existe porque durante el desarrollo se perdio `GET /{order_id}`
—la lectura de la que vive toda la pantalla de detalle— al reordenar el
archivo, y ninguna prueba lo dijo: las de base de datos fallaban antes por otro
motivo y el error real quedo tapado.

No hace falta PostgreSQL para comprobar que una ruta esta declarada. Aqui se
comprueba, y ademas se fija el ORDEN, que en FastAPI es semantico.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.api.v1.production import router

#: Todas las rutas del modulo, con sus metodos. Si alguna desaparece o cambia
#: de forma, esta lista lo dice por su nombre.
ESPERADAS = {
    ("/production-orders", "GET"),
    ("/production-orders", "POST"),
    ("/production-orders/scan/{qr_token}", "GET"),
    ("/production-orders/{order_id}", "GET"),
    ("/production-orders/{order_id}/document", "GET"),
    ("/production-orders/{order_id}/start", "POST"),
    ("/production-orders/{order_id}/complete", "POST"),
    ("/production-orders/{order_id}/cancel", "POST"),
}


def _rutas() -> list[tuple[str, str]]:
    salida: list[tuple[str, str]] = []
    for ruta in router.routes:
        if not isinstance(ruta, APIRoute):
            continue
        for metodo in sorted(ruta.methods - {"HEAD", "OPTIONS"}):
            salida.append((ruta.path, metodo))
    return salida


def test_estan_declaradas_todas_las_rutas_del_modulo() -> None:
    assert set(_rutas()) == ESPERADAS


def test_el_escaneo_del_qr_se_declara_antes_que_el_detalle() -> None:
    """FastAPI resuelve por ORDEN de declaracion, no por especificidad.

    Con `/{order_id}` delante, una peticion a `/scan/abc` entra por ella con
    `order_id="scan"` y responde un 422 sobre un entero invalido: un error
    incomprensible para quien acaba de apuntar la camara a un QR.
    """
    caminos = [ruta for ruta, _metodo in _rutas()]
    assert caminos.index("/production-orders/scan/{qr_token}") < caminos.index(
        "/production-orders/{order_id}"
    )


def test_solo_arrancar_completar_y_anular_son_escrituras_sobre_una_orden() -> None:
    """La superficie mutante del modulo, escrita como lista cerrada.

    Anadir aqui un POST nuevo obliga a mirar esta prueba y a preguntarse si esa
    ruta puede o no mover inventario. De las tres, solo `start` lo hace.
    """
    escrituras = {ruta for ruta, metodo in _rutas() if metodo == "POST"}
    assert escrituras == {
        "/production-orders",
        "/production-orders/{order_id}/start",
        "/production-orders/{order_id}/complete",
        "/production-orders/{order_id}/cancel",
    }


def test_ninguna_ruta_permite_borrar() -> None:
    """Una orden no se borra: se anula, y queda.

    Borrarla se llevaria por delante la referencia de los movimientos de stock
    que la orden origino, que es justamente la evidencia de por que bajo un
    saldo.
    """
    assert not [ruta for ruta, metodo in _rutas() if metodo in {"DELETE", "PUT", "PATCH"}]
