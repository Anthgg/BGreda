"""Con que motor se calculo una cotizacion.

Fase 010A. Hasta aqui solo habia un motor, asi que ninguna cotizacion
necesitaba decir cual: todas se calculaban con el mismo codigo. Desde que
existe un segundo —el Cotizador V2— eso deja de ser cierto, y un documento que
no dice con que motor nacio es un documento que nadie puede volver a explicar.

El valor **se persiste en la fila**. No se deduce de la fecha de creacion, ni
del prefijo del correlativo, ni de si tal columna esta rellena. Una heuristica
de ese tipo acierta con las filas que hay hoy y falla con la primera que no se
parezca a las de su epoca; y cuando falle, lo hara sobre un precio que alguien
ya envio a un cliente.

``LEGACY`` es el motor acumulado hasta 009K —el que emiten `quotations` y el
Cotizador multiproducto—. ``V2`` es el motor nuevo, que vive en su propio
dominio. Los dos conviven: esta fase NO retira Legacy, NO recalcula nada suyo y
NO migra sus resultados.

La separacion la garantiza la base de datos, no la buena voluntad del codigo:
cada tabla lleva un CHECK que solo admite su propio motor, de modo que una
cotizacion Legacy no puede declararse V2 ni al reves, ni siquiera por un INSERT
escrito a mano.
"""

from __future__ import annotations

from enum import StrEnum

#: Longitud de la columna que guarda el motor. Sobra para los dos valores
#: actuales; ensanchar un varchar en PostgreSQL no reescribe la tabla, pero
#: estrecharlo si obliga a revisar cada fila.
PRICING_ENGINE_VERSION_LENGTH = 16


class PricingEngineVersion(StrEnum):
    """Motor de calculo con el que se produjo una cotizacion."""

    #: Cotizador historico (Fases 005 a 009K). Se conserva tal cual.
    LEGACY = "LEGACY"
    #: Cotizador V2 (familia 010). Motor nuevo, dominio propio.
    V2 = "V2"
