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
from app.models.quoter_v2 import V2Quotation, V2QuotationStatus
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
