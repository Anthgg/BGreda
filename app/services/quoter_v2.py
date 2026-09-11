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
from app.models.masters import Partner
from app.models.quoter_v2 import V2ProductionType, V2Quotation, V2QuotationStatus
from app.models.sequence import SequenceType
from app.schemas.auth import AuthenticatedUser
from app.services.sequences import SequenceService

#: Tope de pagina del listado. El mismo criterio que el resto de la API: una
#: peticion sin limite es una peticion que un dia devuelve la tabla entera.
MAX_PAGE_SIZE = 200


class V2QuotationNotFoundError(APIError):
    status_code = 404
    code = "V2_QUOTATION_NOT_FOUND"
    message = "La cotizacion V2 no existe"


class V2CustomerNotFoundError(APIError):
    status_code = 404
    code = "V2_CUSTOMER_NOT_FOUND"
    message = "El cliente indicado no existe"


class V2QuotationService:
    """Alta y consulta de cotizaciones del motor V2."""

    def __init__(self, session: AsyncSession, sequences: SequenceService) -> None:
        self._session = session
        self._sequences = sequences

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
        customer_id = data.get("customer_id")
        customer_name: str | None = None
        if customer_id is not None:
            customer = await self._session.get(Partner, customer_id)
            if customer is None:
                raise V2CustomerNotFoundError()
            customer_name = customer.name

        fila = V2Quotation(
            code=await self._sequences.issue(SequenceType.QUOTE_V2, user_id=user.id),
            # Explicito aunque el default del modelo diga lo mismo: es la linea
            # que hace verdadera la frase «V2 tiene identidad persistida».
            pricing_engine_version=PricingEngineVersion.V2,
            status=V2QuotationStatus.DRAFT,
            production_type=V2ProductionType(data.get("production_type", V2ProductionType.RETAIL)),
            customer_id=customer_id,
            customer_name_snapshot=customer_name,
            name=data.get("name"),
            notes=data.get("notes"),
            created_by=user.id,
            created_by_name=user.display_name,
        )
        self._session.add(fila)
        await self._session.flush()
        return fila

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
