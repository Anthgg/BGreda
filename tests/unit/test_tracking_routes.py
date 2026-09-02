"""Fase 009I.1 — el inventario de la superficie PUBLICA de seguimiento.

Esta superficie no exige sesion, asi que lo que se declare aqui queda expuesto
a internet. Por eso su forma se fija por escrito y no se deja a la revision de
quien haga el siguiente cambio: la lista de rutas y de metodos es un contrato,
y anadir un POST bajo este prefijo tiene que romper una prueba, no pasar
desapercibido en un diff.

No hace falta PostgreSQL para comprobar que un endpoint publico no muta.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.api.deps import get_current_user
from app.api.v1.tracking import router

#: Toda la superficie publica de 009I.1, con sus metodos.
ESPERADAS = {
    ("/tracking/production-orders/scan/{qr_token}", "GET"),
    ("/tracking/production-orders/current", "GET"),
    ("/tracking/production-orders/current/document", "GET"),
    ("/tracking/production-orders/current/internal-link", "GET"),
}

#: La unica ruta de esta superficie que exige sesion. Traduce el token del QR a
#: un identificador interno, que es justo lo que no puede darse a cualquiera.
CON_SESION = "/tracking/production-orders/current/internal-link"


def _rutas() -> list[APIRoute]:
    return [ruta for ruta in router.routes if isinstance(ruta, APIRoute)]


def _pares() -> set[tuple[str, str]]:
    return {
        (ruta.path, metodo) for ruta in _rutas() for metodo in ruta.methods - {"HEAD", "OPTIONS"}
    }


def _exige_sesion(ruta: APIRoute) -> bool:
    return any(
        dependencia.call is get_current_user for dependencia in ruta.dependant.dependencies
    ) or any(
        sub.call is get_current_user
        for dependencia in ruta.dependant.dependencies
        for sub in dependencia.dependencies
    )


def test_estan_declaradas_todas_las_rutas_publicas() -> None:
    assert _pares() == ESPERADAS


def test_la_superficie_publica_no_tiene_ni_una_escritura() -> None:
    """QR_PUBLIC_MUTATION_ENDPOINTS: 0.

    Ni arrancar, ni completar, ni anular, ni ajustar existencia. Un GET que
    escribe seria igual de grave, pero eso no lo puede ver esta prueba; lo que
    si puede es impedir que exista el verbo.
    """
    metodos = {metodo for _ruta, metodo in _pares()}
    assert metodos == {"GET"}
    assert not metodos & {"POST", "PUT", "PATCH", "DELETE"}


def test_el_seguimiento_y_el_documento_no_exigen_sesion() -> None:
    """QR_PUBLIC_WITHOUT_LOGIN.

    Es el motivo entero de la subfase: hasta 009J el QR llevaba a una ruta con
    sesion y quien escaneaba sin cuenta acababa en el login.
    """
    sin_sesion = {ruta.path for ruta in _rutas() if not _exige_sesion(ruta)}
    assert sin_sesion == {ruta for ruta, _ in ESPERADAS} - {CON_SESION}


def test_solo_el_puente_a_la_vista_interna_exige_sesion() -> None:
    con_sesion = {ruta.path for ruta in _rutas() if _exige_sesion(ruta)}
    assert con_sesion == {CON_SESION}
