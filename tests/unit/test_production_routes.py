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
    # Fase 010I: consumo real, notas y quemas, seguimiento.
    ("/production-orders/{order_id}/consumptions", "GET"),
    ("/production-orders/{order_id}/consumptions", "POST"),
    ("/production-orders/{order_id}/notes", "POST"),
    ("/production-orders/{order_id}/communications", "POST"),
    ("/production-orders/{order_id}/timeline", "GET"),
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


def test_la_superficie_de_escritura_sobre_una_orden_es_cerrada() -> None:
    """La superficie mutante del modulo, escrita como lista cerrada.

    Anadir aqui un POST nuevo obliga a mirar esta prueba y a preguntarse si esa
    ruta puede o no mover inventario. Mueven inventario `start` en una orden
    Legacy o de muestra y `consumptions` en una V2 (Fase 010I); `notes` y
    `communications` no.
    """
    escrituras = {ruta for ruta, metodo in _rutas() if metodo == "POST"}
    assert escrituras == {
        "/production-orders",
        "/production-orders/{order_id}/start",
        "/production-orders/{order_id}/complete",
        "/production-orders/{order_id}/cancel",
        "/production-orders/{order_id}/consumptions",
        "/production-orders/{order_id}/notes",
        "/production-orders/{order_id}/communications",
    }


def test_ninguna_ruta_permite_borrar() -> None:
    """Una orden no se borra: se anula, y queda.

    Borrarla se llevaria por delante la referencia de los movimientos de stock
    que la orden origino, que es justamente la evidencia de por que bajo un
    saldo.
    """
    assert not [ruta for ruta, metodo in _rutas() if metodo in {"DELETE", "PUT", "PATCH"}]


#: Quien puede ejecutar cada mutacion, leido del propio router (Fase 009J).
#:
#: "taller" son ADMIN y OPERATOR; "admin" es solo ADMIN. Ampliar un permiso sin
#: querer es un cambio de una palabra que no rompe ningun tipo ni ninguna
#: prueba de comportamiento: por eso la matriz se fija aqui, donde una linea
#: cambiada se ve.
PERMISOS_ESPERADOS = {
    "create_production_order": "taller",
    "start_production_order": "taller",
    "complete_production_order": "taller",
    # Anular deshace un compromiso ya tomado y deja la cotizacion de origen
    # ocupada para siempre. Es decision administrativa.
    "cancel_production_order": "admin",
    # Fase 010I. Registrar lo que paso en el taller es de taller (D2 y D4).
    "record_production_consumption": "taller",
    "add_production_note": "taller",
    "record_production_communication": "taller",
    "list_production_consumptions": "cualquier sesion",
    "get_production_timeline": "cualquier sesion",
}


#: Nombre de la dependencia declarada -> quien la satisface.
DEPENDENCIA_A_ALCANCE = {
    "WorkshopUserDep": "taller",
    "AdminUserDep": "admin",
    "CurrentUserDep": "cualquier sesion",
}


def _dependencias() -> dict[str, str]:
    """Lee la dependencia declarada en la firma de cada endpoint.

    Con `from __future__ import annotations` las anotaciones llegan como texto,
    asi que se lee el NOMBRE de la dependencia. Es una comprobacion de la
    declaracion, no del comportamiento: que `WorkshopUserDep` admita de verdad
    a un operario y rechace a un tercero lo prueban las de base de datos, que
    hacen la peticion y miran el 403.
    """
    import inspect

    from app.api.v1 import production

    salida: dict[str, str] = {}
    for nombre in PERMISOS_ESPERADOS:
        firma = inspect.signature(getattr(production, nombre))
        deps = [
            DEPENDENCIA_A_ALCANCE[str(p.annotation)]
            for p in firma.parameters.values()
            if str(p.annotation) in DEPENDENCIA_A_ALCANCE
        ]
        assert deps, f"{nombre} no declara ninguna dependencia de autorizacion"
        salida[nombre] = deps[0]
    return salida


def test_la_matriz_de_permisos_de_produccion_es_la_acordada() -> None:
    """PRODUCTION_PERMISSION_MATRIX.

    El taller ejecuta —crear, arrancar, completar— y la administracion decide
    —anular—. Si alguien mueve una de las cuatro de sitio, esta prueba lo dice
    por su nombre en vez de dejarlo pasar a produccion.
    """
    assert _dependencias() == PERMISOS_ESPERADOS


def test_anular_no_se_amplia_al_taller_por_descuido() -> None:
    """La unica de las cuatro que sigue restringida, dicha aparte.

    Se comprueba sola porque es la que mas tienta ampliar: el boton esta al
    lado de los otros tres y parece del mismo tipo.
    """
    assert _dependencias()["cancel_production_order"] == "admin"


async def test_un_rol_ajeno_al_taller_no_registra_comunicaciones() -> None:
    """Fase 010I, bloque D. Hoy solo existen ADMIN y OPERATOR, y los dos pueden;
    el 403 se comprueba sobre la dependencia real con un rol que no es de taller,
    para que un tercer rol futuro no herede el permiso sin decidirlo."""
    import uuid

    import pytest

    from app.api.deps import require_roles
    from app.core.errors import APIError
    from app.models.profile import UserRole
    from app.schemas.auth import AuthenticatedUser

    taller = require_roles(UserRole.ADMIN, UserRole.OPERATOR)
    ajeno = AuthenticatedUser.model_construct(
        id=uuid.uuid4(), email="x@example.com", display_name="Visita", role="VIEWER"
    )
    with pytest.raises(APIError) as error:
        await taller(ajeno)
    assert error.value.status_code == 403
