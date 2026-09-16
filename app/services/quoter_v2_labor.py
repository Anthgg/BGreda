"""Mano de obra del Cotizador V2: quien trabaja, cuanto tarda y cuanto cuesta.

Fase 010D. Cuatro responsabilidades, y conviene separarlas al leer:

1. **mantener los maestros** —trabajadores y tecnicas—, que son politica del
   taller y valen para todo lo que se cotice despues;
2. **resolver la jornada** de cada persona: la suya si la declara, la del
   taller si no. Una sola fuente, nunca dos;
3. **congelar** en la tarea todo lo que hizo falta para calcular su costo;
4. **avisar** cuando lo asignado a alguien no cabe en su jornada, sin decidir
   por el usuario que hacer al respecto.

## Lo que este modulo NO hace

**No aprende.** El rendimiento de una tecnica es un estandar configurado. Que
alguien haga hoy 70 piezas donde el estandar dice 50 no lo sube, y que haga 40
no lo baja. Ajustarlo es una decision de taller que se escribe a mano.

**No decide por quien planifica.** Diez horas en una jornada de ocho producen un
AVISO. Si eso se resuelve con un dia largo, con dos dias o con mas gente lo
elige una persona: aqui no hay recargo nocturno, ni hora extra, ni
multiplicador.

**No acelera por contar cabezas.** Anadir personal suma costo y no resta plazo.
Que dos personas tarden la mitad es una decision de planificacion, no una
consecuencia aritmetica, y suponerlo prometeria al cliente una fecha que el
taller no acordo.

**No cobra jornadas.** Cobra horas. Tres horas de torno son tres horas, y si la
misma persona hace ademas asas y vidriado para el mismo pedido, eso es UNA
jornada repartida y no tres.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.quoter_v2_labor import (
    LaborMathError,
    exceeds_workday,
    hourly_rate,
    hours_required,
    labor_cost,
    minimum_work_days,
    quantize_hours,
    quantize_rate,
)
from app.models.audit import AuditAction
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct, V2QuotationStatus
from app.models.quoter_v2_labor import (
    V2QuotationLabor,
    V2Technique,
    V2Worker,
    V2WorkerTechnique,
    V2WorkerType,
)
from app.models.quoter_v2_processes import V2QuotationProcess
from app.models.quoter_v2_settings import V2CommercialSettings
from app.models.settings import SINGLETON_ID
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2_pricing import refresh_pricing

ZERO = Decimal(0)

#: Entidades de auditoria propias. Cambiar el jornal de una persona es una
#: decision laboral, y no es lo mismo que asignarle una tarea en un borrador.
V2_WORKER_ENTITY = "v2_worker"
V2_TECHNIQUE_ENTITY = "v2_technique"
V2_LABOR_ENTITY = "v2_quotation_labor"
V2_ILLUSTRATION_ENTITY = "v2_quotation_illustration"

#: Avisos. No bloquean: un borrador a medias tiene que poder guardarse.
WARN_WORKDAY_EXCEEDED = "V2_LABOR_WORKDAY_EXCEEDED"
WARN_WORKER_UNAVAILABLE = "V2_LABOR_WORKER_UNAVAILABLE"
WARN_TECHNIQUE_UNAVAILABLE = "V2_LABOR_TECHNIQUE_UNAVAILABLE"
WARN_GLAZE_TECHNIQUE_WITHOUT_GLAZE = "V2_LABOR_GLAZE_TECHNIQUE_WITHOUT_GLAZE"
#: Correccion 010H. La tarea de un borrador usa una tecnica que el trabajador ya
#: no tiene habilitada en su maestro. Avisa y no bloquea: la tarea conserva lo
#: que congelo, igual que con un trabajador dado de baja.
WARN_TECHNIQUE_NOT_ENABLED = "V2_LABOR_TECHNIQUE_NOT_ENABLED"
#: La tecnica ya es un proceso vivo de la pieza: se salta en vez de duplicarla.
WARN_TECHNIQUE_IS_PROCESS = "V2_LABOR_TECHNIQUE_IS_PROCESS"
#: El trabajador elegido no tiene ninguna tecnica activa habilitada.
WARN_WORKER_WITHOUT_TECHNIQUES = "V2_LABOR_WORKER_WITHOUT_TECHNIQUES"


class V2WorkerNotFoundError(APIError):
    status_code = 404
    code = "V2_WORKER_NOT_FOUND"
    message = "El trabajador no existe"


class V2TechniqueNotFoundError(APIError):
    status_code = 404
    code = "V2_TECHNIQUE_NOT_FOUND"
    message = "La tecnica no existe"


class V2LaborNotFoundError(APIError):
    status_code = 404
    code = "V2_LABOR_NOT_FOUND"
    message = "La tarea no existe en esta cotizacion"


class V2LaborInputInvalid(APIError):
    """Una entrada imposible, traducida a una respuesta que se entiende."""

    status_code = 422
    code = "V2_LABOR_INPUT_INVALID"
    message = "Los datos no permiten calcular la mano de obra"


class V2LaborResourceInactiveError(APIError):
    """Elegir HOY a alguien dado de baja, o una tecnica retirada."""

    status_code = 422
    code = "V2_LABOR_RESOURCE_INACTIVE"
    message = "El trabajador o la tecnica ya no estan activos"


class V2LaborAlreadyAProcessError(APIError):
    """Ese trabajo ya es un proceso de la pieza.

    Crear ademas una tarea suelta con la misma tecnica sobre la misma linea
    cobraria dos veces lo mismo: el proceso ya tiene -o tendra- la suya. Para
    ponerle a alguien se asigna el proceso; para traer a una persona de mas se
    marca personal adicional, que es otra cosa.
    """

    status_code = 409
    code = "V2_LABOR_ALREADY_A_PROCESS"
    message = "Ese trabajo ya es un proceso de la pieza: asigne el trabajador en el proceso"


class V2LaborTechniqueNotAllowedError(APIError):
    """Correccion 010H. Ese trabajador no tiene habilitada esa tecnica.

    La pantalla solo ofrece las habilitadas, pero la pantalla no es una barrera:
    un request escrito a mano llega igual. El backend es la autoridad.
    """

    status_code = 422
    code = "V2_LABOR_TECHNIQUE_NOT_ALLOWED"
    message = "El trabajador no tiene habilitada esa tecnica en su ficha"


class V2LaborVersionConflictError(APIError):
    status_code = 409
    code = "V2_LABOR_VERSION_CONFLICT"
    message = "El registro cambio desde que se abrio. Vuelva a cargarlo y repita el cambio"


class V2LaborQuotationNotEditableError(APIError):
    status_code = 409
    code = "V2_QUOTATION_NOT_EDITABLE"
    message = "La cotizacion ya no es un borrador y su mano de obra no puede cambiarse"


class V2LaborService:
    """Maestros de trabajo y costeo de la mano de obra de una cotizacion."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------
    # La jornada: una sola fuente
    # ------------------------------------------------------------------
    async def global_workday_hours(self) -> Decimal:
        """La jornada del taller, de la configuracion de 010B.

        No se copia a ningun sitio. Copiarla permitiria que las copias se
        separaran de la original sin que nadie lo pidiera, y entonces habria
        varias jornadas y ninguna seria la buena.
        """
        horas = await self._session.scalar(
            select(V2CommercialSettings.workday_hours).where(
                V2CommercialSettings.id == SINGLETON_ID
            )
        )
        if horas is None or horas <= ZERO:
            raise V2LaborInputInvalid(
                "La configuracion del Cotizador V2 no declara una jornada valida",
                code="V2_LABOR_WORKDAY_UNCONFIGURED",
            )
        return Decimal(horas)

    async def resolve_workday_hours(self, worker: V2Worker) -> Decimal:
        """La jornada de una persona: la suya, o la del taller si no declara una."""
        if worker.workday_hours is not None:
            return worker.workday_hours
        return await self.global_workday_hours()

    async def hourly_rate_for(self, worker: V2Worker) -> Decimal:
        """Tarifa por hora, derivada siempre y guardada en ningun maestro.

        Se redondea a la escala en la que se congelara. Devolver aqui un numero
        mas largo del que cabe en la columna haria que el costo no coincidiera
        con la tarifa que despues lee quien revisa la cotizacion.
        """
        try:
            return quantize_rate(
                hourly_rate(worker.daily_rate, await self.resolve_workday_hours(worker))
            )
        except LaborMathError as error:
            raise V2LaborInputInvalid(str(error)) from error

    # ------------------------------------------------------------------
    # Maestro de trabajadores
    # ------------------------------------------------------------------
    async def list_workers(self, *, active_only: bool = False) -> list[V2Worker]:
        consulta = select(V2Worker)
        if active_only:
            consulta = consulta.where(V2Worker.active.is_(True))
        return list((await self._session.scalars(consulta.order_by(V2Worker.name))).all())

    async def get_worker(self, worker_id: int) -> V2Worker:
        worker = await self._session.get(V2Worker, worker_id)
        if worker is None:
            raise V2WorkerNotFoundError()
        return worker

    async def create_worker(self, data: dict[str, Any], *, user: AuthenticatedUser) -> V2Worker:
        tecnicas = data.pop("technique_ids", None) or []
        worker = V2Worker(**data)
        self._session.add(worker)
        await self._session.flush()
        await self._set_capacities(worker.id, tecnicas)
        self._audit.record_action(
            entity_type=V2_WORKER_ENTITY,
            entity_id=str(worker.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "name": worker.name,
                "worker_type": worker.worker_type.value,
                "daily_rate": str(worker.daily_rate),
            },
        )
        return worker

    async def update_worker(
        self,
        worker_id: int,
        data: dict[str, Any],
        *,
        expected_version: int,
        user: AuthenticatedUser,
    ) -> V2Worker:
        """Cambia un trabajador declarando la version que se leyo.

        `with_for_update` porque comprobar la version sin bloquear no basta:
        dos administradores que abran la ficha a la vez leerian la misma, los
        dos pasarian la comprobacion y el ultimo en confirmar borraria el
        cambio del primero sin que nadie viera un conflicto.
        """
        worker = (
            await self._session.scalars(
                select(V2Worker).where(V2Worker.id == worker_id).with_for_update()
            )
        ).one_or_none()
        if worker is None:
            raise V2WorkerNotFoundError()
        if worker.version != expected_version:
            raise V2LaborVersionConflictError()

        anterior = str(worker.daily_rate)
        tecnicas = data.pop("technique_ids", None)
        for campo, valor in data.items():
            setattr(worker, campo, valor)
        if tecnicas is not None:
            # El conjunto se REEMPLAZA, con la version de la ficha: dos
            # administradores que editen capacidades a la vez chocan aqui en
            # vez de mezclar dos listas que ninguno de los dos eligio.
            await self._set_capacities(worker.id, tecnicas)
        worker.version += 1
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_WORKER_ENTITY,
            entity_id=str(worker.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "name": worker.name,
                "daily_rate_before": anterior,
                "daily_rate_after": str(worker.daily_rate),
                "active": str(worker.active),
            },
        )
        return worker

    # ------------------------------------------------------------------
    # Capacidades del trabajador (correccion 010H)
    # ------------------------------------------------------------------
    async def _set_capacities(self, worker_id: int, technique_ids: list[int]) -> None:
        """Deja habilitadas EXACTAMENTE esas tecnicas.

        Las que salen se desactivan, no se borran: una capacidad retirada no
        reescribe una cotizacion que la uso. Las que entran se crean o se
        reactivan. Una tecnica inexistente es un error de la peticion.
        """
        pedidas = set(technique_ids)
        if pedidas:
            existentes = set(
                (
                    await self._session.scalars(
                        select(V2Technique.id).where(V2Technique.id.in_(pedidas))
                    )
                ).all()
            )
            faltan = pedidas - existentes
            if faltan:
                raise V2TechniqueNotFoundError(
                    "Una de las tecnicas indicadas no existe",
                )
        filas = {
            fila.technique_id: fila
            for fila in (
                await self._session.scalars(
                    select(V2WorkerTechnique).where(V2WorkerTechnique.worker_id == worker_id)
                )
            ).all()
        }
        for technique_id, fila in filas.items():
            fila.active = technique_id in pedidas
        for technique_id in sorted(pedidas - set(filas)):
            self._session.add(
                V2WorkerTechnique(worker_id=worker_id, technique_id=technique_id, active=True)
            )
        await self._session.flush()

    async def capacities_of(self, worker_ids: list[int]) -> dict[int, list[int]]:
        """Las tecnicas HABILITADAS de cada trabajador, activas o no en el catalogo."""
        salida: dict[int, list[int]] = {worker_id: [] for worker_id in worker_ids}
        if not worker_ids:
            return salida
        filas = (
            await self._session.execute(
                select(V2WorkerTechnique.worker_id, V2WorkerTechnique.technique_id)
                .where(
                    V2WorkerTechnique.worker_id.in_(worker_ids),
                    V2WorkerTechnique.active.is_(True),
                )
                .order_by(V2WorkerTechnique.technique_id)
            )
        ).all()
        for worker_id, technique_id in filas:
            salida[int(worker_id)].append(int(technique_id))
        return salida

    async def is_enabled(self, worker_id: int, technique_id: int) -> bool:
        return (
            await self._session.scalar(
                select(func.count())
                .select_from(V2WorkerTechnique)
                .where(
                    V2WorkerTechnique.worker_id == worker_id,
                    V2WorkerTechnique.technique_id == technique_id,
                    V2WorkerTechnique.active.is_(True),
                )
            )
            or 0
        ) > 0

    async def load_worker_techniques(
        self,
        quotation_id: int,
        worker_id: int,
        product_line_id: int | None,
        technique_ids: list[int] | None,
        *,
        user: AuthenticatedUser,
    ) -> tuple[list[V2QuotationLabor], list[int], list[str]]:
        """Carga en la cotizacion las tecnicas habilitadas de un trabajador.

        Devuelve `(tareas_creadas, tecnicas_ya_cargadas, avisos)`.

        - Solo tecnicas ACTIVAS del catalogo y HABILITADAS para esa persona.
          `technique_ids`, si viene, es el subconjunto que quien cotiza dejo
          marcado; pedir una no habilitada es un 422, no un silencio.
        - Una tarea por tecnica. La que ya estaba cargada para el mismo
          trabajador y el mismo producto no se duplica: el doble clic y la
          segunda carga devuelven lo mismo. Todo bajo el bloqueo de la
          cotizacion, que serializa dos cargas simultaneas.
        - Piezas: las del producto elegido. «Todo el pedido» nace en cero —sumar
          platos y tazas no es una cantidad de nada— y una tecnica de horas
          manuales tambien, marcada como personal adicional.

        No toca el maestro: quitar despues una tarea es borrar ESA tarea.
        """
        quotation = await self._draft(quotation_id)
        worker = await self.get_worker(worker_id)
        if not worker.active:
            raise V2LaborResourceInactiveError(
                f"«{worker.name}» esta dado de baja y no puede asignarse"
            )
        linea: V2QuotationProduct | None = None
        if product_line_id is not None:
            linea = await self._session.get(V2QuotationProduct, product_line_id)
            if linea is None or linea.v2_quotation_id != quotation.id:
                raise V2LaborInputInvalid("El producto indicado no pertenece a esta cotizacion")

        habilitadas = list(
            (
                await self._session.scalars(
                    select(V2Technique)
                    .join(V2WorkerTechnique, V2WorkerTechnique.technique_id == V2Technique.id)
                    .where(
                        V2WorkerTechnique.worker_id == worker.id,
                        V2WorkerTechnique.active.is_(True),
                        V2Technique.active.is_(True),
                    )
                    .order_by(V2Technique.name, V2Technique.id)
                )
            ).all()
        )
        if technique_ids is not None:
            # Una lista vacia es «no marque ninguna», no «no tiene tecnicas»:
            # confundirlas diria al usuario que falta configurar la ficha.
            if not technique_ids:
                raise V2LaborInputInvalid("Marque al menos una tecnica para anadir")
            pedidas = set(technique_ids)
            no_habilitadas = pedidas - {tecnica.id for tecnica in habilitadas}
            if no_habilitadas:
                raise V2LaborTechniqueNotAllowedError()
            habilitadas = [tecnica for tecnica in habilitadas if tecnica.id in pedidas]
        if not habilitadas:
            return [], [], [WARN_WORKER_WITHOUT_TECHNIQUES]

        condicion_producto = (
            V2QuotationLabor.v2_quotation_product_id.is_(None)
            if product_line_id is None
            else V2QuotationLabor.v2_quotation_product_id == product_line_id
        )
        ya_cargadas = set(
            (
                await self._session.scalars(
                    select(V2QuotationLabor.technique_id).where(
                        V2QuotationLabor.v2_quotation_id == quotation.id,
                        V2QuotationLabor.worker_id == worker.id,
                        condicion_producto,
                    )
                )
            ).all()
        )

        # Las tecnicas que ya son un proceso vivo de esa pieza no se cargan por
        # aqui: el proceso es quien manda, y duplicarlas seria cobrar dos veces.
        procesos_vivos = (
            set()
            if product_line_id is None
            else set(
                (
                    await self._session.scalars(
                        select(V2QuotationProcess.technique_id).where(
                            V2QuotationProcess.v2_quotation_product_id == product_line_id,
                            V2QuotationProcess.removed_at.is_(None),
                        )
                    )
                ).all()
            )
        )

        creadas: list[V2QuotationLabor] = []
        saltadas: list[int] = []
        avisos: list[str] = []
        for tecnica in habilitadas:
            if tecnica.id in ya_cargadas:
                saltadas.append(tecnica.id)
                continue
            if tecnica.id in procesos_vivos:
                saltadas.append(tecnica.id)
                if WARN_TECHNIQUE_IS_PROCESS not in avisos:
                    avisos.append(WARN_TECHNIQUE_IS_PROCESS)
                continue
            piezas = ZERO if tecnica.manual_hours or linea is None else Decimal(linea.quantity)
            tarea, propios = await self.add_labor(
                quotation.id,
                {
                    "worker_id": worker.id,
                    "technique_id": tecnica.id,
                    "v2_quotation_product_id": product_line_id,
                    "quantity": piezas,
                    "is_additional_personnel": tecnica.manual_hours,
                },
                user=user,
            )
            creadas.append(tarea)
            avisos += propios
        return creadas, saltadas, sorted(set(avisos))

    # ------------------------------------------------------------------
    # Maestro de tecnicas
    # ------------------------------------------------------------------
    async def list_techniques(self, *, active_only: bool = False) -> list[V2Technique]:
        consulta = select(V2Technique)
        if active_only:
            consulta = consulta.where(V2Technique.active.is_(True))
        return list((await self._session.scalars(consulta.order_by(V2Technique.name))).all())

    async def get_technique(self, technique_id: int) -> V2Technique:
        tecnica = await self._session.get(V2Technique, technique_id)
        if tecnica is None:
            raise V2TechniqueNotFoundError()
        return tecnica

    async def create_technique(
        self, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> V2Technique:
        tecnica = V2Technique(**data)
        self._session.add(tecnica)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # El codigo es unico. Dos altas simultaneas del mismo codigo son un
            # conflicto de concurrencia, no una averia del servidor.
            raise V2LaborVersionConflictError(
                f"Ya existe una tecnica con el codigo «{data.get('code')}»"
            ) from exc

        self._audit.record_action(
            entity_type=V2_TECHNIQUE_ENTITY,
            entity_id=str(tecnica.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": tecnica.code,
                "name": tecnica.name,
                "capacity": str(tecnica.default_capacity_per_workday),
            },
        )
        return tecnica

    async def update_technique(
        self,
        technique_id: int,
        data: dict[str, Any],
        *,
        expected_version: int,
        user: AuthenticatedUser,
    ) -> V2Technique:
        tecnica = (
            await self._session.scalars(
                select(V2Technique).where(V2Technique.id == technique_id).with_for_update()
            )
        ).one_or_none()
        if tecnica is None:
            raise V2TechniqueNotFoundError()
        if tecnica.version != expected_version:
            raise V2LaborVersionConflictError()

        anterior = str(tecnica.default_capacity_per_workday)
        for campo, valor in data.items():
            setattr(tecnica, campo, valor)
        tecnica.version += 1
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_TECHNIQUE_ENTITY,
            entity_id=str(tecnica.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "code": tecnica.code,
                "capacity_before": anterior,
                "capacity_after": str(tecnica.default_capacity_per_workday),
                "active": str(tecnica.active),
            },
        )
        return tecnica

    # ------------------------------------------------------------------
    # Tareas de una cotizacion
    # ------------------------------------------------------------------
    async def list_labor(self, quotation_id: int) -> list[V2QuotationLabor]:
        consulta = (
            select(V2QuotationLabor)
            .where(V2QuotationLabor.v2_quotation_id == quotation_id)
            .order_by(V2QuotationLabor.sort_order, V2QuotationLabor.id)
        )
        return list((await self._session.scalars(consulta)).all())

    async def add_labor(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> tuple[V2QuotationLabor, list[str]]:
        quotation = await self._draft(quotation_id)
        siguiente = await self._session.scalar(
            select(func.coalesce(func.max(V2QuotationLabor.sort_order), -1) + 1).where(
                V2QuotationLabor.v2_quotation_id == quotation_id
            )
        )
        # Los ceros se ponen aqui y no se dejan al `server_default`: hasta que
        # la fila baja a la base, esas columnas valen `None` en memoria, y la
        # aritmetica que viene a continuacion compara numeros.
        tarea = V2QuotationLabor(
            v2_quotation_id=quotation.id,
            sort_order=int(siguiente or 0),
            quantity=ZERO,
            calculated_hours=ZERO,
            final_hours=ZERO,
            labor_cost=ZERO,
            hours_overridden=False,
            rate_overridden=False,
            is_additional_personnel=False,
        )
        self._session.add(tarea)
        # `no_autoflush`: rellenar la tarea consulta el maestro, y cualquier
        # consulta empuja a la base lo que haya pendiente. La fila todavia esta
        # a medias —sin nombre de trabajador, sin tarifa— y el vuelco fallaria
        # contra los NOT NULL con un error que no dice nada de lo que pasa.
        with self._session.no_autoflush:
            avisos = await self._fill_labor(tarea, data, creando=True)
            await self._check_process_duplicate(tarea)
        await self._session.flush()
        avisos += await self._workday_warning(tarea)
        # Fase 010F. La mano de obra entra en el costo directo de su producto y
        # en la base por horas con la que se reparte el espacio: sin recalcular,
        # el precio quedaria explicandose con un costo que ya no es el suyo.
        avisos += await refresh_pricing(self._session, quotation)

        self._audit.record_action(
            entity_type=V2_LABOR_ENTITY,
            entity_id=str(tarea.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id), "worker": tarea.worker_name_snapshot},
        )
        return tarea, avisos

    async def update_labor(
        self,
        quotation_id: int,
        labor_id: int,
        data: dict[str, Any],
        *,
        user: AuthenticatedUser,
        recalcular: bool = True,
    ) -> tuple[V2QuotationLabor, list[str]]:
        """Cambia una tarea.

        `recalcular=False` es para quien va a tocar VARIAS y recalcula una sola
        vez al final —cambiar la cantidad de un producto mueve todas las tareas
        de sus procesos—. Recalcular una vez por tarea daria el mismo numero al
        final por un precio mucho mas caro.
        """
        quotation = await self._draft(quotation_id)
        tarea = await self._labor(quotation_id, labor_id)
        # Mismo motivo que al crear: cambiar de trabajador deja la fila en un
        # estado intermedio mientras se resuelve el nuevo.
        with self._session.no_autoflush:
            avisos = await self._fill_labor(tarea, data, creando=False)
        await self._session.flush()
        avisos += await self._workday_warning(tarea)
        if recalcular:
            avisos += await refresh_pricing(self._session, quotation)

        self._audit.record_action(
            entity_type=V2_LABOR_ENTITY,
            entity_id=str(tarea.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        return tarea, avisos

    async def delete_labor(
        self, quotation_id: int, labor_id: int, *, user: AuthenticatedUser
    ) -> None:
        quotation = await self._draft(quotation_id)
        tarea = await self._labor(quotation_id, labor_id)
        await self._session.delete(tarea)
        self._audit.record_action(
            entity_type=V2_LABOR_ENTITY,
            entity_id=str(labor_id),
            action=AuditAction.DELETE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        await self._session.flush()
        await refresh_pricing(self._session, quotation)

    # ------------------------------------------------------------------
    # Congelado de una tarea
    # ------------------------------------------------------------------
    async def _fill_labor(
        self, tarea: V2QuotationLabor, data: dict[str, Any], *, creando: bool
    ) -> list[str]:
        avisos: list[str] = []

        if "v2_quotation_process_id" in data:
            tarea.v2_quotation_process_id = data["v2_quotation_process_id"]
        if "v2_quotation_product_id" in data:
            tarea.v2_quotation_product_id = data["v2_quotation_product_id"]
        if "is_additional_personnel" in data:
            tarea.is_additional_personnel = bool(data["is_additional_personnel"])
        if "quantity" in data:
            tarea.quantity = data["quantity"] if data["quantity"] is not None else ZERO

        trabajador_antes = None if creando else tarea.worker_id
        tecnica_antes = None if creando else tarea.technique_id
        avisos += await self._apply_worker(tarea, data, creando=creando)
        avisos += await self._apply_technique(tarea, data, creando=creando)
        avisos += await self._check_capacity(
            tarea,
            elegido_ahora=creando
            or tarea.worker_id != trabajador_antes
            or tarea.technique_id != tecnica_antes,
        )
        avisos += await self._apply_hours(tarea, data)
        avisos += await self._check_glaze(tarea)
        return avisos

    async def _check_process_duplicate(self, tarea: V2QuotationLabor) -> None:
        """Una tarea normal no puede repetir un proceso vivo de su pieza.

        La tarea que SALE de un proceso llega ya con su `v2_quotation_process_id`
        y no entra aqui. El personal adicional tampoco: traer a alguien de mas a
        hacer el mismo torno es legitimo y se cobra aparte, que es justo lo que
        el Excel llama «personal adicional».
        """
        if (
            tarea.v2_quotation_process_id is not None
            or tarea.is_additional_personnel
            or tarea.v2_quotation_product_id is None
        ):
            return
        proceso = (
            await self._session.scalars(
                select(V2QuotationProcess).where(
                    V2QuotationProcess.v2_quotation_product_id == tarea.v2_quotation_product_id,
                    V2QuotationProcess.technique_id == tarea.technique_id,
                    V2QuotationProcess.removed_at.is_(None),
                )
            )
        ).first()
        if proceso is not None:
            raise V2LaborAlreadyAProcessError()

    async def _check_capacity(self, tarea: V2QuotationLabor, *, elegido_ahora: bool) -> list[str]:
        """Que el trabajador tenga habilitada la tecnica. Se mira el PAR final.

        Mismo criterio que con el trabajador dado de baja: elegir HOY una
        combinacion no habilitada se rechaza; que la capacidad se retirara
        DESPUES de congelar la tarea solo avisa. Mirarlo campo a campo dejaria
        pasar un cambio de trabajador que conserva una tecnica que la persona
        nueva no sabe hacer.
        """
        if await self.is_enabled(tarea.worker_id, tarea.technique_id):
            return []
        if elegido_ahora:
            raise V2LaborTechniqueNotAllowedError()
        return [WARN_TECHNIQUE_NOT_ENABLED]

    async def _apply_worker(
        self, tarea: V2QuotationLabor, data: dict[str, Any], *, creando: bool
    ) -> list[str]:
        # «Viene en la peticion» NO es «lo acaba de elegir». Un cliente que
        # reenvia el formulario entero manda el mismo `worker_id` de siempre, y
        # tomarlo por una eleccion nueva tendria dos consecuencias caras:
        # encallaria el borrador si esa persona se dio de baja despues de
        # asignarla, y borraria la tarifa acordada como si se hubiera cambiado
        # de persona. Lo que importa es si CAMBIA.
        propuesto = data.get("worker_id")
        cambia = propuesto is not None and (creando or propuesto != tarea.worker_id)
        if propuesto is not None:
            tarea.worker_id = propuesto
        elif creando:
            raise V2LaborInputInvalid("Hay que indicar quien hace el trabajo")

        worker = await self.get_worker(tarea.worker_id)
        if not worker.active:
            # Elegir HOY a alguien dado de baja se rechaza; que se diera de baja
            # DESPUES de asignarlo solo avisa, y la tarea conserva lo congelado.
            # Bloquear ahi encallaria un borrador por una decision de otra
            # pantalla, y el costo ya calculado sigue siendo el que se acordo.
            if cambia:
                raise V2LaborResourceInactiveError(
                    f"«{worker.name}» esta dado de baja y no puede asignarse"
                )
            return [WARN_WORKER_UNAVAILABLE]

        jornada = await self.resolve_workday_hours(worker)
        tarifa_manual = self._explicit(data, "hourly_rate_override")

        tarea.worker_name_snapshot = worker.name
        tarea.worker_type_snapshot = worker.worker_type
        tarea.daily_rate_snapshot = worker.daily_rate
        tarea.workday_hours_snapshot = jornada

        if tarifa_manual is not None:
            # Una tarifa acordada para ESTA cotizacion. No toca el maestro: la
            # siguiente cotizacion vuelve a la tarifa de la casa.
            tarea.hourly_rate_snapshot = quantize_rate(tarifa_manual)
            tarea.rate_overridden = True
        elif "hourly_rate_override" in data:
            # Presente y en nulo: se retira el acuerdo y vuelve la tarifa real.
            tarea.hourly_rate_snapshot = await self.hourly_rate_for(worker)
            tarea.rate_overridden = False
        elif not tarea.rate_overridden or cambia:
            # Ausente y sin acuerdo previo —o con la persona CAMBIADA, porque un
            # acuerdo se tomo sobre alguien concreto y no se hereda—.
            tarea.hourly_rate_snapshot = await self.hourly_rate_for(worker)
            tarea.rate_overridden = False
        return []

    async def _apply_technique(
        self, tarea: V2QuotationLabor, data: dict[str, Any], *, creando: bool
    ) -> list[str]:
        # Mismo criterio que con el trabajador: reenviar la misma tecnica no es
        # elegirla de nuevo, y tratarlo asi encallaria un borrador en cuanto
        # alguien retirara del catalogo una tecnica ya asignada.
        propuesta = data.get("technique_id")
        cambia = propuesta is not None and (creando or propuesta != tarea.technique_id)
        if propuesta is not None:
            tarea.technique_id = propuesta
        elif creando:
            raise V2LaborInputInvalid("Hay que indicar que tecnica se aplica")

        tecnica = await self.get_technique(tarea.technique_id)
        if not tecnica.active:
            if cambia:
                raise V2LaborResourceInactiveError(
                    f"La tecnica «{tecnica.name}» esta retirada y no puede asignarse"
                )
            return [WARN_TECHNIQUE_UNAVAILABLE]

        tarea.technique_name_snapshot = tecnica.name
        tarea.technique_unit_snapshot = tecnica.unit
        tarea.standard_capacity_snapshot = tecnica.default_capacity_per_workday
        return []

    async def _apply_hours(self, tarea: V2QuotationLabor, data: dict[str, Any]) -> list[str]:
        """Calcula las horas del estandar y respeta las que se hayan acordado.

        El estandar dice cuanto deberia tardar; quien cotiza puede decir cuanto
        va a tardar de verdad en ESTE encargo. Lo segundo no cambia lo primero:
        un jarron dificil no baja el rendimiento de manana.
        """
        try:
            tarea.calculated_hours = quantize_hours(
                hours_required(
                    tarea.quantity,
                    tarea.standard_capacity_snapshot,
                    tarea.workday_hours_snapshot,
                )
            )
        except LaborMathError as error:
            raise V2LaborInputInvalid(str(error)) from error

        horas_manuales = self._explicit(data, "final_hours_override")
        if horas_manuales is not None:
            tarea.final_hours = quantize_hours(horas_manuales)
            tarea.hours_overridden = True
        elif "final_hours_override" in data:
            tarea.final_hours = tarea.calculated_hours
            tarea.hours_overridden = False
        elif not tarea.hours_overridden:
            tarea.final_hours = tarea.calculated_hours

        try:
            tarea.labor_cost = labor_cost(tarea.final_hours, tarea.hourly_rate_snapshot)
        except LaborMathError as error:
            raise V2LaborInputInvalid(str(error)) from error
        return []

    async def _check_glaze(self, tarea: V2QuotationLabor) -> list[str]:
        """Una tecnica de esmaltado sobre una pieza que no lleva esmalte.

        La regla es de 010C y no se duplica: el 15 %, el peso y el costo del
        esmalte los sigue calculando aquella fase. Aqui solo se avisa de una
        combinacion que casi siempre es un descuido, y se avisa en vez de
        bloquear porque el orden en que se llena un borrador es del usuario.
        """
        if tarea.v2_quotation_product_id is None:
            return []
        tecnica = await self._session.get(V2Technique, tarea.technique_id)
        if tecnica is None or not tecnica.requires_glaze:
            return []
        producto = await self._session.get(V2QuotationProduct, tarea.v2_quotation_product_id)
        if producto is not None and not producto.requires_glaze:
            return [WARN_GLAZE_TECHNIQUE_WITHOUT_GLAZE]
        return []

    async def _workday_warning(self, tarea: V2QuotationLabor) -> list[str]:
        """Avisa si lo que ahora acumula esa persona no cabe en su jornada.

        Se mira DESPUES de guardar y sobre el total de la cotizacion, no sobre
        la tarea sola: el caso que importa es el de tres tareas de tres horas
        que por separado no dicen nada y juntas no caben en el dia.

        Avisar es todo lo que se hace. Repartir en dos dias, alargar el dia o
        traer a alguien mas lo decide una persona, y hasta entonces las diez
        horas cuestan diez horas.
        """
        asignadas = await self._session.scalar(
            select(func.coalesce(func.sum(V2QuotationLabor.final_hours), 0)).where(
                V2QuotationLabor.v2_quotation_id == tarea.v2_quotation_id,
                V2QuotationLabor.worker_id == tarea.worker_id,
            )
        )
        if exceeds_workday(Decimal(asignadas or 0), tarea.workday_hours_snapshot):
            return [WARN_WORKDAY_EXCEEDED]
        return []

    # ------------------------------------------------------------------
    # Jornada compartida y avisos
    # ------------------------------------------------------------------
    async def workday_load(self, quotation_id: int) -> list[dict[str, Any]]:
        """Horas asignadas a cada persona en la cotizacion entera.

        Por cotizacion y no por producto: tres tareas de la misma persona en
        tres productos distintos son una jornada repartida, y mirarlas por
        separado haria creer que ninguna llega al limite.
        """
        filas = (
            await self._session.execute(
                select(
                    V2QuotationLabor.worker_id,
                    func.min(V2QuotationLabor.worker_name_snapshot).label("worker_name"),
                    func.min(V2QuotationLabor.workday_hours_snapshot).label("workday_hours"),
                    func.sum(V2QuotationLabor.final_hours).label("assigned_hours"),
                )
                .where(V2QuotationLabor.v2_quotation_id == quotation_id)
                .group_by(V2QuotationLabor.worker_id)
                .order_by(func.min(V2QuotationLabor.worker_name_snapshot))
            )
        ).all()

        carga: list[dict[str, Any]] = []
        for fila in filas:
            horas = Decimal(fila.assigned_hours or 0)
            jornada = Decimal(fila.workday_hours)
            carga.append(
                {
                    "worker_id": fila.worker_id,
                    "worker_name": fila.worker_name,
                    "workday_hours": jornada,
                    "assigned_hours": horas,
                    "exceeds_workday": exceeds_workday(horas, jornada),
                    # Cuantos dias harian falta si nadie alarga la jornada. Es
                    # una sugerencia para poder plantear la decision, no la
                    # decision: quien planifica puede elegir un dia largo.
                    "minimum_days": minimum_work_days(horas, jornada),
                }
            )
        return carga

    # ------------------------------------------------------------------
    # Ilustracion
    # ------------------------------------------------------------------
    async def set_illustration(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> V2Quotation:
        """Enciende o apaga la ilustracion y la congela con la tarifa de hoy.

        Es un concepto comercial aparte y no una tecnica mas: ilustrar no es
        tornear, y meterla en el catalogo de tecnicas le haria heredar reglas
        —rendimiento por trabajador, jornada compartida— que no son suyas.
        """
        quotation = await self._draft(quotation_id)

        if "illustration_quantity" in data and data["illustration_quantity"] is not None:
            quotation.illustration_quantity = data["illustration_quantity"]
        if "illustration_notes" in data:
            quotation.illustration_notes = data["illustration_notes"]
        if "illustration_enabled" in data:
            quotation.illustration_enabled = bool(data["illustration_enabled"])

        if not quotation.illustration_enabled:
            # Apagada es apagada. El CHECK de la tabla lo vuelve a exigir por si
            # alguien escribe por otra via.
            quotation.illustration_hours = ZERO
            quotation.illustration_cost = ZERO
            await self._session.flush()
            await refresh_pricing(self._session, quotation)
            # Apagarla tambien se audita. Sin esto, el rastro solo recogia
            # quien la encendio: retirar un concepto que cuesta dinero no
            # dejaba huella de quien lo hizo ni de cuando.
            self._audit.record_action(
                entity_type=V2_ILLUSTRATION_ENTITY,
                entity_id=str(quotation.id),
                action=AuditAction.UPDATE,
                user_id=user.id,
                user_display_name=user.display_name,
                metadata={"enabled": "False", "hours": "0", "cost": "0"},
            )
            return quotation

        ajustes = await self._settings()
        # Los snapshots se toman UNA vez, al encender. Si ya estaban, se
        # respetan: subir manana el jornal de ilustracion no puede reescribir
        # un precio que ya se entrego.
        if quotation.illustration_daily_rate_snapshot is None:
            quotation.illustration_daily_rate_snapshot = ajustes.illustration_daily_rate
            quotation.illustration_workday_hours_snapshot = ajustes.workday_hours
            quotation.illustration_capacity_snapshot = ajustes.illustration_pieces_per_workday
            try:
                quotation.illustration_hourly_rate_snapshot = quantize_rate(
                    hourly_rate(ajustes.illustration_daily_rate, ajustes.workday_hours)
                )
            except LaborMathError as error:
                raise V2LaborInputInvalid(str(error)) from error

        # Misma regla que en las tareas, y por el mismo motivo: ausente conserva
        # el acuerdo, presente y en nulo lo retira. Distinguirlos importa porque
        # confundirlos deja cobrando una tarifa que alguien creyo haber quitado.
        if "illustration_hourly_rate_override" in data:
            tarifa = data["illustration_hourly_rate_override"]
            if tarifa is not None:
                quotation.illustration_hourly_rate_snapshot = quantize_rate(tarifa)
            else:
                # Vuelve a la tarifa que ESTA cotizacion congelo, no a la de hoy:
                # retirar un acuerdo no es motivo para recotizar con otro jornal.
                try:
                    quotation.illustration_hourly_rate_snapshot = quantize_rate(
                        hourly_rate(
                            _required(quotation.illustration_daily_rate_snapshot),
                            _required(quotation.illustration_workday_hours_snapshot),
                        )
                    )
                except LaborMathError as error:
                    raise V2LaborInputInvalid(str(error)) from error

        try:
            quotation.illustration_hours = quantize_hours(
                hours_required(
                    quotation.illustration_quantity,
                    _required(quotation.illustration_capacity_snapshot),
                    _required(quotation.illustration_workday_hours_snapshot),
                )
            )
            quotation.illustration_cost = labor_cost(
                quotation.illustration_hours,
                _required(quotation.illustration_hourly_rate_snapshot),
            )
        except LaborMathError as error:
            raise V2LaborInputInvalid(str(error)) from error

        await self._session.flush()
        # Fase 010F. La ilustracion es un costo general de la cotizacion: al
        # encenderla o cambiarla se reparte entre los productos y mueve todos
        # los precios.
        await refresh_pricing(self._session, quotation)
        self._audit.record_action(
            entity_type=V2_ILLUSTRATION_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "quantity": str(quotation.illustration_quantity),
                "hours": str(quotation.illustration_hours),
                "cost": str(quotation.illustration_cost),
            },
        )
        return quotation

    async def set_planning(
        self, quotation_id: int, effective_work_days: int | None, *, user: AuthenticatedUser
    ) -> V2Quotation:
        """Guarda cuantos dias de taller se van a usar. Es una decision humana.

        Diez horas caben en un dia largo —y son un dia efectivo— o en dos dias
        —y son dos—. El sistema sugiere el minimo y no elige: 010F cobrara el
        espacio por lo que se decida aqui.
        """
        quotation = await self._draft(quotation_id)
        quotation.effective_work_days = effective_work_days
        await self._session.flush()
        # El espacio se cobra por dias efectivos: decidirlos pone precio a algo
        # que hasta ahora no lo tenia.
        await refresh_pricing(self._session, quotation)
        self._audit.record_action(
            entity_type=V2_LABOR_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"effective_work_days": str(effective_work_days)},
        )
        return quotation

    async def suggested_work_days(self, quotation_id: int) -> int:
        """Minimo de dias si nadie alarga su jornada. Una sugerencia, no un dato.

        Se toma el maximo por trabajador y no la suma de horas: las personas
        trabajan en paralelo, asi que sumar las horas de cinco personas y
        dividir por una jornada daria cinco veces los dias que hacen falta.
        """
        carga = await self.workday_load(quotation_id)
        if not carga:
            return 0
        return max(int(fila["minimum_days"]) for fila in carga)

    async def labor_total(self, quotation_id: int) -> Decimal:
        total = await self._session.scalar(
            select(func.coalesce(func.sum(V2QuotationLabor.labor_cost), 0)).where(
                V2QuotationLabor.v2_quotation_id == quotation_id
            )
        )
        return Decimal(total or 0)

    # ------------------------------------------------------------------
    # Interno
    # ------------------------------------------------------------------
    @staticmethod
    def _explicit(data: dict[str, Any], campo: str) -> Decimal | None:
        """El valor de un override SOLO cuando viene con valor.

        La distincion es el aprendizaje caro de 010C: en un PATCH parcial,
        `data.get(campo)` devuelve `None` tanto si el campo no vino como si vino
        en nulo. Lo primero significa «no lo toques» y lo segundo «quitalo», y
        confundirlos borra en silencio una tarifa acordada con el cliente al
        cambiar cualquier otra cosa.
        """
        valor = data.get(campo)
        return valor if valor is not None else None

    async def _settings(self) -> V2CommercialSettings:
        ajustes = await self._session.get(V2CommercialSettings, SINGLETON_ID)
        if ajustes is None:
            raise V2LaborInputInvalid(
                "No hay configuracion del Cotizador V2",
                code="V2_LABOR_SETTINGS_MISSING",
            )
        return ajustes

    async def quotation(self, quotation_id: int) -> V2Quotation:
        """La cotizacion, para LEER. Sin bloqueo y sin exigir que sea borrador.

        Consultar la ilustracion o los dias de una cotizacion ya emitida tiene
        que seguir funcionando: lo que no puede es cambiarla.
        """
        quotation = await self._session.get(V2Quotation, quotation_id)
        if quotation is None:
            raise V2LaborNotFoundError("La cotizacion V2 no existe")
        return quotation

    async def _draft(self, quotation_id: int) -> V2Quotation:
        """La cotizacion, bloqueada, si todavia admite cambios.

        Mismo criterio y mismo motivo que en 010C: leer el estado sin bloquear
        deja una ventana entre la comprobacion y el guardado por la que una
        emision simultanea colaria trabajo en una cotizacion ya comprometida.
        El bloqueo ademas serializa las escrituras de tareas de una misma
        cotizacion, que es lo que impide que dos altas a la vez repitan
        `sort_order`.
        """
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
        if quotation is None:
            raise V2LaborNotFoundError("La cotizacion V2 no existe")
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2LaborQuotationNotEditableError()
        return quotation

    async def _labor(self, quotation_id: int, labor_id: int) -> V2QuotationLabor:
        tarea = (
            await self._session.scalars(
                select(V2QuotationLabor).where(
                    V2QuotationLabor.id == labor_id,
                    # El id de la cotizacion NO sobra: sin el, conocer un id de
                    # tarea bastaria para editar la de otra cotizacion.
                    V2QuotationLabor.v2_quotation_id == quotation_id,
                )
            )
        ).one_or_none()
        if tarea is None:
            raise V2LaborNotFoundError()
        return tarea


def _required(valor: Decimal | None) -> Decimal:
    """Un snapshot que deberia existir. Si no existe, no se inventa un cero."""
    if valor is None:
        raise V2LaborInputInvalid(
            "Falta un dato congelado de ilustracion",
            code="V2_ILLUSTRATION_SNAPSHOT_MISSING",
        )
    return valor


__all__ = [
    "V2LaborInputInvalid",
    "V2LaborNotFoundError",
    "V2LaborQuotationNotEditableError",
    "V2LaborResourceInactiveError",
    "V2LaborService",
    "V2LaborTechniqueNotAllowedError",
    "V2LaborVersionConflictError",
    "V2TechniqueNotFoundError",
    "V2WorkerNotFoundError",
    "V2WorkerType",
]
