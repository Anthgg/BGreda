"""Lo que el modelo de prototipos tiene que garantizar sin preguntar a nadie.

Nace de un fallo real de 009K contra PostgreSQL: `POST /prototypes` devolvia
500 al crear una muestra. La causa no estaba en la logica sino en cuando se
lee `prototype.lines`. Mientras la fila no existe, leer esa coleccion es un
acceso en memoria; en cuanto se hace el flush, pasa a ser una consulta, y una
consulta emitida asi en sesion asincrona levanta `MissingGreenlet`.

Se arreglo por los dos lados —la coleccion se inicializa al construir la
muestra, y la relacion carga con `selectin`— y esta prueba fija el segundo,
que es el que cualquiera podria «optimizar» de vuelta sin ver el problema:
volver a la carga perezosa no da un viaje de mas, da un 500.
"""

from __future__ import annotations

from app.models.prototypes import Prototype

#: Toda lectura de una muestra necesita sus materiales: la disponibilidad los
#: recorre, la respuesta los enumera y el resumen los cuenta. No hay ni un caso
#: que gane algo cargandolos tarde.
CARGA_ESPERADA = "selectin"


def test_los_materiales_de_una_muestra_no_se_cargan_tarde() -> None:
    relacion = Prototype.__mapper__.relationships["lines"]
    assert relacion.lazy == CARGA_ESPERADA
