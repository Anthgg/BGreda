"""El puente entre la muestra aprobada y la cotizacion final (Fase 009K.1).

Vive en su propio modulo, y no dentro de `PrototypeService` ni de
`QuotationBuilderService`, porque conectar dos dominios no es lo mismo que
fundirlos. Si el prototipo importara el Cotizador entero acabaria sabiendo de
margenes; si el Cotizador importara los prototipos acabaria sabiendo de
inventario de muestras. Aqui cada uno expone lo suyo y el puente traduce.

Lo que traduce es POCO a proposito. Solo cruzan datos fisicos que la muestra
aprobada demostro:

- el producto, si la muestra colgaba de uno;
- las medidas, y solo desde la ficha estructurada;
- el material del cuerpo, y solo cuando no hay ninguna ambiguedad.

Todo lo comercial —cantidad final, cliente, margen, precio, moneda, quema— lo
pone una persona despues. Una muestra dice como se hace una pieza, no cuantas
van a pedir ni a que precio.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.prototypes import (
    Prototype,
    PrototypeApproval,
    PrototypeMaterialLine,
    PrototypeMaterialRole,
    PrototypeStatus,
)
from app.models.quotations import Quotation, QuotationStatus
from app.schemas.auth import AuthenticatedUser
from app.schemas.quotation_builder import (
    BodyMaterialIn,
    ProductDimensionCompletionIn,
    QuotationBuilderCreateIn,
    QuotationBuilderItemIn,
    QuotationBuilderOut,
)
from app.services.audit import AuditRecorder
from app.services.prototypes import PrototypeService
from app.services.quotation_builder import QuotationBuilderService

BRIDGE_ENTITY = "prototype_quotation"

#: Unidades que el Cotizador sabe transportar sin inventarse una conversion.
#: `und` y `%` existen en el cuaderno del taller y se guardan, pero no pueden
#: convertirse en «cantidad de material por pieza» sin una regla que nadie ha
#: escrito: ahi el puente no precarga y se lo deja a una persona.
BRIDGEABLE_UOMS = frozenset({"g", "kg", "ml", "l"})


class PrototypeNotApprovedForQuotationError(APIError):
    """La muestra todavia no autoriza cotizar.

    Es 409 y no 403: no falta permiso, falta que la muestra este terminada y
    aprobada. Un administrador no puede arreglarlo dando permisos.
    """

    status_code = 409
    code = "PROTOTYPE_NOT_APPROVED_FOR_QUOTATION"
    message = "Solo una muestra completada y aprobada puede originar la cotizacion final"


class PrototypeSupersededForQuotationError(APIError):
    """Hay una iteracion posterior: cotizar desde esta seria cotizar lo viejo."""

    status_code = 409
    code = "PROTOTYPE_SUPERSEDED_FOR_QUOTATION"
    message = "Esta muestra fue sustituida por otra; cotice desde la iteracion vigente"


@dataclass(frozen=True)
class BodyMaterialPrefill:
    """El material del cuerpo, cuando la muestra lo dice sin ambiguedad."""

    product_id: int
    quantity_per_piece: Decimal


def _body_line(prototype: Prototype) -> PrototypeMaterialLine | None:
    """La UNICA linea declarada como cuerpo, o nada.

    Cero lineas BODY significa que nadie lo declaro —todas las muestras
    anteriores a 0022 estan asi—. Dos o mas significa que la muestra llevaba
    varios cuerpos y el puente no puede elegir por su cuenta. En ambos casos la
    respuesta correcta es no precargar: adivinar aqui produce una cotizacion
    con el material equivocado y un numero creible al lado.
    """
    cuerpos = [line for line in prototype.lines if line.material_role is PrototypeMaterialRole.BODY]
    return cuerpos[0] if len(cuerpos) == 1 else None


def body_material_prefill(prototype: Prototype) -> BodyMaterialPrefill | None:
    """Cuanto material de cuerpo lleva UNA pieza, segun lo que la muestra gasto.

    Se divide el consumo REAL entre las piezas de muestra. Lo previsto no
    sirve: la cotizacion final tiene que heredar lo que de verdad costo hacerla,
    no lo que se penso que costaria. Si el consumo real no consta —porque la
    muestra es anterior a 0022 o porque nunca llego a arrancar— no se cae a lo
    previsto en silencio: se deja sin precargar.
    """
    linea = _body_line(prototype)
    if linea is None or linea.quantity_actual is None or linea.quantity_actual <= 0:
        return None
    if prototype.quantity <= 0:
        return None
    if linea.uom_code not in BRIDGEABLE_UOMS:
        return None
    return BodyMaterialPrefill(
        product_id=linea.product_id,
        quantity_per_piece=linea.quantity_actual / Decimal(prototype.quantity),
    )


def dimensions_prefill(prototype: Prototype) -> ProductDimensionCompletionIn:
    """Las medidas aprobadas, y solo desde la ficha estructurada.

    `notes` no se lee. Es texto que compone el navegador, y construir una
    cotizacion partiendolo con expresiones regulares ataria el backend a un
    formato que no controla: bastaria que alguien editara la nota para que las
    medidas empezaran a salir mal.

    Una muestra anterior a 0022 no tiene ficha, y entonces no hay medidas que
    precargar. No es un defecto: es que ese dato nunca existio estructurado.
    """
    ficha = prototype.technical_specifications or {}

    def _medida(clave: str) -> Decimal | None:
        valor = ficha.get(clave)
        if valor is None:
            return None
        try:
            numero = Decimal(str(valor))
        except (ArithmeticError, ValueError):
            return None
        return numero if numero > 0 else None

    return ProductDimensionCompletionIn(
        width=_medida("width_cm"),
        height=_medida("height_cm"),
        length=_medida("length_cm"),
        depth=_medida("depth_cm"),
    )


class PrototypeQuotationBridge:
    """Crea la cotizacion final a partir de una muestra aprobada."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        prototypes: PrototypeService,
        builder: QuotationBuilderService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._prototypes = prototypes
        self._builder = builder

    async def create_final_quotation(
        self, prototype_id: int, *, user: AuthenticatedUser
    ) -> tuple[QuotationBuilderOut, bool]:
        """Devuelve `(cotizacion, se_creo_ahora)`.

        El segundo valor es lo que hace la accion idempotente de verdad: pulsar
        dos veces, o que el navegador reintente por un timeout, devuelve la
        misma cotizacion en vez de dejar dos borradores gemelos que nadie sabe
        cual vale.

        La muestra se bloquea con `FOR UPDATE` antes de mirar si ya hay
        borrador. Sin ese cerrojo, dos peticiones simultaneas leerian las dos
        «no hay» y entrarian las dos a crear; el indice unico parcial las
        atraparia, pero una de ellas se llevaria un error de integridad en la
        cara en vez de la cotizacion que pidio.
        """
        prototype = await self._prototypes.get(prototype_id, for_update=True)

        if (
            prototype.status is not PrototypeStatus.COMPLETED
            or prototype.approval is not PrototypeApproval.APPROVED
        ):
            raise PrototypeNotApprovedForQuotationError()

        # La vigente de la cadena, no una predecesora rechazada que ademas
        # esta completada: cotizar desde ella seria cotizar la muestra que
        # justamente no valio.
        vigente = await self._prototypes.current_effective(prototype)
        if vigente.id != prototype.id:
            raise PrototypeSupersededForQuotationError()

        existente = await self._active_draft(prototype.id)
        if existente is not None:
            return await self._builder.get(existente.id), False

        payload = self._prefill(prototype)
        creada = await self._builder.create(payload, user=user)
        assert creada.id is not None

        fila = await self._session.get(Quotation, creada.id)
        assert fila is not None
        # Se asigna la MUESTRA, no su id. La fila se cargo cuando el origen
        # todavia era nulo, asi que la relacion quedo resuelta como «ninguna»;
        # tocar solo la columna deja ese None cacheado y la respuesta sale sin
        # el codigo. Asignar el objeto actualiza las dos cosas a la vez.
        fila.origin_prototype = prototype
        await self._session.flush()

        self._audit.record_action(
            entity_type=BRIDGE_ENTITY,
            entity_id=str(creada.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "prototype_id": prototype.id,
                "prototype_code": prototype.code,
                "quotation_id": creada.id,
                "quotation_code": creada.code,
            },
        )
        return await self._builder.get(creada.id), True

    async def _active_draft(self, prototype_id: int) -> Quotation | None:
        """El borrador vivo de esta muestra, si lo hay.

        Solo DRAFT. Una confirmada o una anulada son historia y no estorban:
        recotizar la misma muestra mas adelante es legitimo.
        """
        return (
            await self._session.execute(
                select(Quotation).where(
                    Quotation.origin_prototype_id == prototype_id,
                    Quotation.status == QuotationStatus.DRAFT,
                )
            )
        ).scalar_one_or_none()

    def _prefill(self, prototype: Prototype) -> QuotationBuilderCreateIn:
        """Lo unico que cruza: producto, medidas y material del cuerpo.

        No viajan la cantidad ni el cliente. La cantidad de muestra es cuantas
        piezas se hicieron para mirarlas, no cuantas va a pedir nadie; y el
        cliente de la cotizacion que autorizo la muestra no tiene por que ser
        el del pedido final.

        Los esmaltes tampoco: la muestra registra gramos absolutos y el
        Cotizador reparte un total por pesos relativos. No hay traduccion
        honesta entre las dos cosas, asi que el plan de esmaltes se configura
        como en cualquier otra cotizacion.
        """
        items: list[QuotationBuilderItemIn] = []
        if prototype.product_id is not None:
            medidas = dimensions_prefill(prototype)
            cuerpo = body_material_prefill(prototype)
            items.append(
                QuotationBuilderItemIn(
                    product_id=prototype.product_id,
                    dimensions=medidas,
                    # Las medidas de la muestra son de ESTA pieza, no del
                    # catalogo: entran como personalizadas y el maestro no se
                    # toca.
                    dimensions_overridden=any(
                        valor is not None for valor in medidas.model_dump().values()
                    ),
                    body_material=(
                        BodyMaterialIn(
                            product_id=cuerpo.product_id,
                            quantity_per_piece=cuerpo.quantity_per_piece,
                        )
                        if cuerpo is not None
                        else None
                    ),
                )
            )

        return QuotationBuilderCreateIn(name=prototype.name, items=items)
