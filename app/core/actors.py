"""Como se llama, en un papel, la persona que firmo un documento.

Un documento comercial guarda el nombre visible de su actor COPIADO en el
momento en que se creo o se emitio. Leerlo es, por tanto, trivial —ya esta
escrito— y esta funcion existe por lo unico que no lo es: que hacer cuando no
hay nada escrito.

Los documentos anteriores a la Fase 009K.2 no registraron a nadie. No se les
puede atribuir un autor sin inventarlo, asi que dicen que no se sabe. Que lo
digan todos igual, y en un solo sitio, es la diferencia entre un hueco honesto
y tres pantallas contando cosas distintas.
"""

from __future__ import annotations

#: Lo que se ensena cuando el documento no registro a nadie. No es un error ni
#: un valor por defecto: es la verdad sobre ese documento.
ACTOR_NO_REGISTRADO = "No registrado"


def nombre_de_actor(snapshot: str | None) -> str:
    """El nombre congelado del actor, o que no se sabe.

    NUNCA consulta el perfil actual. Resolver el nombre vivo aqui haria que un
    documento emitido cambiara de autor cuando esa persona se renombra, y un
    papel que ya se envio a un cliente no puede cambiar.
    """
    limpio = (snapshot or "").strip()
    return limpio or ACTOR_NO_REGISTRADO
