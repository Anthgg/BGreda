"""Servicio base del Cotizador V2.

Fase 010A. Crea, lee y lista cabeceras V2. Nada mas: **no calcula**. Cuando
lleguen los motores de 010C a 010F entraran aqui, en un servicio que nunca ha
llamado a Legacy y que por tanto no puede heredar sin querer una formula suya.

## Aislamiento

Este modulo importa maestros compartidos (`partners`, secuencias) y su propio
modelo. No importa —ni debe importar nunca— `app.services.quotations`,
`app.services.quotation_builder`, `app.core.quotations`, `app.core.pricing` ni
`app.models.quotations`. `tests/unit/test_quoter_v2_isolation.py` lo comprueba
leyendo los imports del fichero, de modo que el dia que alguien anada uno «solo
para reutilizar esta funcioncita» la prueba se pone roja antes que el precio.

El sentido de la regla no es purismo: es que Legacy lleva anos de decisiones
acumuladas —el factor por ocupacion de horno, entre ellas— y V2 nace
explicitamente sin ellas. Compartir codigo es la via mas rapida para que
vuelvan por la puerta de atras.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.pricing_engine import PricingEngineVersion
from app.models.audit import AuditAction
from app.models.masters import Partner, PartnerRole
from app.models.quoter_v2 import V2ProductionType, V2Quotation, V2QuotationStatus
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2_settings import V2SettingsService
from app.services.sequences import SequenceService

#: Tope de pagina del listado. El mismo criterio que el resto de la API: una
#: peticion sin limite es una peticion que un dia devuelve la tabla entera.
MAX_PAGE_SIZE = 200

#: Entidad con la que se audita una cotizacion V2. Distinta de la de Legacy
#: —"quotation"— porque son documentos de motores distintos: mezclarlas haria
#: que el historial de uno apareciera dentro del otro.
V2_QUOTATION_ENTITY = "v2_quotation"

#: Roles de tercero que pueden ser el cliente de una cotizacion. El mismo
#: criterio que aplica el Cotizador historico. Se repite aqui en vez de
#: importarlo de alli: el dato compartido es el maestro `partners`, no el
#: servicio Legacy.
CUSTOMER_ROLES = (PartnerRole.CLIENT, PartnerRole.BOTH)


#: Todo lo que `update_draft` puede llegar a mover, directa o indirectamente.
#:
#: Incluye lo que el cuerpo nombra y tambien lo que se mueve en cascada: el
#: horno que arrastra el tipo de produccion y las tarifas que se retiran al
#: cambiar de tipo de cliente. Sirve para saber si la peticion cambio algo de
#: verdad, y por tanto si hay una edicion que auditar.
CAMPOS_DE_CABECERA = (
    "customer_id",
    "customer_name_snapshot",
    "name",
    "notes",
    "customer_kind",
    "production_type",
    "currency_code_snapshot",
    "currency_symbol_snapshot",
    "exchange_rate_snapshot",
    "kiln_id",
    "kiln_name_snapshot",
    "kiln_capacity_snapshot",
    "gas_cost_low_snapshot",
    "gas_cost_high_snapshot",
    "commercial_rate_low_snapshot",
    "commercial_rate_high_snapshot",
    "gas_low_is_override",
    "gas_high_is_override",
    "commercial_low_is_override",
    "commercial_high_is_override",
)


def _cabecera(quotation: V2Quotation) -> dict[str, Any]:
    """Foto de la cabecera, para comparar antes y despues."""
    return {campo: getattr(quotation, campo) for campo in CAMPOS_DE_CABECERA}


class V2QuotationNotFoundError(APIError):
    status_code = 404
    code = "V2_QUOTATION_NOT_FOUND"
    message = "La cotizacion V2 no existe"


class V2CustomerNotFoundError(APIError):
    status_code = 404
    code = "V2_CUSTOMER_NOT_FOUND"
    message = "El cliente indicado no existe o esta archivado"


class V2CustomerRoleError(APIError):
    status_code = 422
    code = "V2_CUSTOMER_ROLE_REQUIRED"
    message = "El tercero seleccionado no tiene rol de cliente"


class V2QuotationNotEditableError(APIError):
    """Una cotizacion que ya no es borrador no cambia de cabecera."""

    status_code = 409
    code = "V2_QUOTATION_NOT_EDITABLE"
    message = "La cotizacion ya no es un borrador y sus datos no pueden cambiarse"


class V2QuotationService:
    """Alta y consulta de cotizaciones del motor V2."""

    def __init__(
        self,
        session: AsyncSession,
        sequences: SequenceService,
        audit: AuditRecorder,
        settings: V2SettingsService,
    ) -> None:
        self._session = session
        self._sequences = sequences
        self._audit = audit
        self._settings = settings

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------
    async def create_draft(self, data: dict[str, Any], *, user: AuthenticatedUser) -> V2Quotation:
        """Abre un borrador V2 y le asigna su correlativo propio.

        El codigo se emite aqui y no al confirmar porque una cotizacion V2 se
        trabaja durante dias y el equipo necesita nombrarla mientras tanto. El
        ``commit`` es del llamador: correlativo y documento se confirman juntos
        o no se confirma ninguno.
        """
        customer = await self._customer(data.get("customer_id"))

        # Fase 010B. La configuracion se COPIA aqui, una vez. A partir de este
        # instante la cotizacion vive de su copia: mover un default manana no
        # reescribe lo que hoy se presupuesto, y corregir un numero dentro de
        # esta cotizacion no cambia el default de la casa.
        snapshot = await self._settings.capture_snapshot(
            currency_code=data.get("currency_code"),
            exchange_rate=data.get("exchange_rate"),
            commercial_factor=data.get("commercial_factor"),
            customer_kind=data.get("customer_kind"),
            production_type=data.get("production_type"),
        )

        fila = V2Quotation(
            code=await self._sequences.issue(SequenceType.QUOTE_V2, user_id=user.id),
            # Explicito aunque el default del modelo diga lo mismo: es la linea
            # que hace verdadera la frase «V2 tiene identidad persistida».
            pricing_engine_version=PricingEngineVersion.V2,
            status=V2QuotationStatus.DRAFT,
            customer_id=customer.id if customer else None,
            customer_name_snapshot=customer.name if customer else None,
            name=data.get("name"),
            notes=data.get("notes"),
            created_by=user.id,
            created_by_name=user.display_name,
            # `production_type` sale del snapshot: si el alta no lo dice, lo
            # pone la configuracion. Nunca se deduce de la cantidad.
            **snapshot,
        )
        self._session.add(fila)
        await self._session.flush()
        self._audit.record_action(
            entity_type=V2_QUOTATION_ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": fila.code,
                "pricing_engine_version": PricingEngineVersion.V2.value,
                "production_type": fila.production_type.value,
            },
        )
        return fila

    async def update_draft(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> V2Quotation:
        """Cambia la CABECERA de un borrador. Fase 010G.

        Existe porque el flujo deja volver atras: quien esta eligiendo el horno
        puede darse cuenta de que el cliente esta mal y regresar al primer paso.
        Sin esta ruta el unico remedio era abrir otra cotizacion.

        Lo que NO hace: tocar un maestro. Cambiar aqui el cliente o la moneda
        afecta a ESTA cotizacion y a ninguna otra, ni a la configuracion.
        """
        quotation = await self._draft(quotation_id)
        antes = _cabecera(quotation)

        if "customer_id" in data:
            customer = await self._customer(data["customer_id"])
            quotation.customer_id = customer.id if customer else None
            quotation.customer_name_snapshot = customer.name if customer else None
        for campo in ("name", "notes"):
            if campo in data:
                setattr(quotation, campo, data[campo])
        if "customer_kind" in data and data["customer_kind"] is not None:
            # Cambiar a quien se cotiza reevalua lo que se COBRA, y solo eso: el
            # gas no depende del cliente, porque un alumno y un externo queman
            # el mismo. Los acuerdos de tarifa tampoco se heredan, porque se
            # pactaron para el otro tipo de cliente.
            #
            # Es la misma regla que aplica 010E cuando el tipo de cliente se
            # cambia desde el paso de la quema. Se repite aqui porque desde
            # 010G hay DOS puertas al mismo campo, y una puerta que no aplique
            # la regla deja una cotizacion de alumno cobrando tarifa de externo.
            if data["customer_kind"] != quotation.customer_kind:
                quotation.commercial_rate_low_snapshot = None
                quotation.commercial_rate_high_snapshot = None
                quotation.commercial_low_is_override = False
                quotation.commercial_high_is_override = False
            quotation.customer_kind = data["customer_kind"]

        # La moneda se recongela entera: el simbolo y el tipo de cambio van con
        # ella, y la base exige que las tres columnas sean coherentes.
        if "currency_code" in data or "exchange_rate" in data:
            # `in` y no `or`: presente y en nulo RETIRA el tipo de cambio
            # pactado y devuelve el de la configuracion, que es lo que
            # significa un nulo en toda la familia V2. Con `or`, un cero o un
            # nulo explicitos se confundian con «no lo mandaron» y el acuerdo
            # anterior se quedaba puesto para siempre.
            tasa = (
                data["exchange_rate"]
                if "exchange_rate" in data
                else quotation.exchange_rate_snapshot
            )
            codigo = (
                data["currency_code"]
                if data.get("currency_code")
                else quotation.currency_code_snapshot
            )
            moneda = await self._settings.currency_snapshot(codigo, tasa)
            for campo, valor in moneda.items():
                setattr(quotation, campo, valor)

        if "production_type" in data and data["production_type"] is not None:
            await self._apply_production_type(quotation, data["production_type"])

        await self._session.flush()

        # Una peticion que no movio nada NO es una edicion. El flujo de siete
        # pasos deja volver atras, asi que abrir el paso uno para mirar es
        # normal: si mirar dejara rastro, el historial quedaria ilegible justo
        # para la pregunta que se le hace, que es quien cambio el cliente y
        # cuando. Mismo criterio que `diff_model` en el resto del proyecto.
        cambiados = sorted(
            campo for campo, valor in _cabecera(quotation).items() if antes[campo] != valor
        )
        if not cambiados:
            return quotation

        self._audit.record_action(
            entity_type=V2_QUOTATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "production_type": quotation.production_type.value,
                # Nulo y no la cadena "None": en el JSONB de auditoria
                # `metadata->>'customer_id'` devolveria un identificador de
                # texto valido con valor "None", y los informes contarian
                # cotizaciones sin cliente como si tuvieran uno.
                "customer_id": (
                    str(quotation.customer_id) if quotation.customer_id is not None else None
                ),
                "currency": str(quotation.currency_code_snapshot),
                # Que se toco exactamente. Sin esto, dos eventos seguidos son
                # indistinguibles y hay que adivinar cual movio el precio.
                "campos": ",".join(cambiados),
            },
        )
        return quotation

    async def _apply_production_type(
        self, quotation: V2Quotation, production_type: V2ProductionType
    ) -> None:
        """Cambia por menor / por mayor y mueve el horno SOLO si nadie lo eligio.

        El horno sugerido cuelga del tipo de produccion —chico para por menor,
        grande para por mayor— asi que cambiar el tipo tiene que moverlo. Pero
        si quien cotiza ya habia elegido OTRO horno a mano, esa decision manda:
        pisarla seria justo lo que 010E prohibio, cambiar el horno sin que nadie
        lo pidiera.

        Por eso se compara con el horno que sugeria el tipo ANTERIOR: si coinciden
        es que nadie lo toco, y entonces se mueve al del tipo nuevo.
        """
        anterior = quotation.production_type
        quotation.production_type = production_type
        if anterior is production_type:
            return

        sugerido_antes = await self._settings.suggested_kiln_for(anterior)
        if quotation.kiln_id is not None and (
            sugerido_antes is None or quotation.kiln_id != sugerido_antes.id
        ):
            return

        sugerido_ahora = await self._settings.suggested_kiln_for(production_type)
        # Sin horno sugerido para el tipo nuevo NO se retira el que habia. La
        # configuracion puede no haber nombrado uno para por mayor, y dejar la
        # cotizacion sin horno la costearia en cero: peor que conservar un
        # default que al menos existe y que quien cotiza puede cambiar.
        if sugerido_ahora is None:
            return
        # Y si el horno sugerido resulta ser el MISMO —una sola maquina en el
        # taller, o la misma configurada para los dos tipos— aqui no ha pasado
        # nada: reasignar lo mismo y de paso tirar las tarifas pactadas seria
        # destruir un acuerdo sin que nadie lo pidiera.
        if quotation.kiln_id == sugerido_ahora.id:
            return

        quotation.kiln_id = sugerido_ahora.id
        quotation.kiln_name_snapshot = sugerido_ahora.name
        quotation.kiln_capacity_snapshot = sugerido_ahora.capacity_volume_cm3
        # El horno cambia, y con el las tarifas: las pactadas se pactaron sobre
        # el anterior. Mismo criterio que 010E al cambiar de horno a mano.
        for campo in (
            "gas_cost_low_snapshot",
            "gas_cost_high_snapshot",
            "commercial_rate_low_snapshot",
            "commercial_rate_high_snapshot",
        ):
            setattr(quotation, campo, None)
        for marca in (
            "gas_low_is_override",
            "gas_high_is_override",
            "commercial_low_is_override",
            "commercial_high_is_override",
        ):
            setattr(quotation, marca, False)

    async def _draft(self, quotation_id: int) -> V2Quotation:
        """La cotizacion, bloqueada, si todavia admite cambios de cabecera.

        Mismo criterio que el resto de la familia: leer el estado sin bloquear
        deja una ventana por la que una emision simultanea colaria otro cliente
        en una cotizacion ya comprometida.
        """
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
        if quotation is None:
            raise V2QuotationNotFoundError()
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2QuotationNotEditableError()
        return quotation

    async def _customer(self, customer_id: int | None) -> Partner | None:
        """El tercero que puede ser cliente de esta cotizacion, o ninguno.

        Las mismas dos condiciones que exige el Cotizador historico: que exista
        y no este archivado, y que tenga rol de cliente. No se heredan de alli
        —eso ataria los motores— pero tampoco se relajan: una superficie nueva
        que acepte lo que la vieja rechaza no es un motor nuevo, es un agujero.

        Un proveedor puro colado como cliente terminaria en el PDF que se envia
        al cliente, y un tercero archivado revive por la puerta de atras.
        """
        if customer_id is None:
            return None
        customer = await self._session.get(Partner, customer_id)
        if customer is None or not customer.active:
            raise V2CustomerNotFoundError()
        if customer.role not in CUSTOMER_ROLES:
            raise V2CustomerRoleError()
        return customer

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    async def get(self, quotation_id: int) -> V2Quotation:
        """Una cotizacion V2 por su id.

        Un id de Legacy da 404 aqui, y no la fila equivocada: son tablas
        distintas y los ids no comparten espacio. Esa es justamente la
        propiedad que se quiere —pedir por la ruta V2 nunca devuelve un
        documento del motor viejo—, y la prueba de aislamiento la fija.
        """
        fila = await self._session.get(V2Quotation, quotation_id)
        if fila is None:
            raise V2QuotationNotFoundError()
        return fila

    async def list(
        self,
        *,
        status: V2QuotationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[V2Quotation], int]:
        consulta: Select[tuple[V2Quotation]] = select(V2Quotation)
        total_query = select(func.count()).select_from(V2Quotation)
        if status is not None:
            consulta = consulta.where(V2Quotation.status == status)
            total_query = total_query.where(V2Quotation.status == status)

        consulta = (
            consulta.order_by(V2Quotation.created_at.desc(), V2Quotation.id.desc())
            .limit(min(limit, MAX_PAGE_SIZE))
            .offset(offset)
        )
        filas = list((await self._session.scalars(consulta)).all())
        total = int((await self._session.scalar(total_query)) or 0)
        return filas, total
