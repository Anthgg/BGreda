"""Procesos de la pieza y adicionales de la cotizacion (correccion 010H).

El orden del negocio, que hasta ahora estaba al reves:

    PIEZA -> PROCESOS QUE NECESITA -> PIEZAS AFECTADAS -> HORAS -> TRABAJADOR -> COSTO

Antes se elegia primero a la persona y despues se escribia todo a mano. Ahora la
pieza de catalogo declara sus procesos, la cotizacion nace con ellos y sus
piezas puestas, y el trabajador se asigna al final, que es cuando aparece el
costo.

## Lo que este servicio NO hace

No calcula ningun costo. Un proceso sin trabajador no cuesta nada: cuesta cuando
alguien lo hace, y entonces la tarea de 010D —intacta— congela jornal, jornada y
tarifa. Asignar delega en `V2LaborService.add_labor`, que ya sabe hacer eso y ya
rechaza a quien no tiene la tecnica habilitada.

## Las tres decisiones que recuerda

1. **quitado**: el proceso se marca, no se borra, o la siguiente regeneracion lo
   devolveria como si nadie hubiera decidido nada;
2. **anadido a mano**: nace con origen MANUAL y el maestro de la pieza no lo
   toca ni lo quita;
3. **piezas escritas a mano**: cambiar la cantidad del producto ya no las pisa.
   Si el logo va en 5 de las 20 tazas, sigue en 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.quoter_v2_labor import hours_required, quantize_hours
from app.models.audit import AuditAction
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct, V2QuotationStatus
from app.models.quoter_v2_labor import V2QuotationLabor, V2Technique
from app.models.quoter_v2_processes import (
    V2ProcessOrigin,
    V2ProductTechnique,
    V2QuotationProcess,
)
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2_labor import (
    V2LaborNotFoundError,
    V2LaborQuotationNotEditableError,
    V2LaborService,
)

ZERO = Decimal(0)

V2_PROCESS_ENTITY = "v2_quotation_process"
V2_PRODUCT_TECHNIQUE_ENTITY = "v2_product_technique"

#: La pieza no dice que procesos necesita. No es un error: una pieza a medida
#: elige los suyos aqui. Pero hay que decirlo, o la pantalla parece rota.
WARN_PRODUCT_WITHOUT_TECHNIQUES = "V2_PROCESS_PRODUCT_WITHOUT_TECHNIQUES"
#: Un proceso que el maestro pedia y cuya tecnica ya no esta activa. La fila
#: existente se queda —el borrador no encalla—, pero se avisa.
WARN_TECHNIQUE_INACTIVE = "V2_PROCESS_TECHNIQUE_INACTIVE"


class V2ProcessNotFoundError(APIError):
    status_code = 404
    code = "V2_PROCESS_NOT_FOUND"
    message = "El proceso no existe en esta cotizacion"


class V2ProcessInputInvalid(APIError):
    status_code = 422
    code = "V2_PROCESS_INPUT_INVALID"
    message = "Los datos del proceso no son validos"


class V2ProcessDuplicatedError(APIError):
    status_code = 409
    code = "V2_PROCESS_DUPLICATED"
    message = "Esa tecnica ya es un proceso de esta pieza"


@dataclass(frozen=True)
class ProcesoCalculado:
    """Un proceso con lo que se deduce de el, sin guardarlo."""

    proceso: V2QuotationProcess
    #: Horas al rendimiento estandar de la tecnica, con la jornada del taller.
    #: `None` cuando la tecnica decide sus horas a mano.
    calculated_hours: Decimal | None
    tarea: V2QuotationLabor | None
    warnings: list[str]


class V2ProcessService:
    """Procesos de una cotizacion y el maestro de procesos de cada pieza."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit
        self._labor = V2LaborService(session, audit)

    # ------------------------------------------------------------------
    # Maestro: que procesos necesita una pieza del catalogo
    # ------------------------------------------------------------------
    async def techniques_of_product(self, product_id: int) -> list[V2ProductTechnique]:
        consulta = (
            select(V2ProductTechnique)
            .where(V2ProductTechnique.product_id == product_id)
            .order_by(V2ProductTechnique.sort_order, V2ProductTechnique.id)
        )
        return list((await self._session.scalars(consulta)).all())

    async def set_product_techniques(
        self, product_id: int, technique_ids: list[int], *, user: Any
    ) -> list[V2ProductTechnique]:
        """Deja la pieza con EXACTAMENTE estos procesos, en este orden.

        Reemplaza el conjunto entero, como el maestro de tecnicas de un
        trabajador: la pantalla manda una lista de casillas, no un diff. Lo que
        desaparece se desactiva en vez de borrarse, porque hay cotizaciones que
        salieron de ahi y su historia no se tira.
        """
        if len(set(technique_ids)) != len(technique_ids):
            raise V2ProcessInputInvalid("Una tecnica no puede repetirse en la misma pieza")
        if technique_ids:
            existen = set(
                (
                    await self._session.scalars(
                        select(V2Technique.id).where(V2Technique.id.in_(technique_ids))
                    )
                ).all()
            )
            faltan = [str(uno) for uno in technique_ids if uno not in existen]
            if faltan:
                raise V2ProcessInputInvalid(
                    f"Estas tecnicas no existen: {', '.join(faltan)}",
                )

        actuales = {
            fila.technique_id: fila for fila in await self.techniques_of_product(product_id)
        }
        for orden, technique_id in enumerate(technique_ids):
            fila = actuales.get(technique_id)
            if fila is None:
                self._session.add(
                    V2ProductTechnique(
                        product_id=product_id,
                        technique_id=technique_id,
                        sort_order=orden,
                        active=True,
                    )
                )
            else:
                fila.active = True
                fila.sort_order = orden
        for technique_id, fila in actuales.items():
            if technique_id not in technique_ids:
                fila.active = False

        self._audit.record_action(
            entity_type=V2_PRODUCT_TECHNIQUE_ENTITY,
            entity_id=str(product_id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"techniques": ",".join(str(uno) for uno in technique_ids)},
        )
        await self._session.flush()
        return await self.techniques_of_product(product_id)

    # ------------------------------------------------------------------
    # Procesos de una cotizacion
    # ------------------------------------------------------------------
    async def list_processes(self, quotation_id: int) -> list[ProcesoCalculado]:
        """Los procesos vivos de la cotizacion, con sus horas y su tarea."""
        procesos = list(
            (
                await self._session.scalars(
                    select(V2QuotationProcess)
                    .where(
                        V2QuotationProcess.v2_quotation_id == quotation_id,
                        V2QuotationProcess.removed_at.is_(None),
                    )
                    .order_by(
                        V2QuotationProcess.v2_quotation_product_id,
                        V2QuotationProcess.sort_order,
                        V2QuotationProcess.id,
                    )
                )
            ).all()
        )
        if not procesos:
            return []

        # La tarea de cada proceso. Si hubiera mas de una atada al mismo
        # proceso, manda la que NO es personal adicional: la otra es gente de
        # mas, y se ve en su propia seccion.
        tareas: dict[int | None, V2QuotationLabor] = {}
        for tarea in (
            await self._session.scalars(
                select(V2QuotationLabor).where(
                    V2QuotationLabor.v2_quotation_process_id.in_(
                        [proceso.id for proceso in procesos]
                    )
                )
            )
        ).all():
            anterior = tareas.get(tarea.v2_quotation_process_id)
            if anterior is None or (
                anterior.is_additional_personnel and not tarea.is_additional_personnel
            ):
                tareas[tarea.v2_quotation_process_id] = tarea
        jornada = await self._labor.global_workday_hours()
        return [
            ProcesoCalculado(
                proceso=proceso,
                calculated_hours=self._horas(proceso, jornada),
                tarea=tareas.get(proceso.id),
                warnings=[] if proceso.technique.active else [WARN_TECHNIQUE_INACTIVE],
            )
            for proceso in procesos
        ]

    def _horas(self, proceso: V2QuotationProcess, jornada: Decimal) -> Decimal | None:
        """Horas estandar del proceso. `None` si la tecnica las decide a mano.

        No se guardan: son consecuencia de las piezas y del rendimiento, y
        guardarlas obligaria a recalcular una columna cada vez que cambiara
        cualquiera de los dos. Lo que se congela son las horas de la TAREA,
        cuando alguien la hace.
        """
        tecnica = proceso.technique
        if tecnica.manual_hours or tecnica.default_capacity_per_workday <= ZERO:
            return None
        return quantize_hours(
            hours_required(proceso.quantity, tecnica.default_capacity_per_workday, jornada)
        )

    async def generate_for_line(self, linea: V2QuotationProduct) -> list[str]:
        """Crea los procesos que la pieza de catalogo pide para esta linea.

        Se llama al anadir la linea y al cambiarle el producto. No resucita lo
        que alguien quito, no duplica lo que ya existe y no toca lo que se
        anadio a mano.
        """
        if linea.product_id is None:
            # Pieza a medida: no hay maestro del que copiar. Sus procesos se
            # eligen en la cotizacion, y eso es legitimo.
            return []

        requeridas = [
            fila for fila in await self.techniques_of_product(linea.product_id) if fila.active
        ]
        if not requeridas:
            return [WARN_PRODUCT_WITHOUT_TECHNIQUES]

        existentes = {
            proceso.technique_id: proceso
            for proceso in (
                await self._session.scalars(
                    select(V2QuotationProcess).where(
                        V2QuotationProcess.v2_quotation_product_id == linea.id
                    )
                )
            ).all()
        }
        activas = set(
            (
                await self._session.scalars(
                    select(V2Technique.id).where(
                        V2Technique.id.in_([fila.technique_id for fila in requeridas]),
                        V2Technique.active.is_(True),
                    )
                )
            ).all()
        )

        avisos: list[str] = []
        for fila in requeridas:
            if fila.technique_id in existentes:
                # Ya esta, o alguien la quito a proposito. Ninguna de las dos
                # cosas se toca.
                continue
            if fila.technique_id not in activas:
                # Una tecnica retirada no se propone: proponerla obligaria a
                # quitarla a mano en cada cotizacion nueva.
                avisos.append(WARN_TECHNIQUE_INACTIVE)
                continue
            self._session.add(
                V2QuotationProcess(
                    v2_quotation_id=linea.v2_quotation_id,
                    v2_quotation_product_id=linea.id,
                    technique_id=fila.technique_id,
                    sort_order=fila.sort_order,
                    origin=V2ProcessOrigin.PRODUCT,
                    quantity=Decimal(linea.quantity),
                    quantity_overridden=False,
                )
            )
        await self._session.flush()
        return avisos

    async def sync_quantity(self, linea: V2QuotationProduct, *, user: AuthenticatedUser) -> None:
        """La cantidad de la linea cambio: las piezas de sus PROCESOS la siguen.

        Salvo las que alguien escribio a mano. Y si el proceso ya tiene
        trabajador, su tarea se actualiza tambien: dejar la tarea con la
        cantidad vieja daria un costo que no es el de este pedido.

        El personal adicional NO se mueve, y es a proposito. Sus horas las
        decide una persona —«horas manuales; no reduce plazo automaticamente»,
        dice el Excel—: si alguien trajo a un refuerzo cuatro horas, subir el
        pedido de 20 a 50 piezas no convierte esas cuatro horas en diez por su
        cuenta. Quien planifica decide si hace falta mas apoyo.
        """
        procesos = list(
            (
                await self._session.scalars(
                    select(V2QuotationProcess).where(
                        V2QuotationProcess.v2_quotation_product_id == linea.id,
                        V2QuotationProcess.removed_at.is_(None),
                        V2QuotationProcess.quantity_overridden.is_(False),
                    )
                )
            ).all()
        )
        cantidad = Decimal(linea.quantity)
        afectados = [proceso for proceso in procesos if not proceso.technique.manual_hours]
        for proceso in afectados:
            proceso.quantity = cantidad
        await self._session.flush()

        # Las tareas de todos ellos en UNA consulta, y un solo recalculo del
        # precio al final: `update_line` ya lo hace por su cuenta.
        #
        # Una LISTA y no un diccionario por proceso: si alguna vez dos tareas
        # apuntaran al mismo proceso, el diccionario se quedaria con una y la
        # otra seguiria cobrando la cantidad vieja (hallazgo de Gemini).
        tareas = list(
            (
                await self._session.scalars(
                    select(V2QuotationLabor).where(
                        V2QuotationLabor.v2_quotation_process_id.in_(
                            [proceso.id for proceso in afectados]
                        )
                    )
                )
            ).all()
        )
        for tarea in tareas:
            if not tarea.hours_overridden:
                # Firma quien cambio la cantidad del producto: la tarea se
                # mueve por su decision, aunque no la escribiera a mano.
                await self._labor.update_labor(
                    linea.v2_quotation_id,
                    tarea.id,
                    {"quantity": cantidad},
                    user=user,
                    recalcular=False,
                )

    async def reset_product_processes(self, linea: V2QuotationProduct) -> list[str]:
        """La linea cambio de pieza: sus procesos son los de la pieza NUEVA.

        Los que trajo la pieza anterior —incluidas las lapidas de lo que alguien
        quito para ella— dejan de tener sentido: eran de otra cosa. Se van con
        sus tareas. Lo que se anadio a mano en esta cotizacion se queda: es una
        decision de este pedido, no de la pieza.
        """
        viejos = list(
            (
                await self._session.scalars(
                    select(V2QuotationProcess).where(
                        V2QuotationProcess.v2_quotation_product_id == linea.id,
                        V2QuotationProcess.origin == V2ProcessOrigin.PRODUCT,
                    )
                )
            ).all()
        )
        if viejos:
            tareas = (
                await self._session.scalars(
                    select(V2QuotationLabor).where(
                        V2QuotationLabor.v2_quotation_process_id.in_(
                            [proceso.id for proceso in viejos]
                        )
                    )
                )
            ).all()
            for tarea in tareas:
                await self._session.delete(tarea)
            for proceso in viejos:
                await self._session.delete(proceso)
            await self._session.flush()
        return await self.generate_for_line(linea)

    async def add_process(
        self, quotation_id: int, data: dict[str, Any], *, user: Any
    ) -> tuple[V2QuotationProcess, list[str]]:
        """Un proceso mas, solo en esta cotizacion."""
        await self._draft(quotation_id)
        linea = await self._linea(quotation_id, int(data["v2_quotation_product_id"]))
        technique_id = int(data["technique_id"])
        tecnica = await self._session.get(V2Technique, technique_id)
        if tecnica is None:
            raise V2ProcessInputInvalid("La tecnica no existe")
        if not tecnica.active:
            raise V2ProcessInputInvalid("La tecnica esta retirada del catalogo")

        existente = (
            await self._session.scalars(
                select(V2QuotationProcess).where(
                    V2QuotationProcess.v2_quotation_product_id == linea.id,
                    V2QuotationProcess.technique_id == technique_id,
                )
            )
        ).one_or_none()
        if existente is not None and existente.removed_at is None:
            raise V2ProcessDuplicatedError()

        cantidad = data.get("quantity")
        piezas = (
            Decimal(str(cantidad))
            if cantidad is not None
            else (ZERO if tecnica.manual_hours else Decimal(linea.quantity))
        )
        if piezas < ZERO:
            raise V2ProcessInputInvalid("Las piezas no pueden ser negativas")

        if existente is not None:
            # Estaba quitado y vuelve: la misma fila, para no perder su historia
            # ni chocar con la unicidad del par.
            existente.removed_at = None
            existente.quantity = piezas
            existente.quantity_overridden = cantidad is not None
            proceso = existente
        else:
            siguiente = await self._session.scalar(
                select(func.coalesce(func.max(V2QuotationProcess.sort_order), -1) + 1).where(
                    V2QuotationProcess.v2_quotation_product_id == linea.id
                )
            )
            proceso = V2QuotationProcess(
                v2_quotation_id=quotation_id,
                v2_quotation_product_id=linea.id,
                technique_id=technique_id,
                sort_order=int(siguiente or 0),
                origin=V2ProcessOrigin.MANUAL,
                quantity=piezas,
                quantity_overridden=cantidad is not None,
            )
            self._session.add(proceso)

        self._audit.record_action(
            entity_type=V2_PROCESS_ENTITY,
            entity_id=str(proceso.id or 0),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id), "technique": tecnica.name},
        )
        await self._session.flush()
        return proceso, []

    async def set_quantity(
        self, quotation_id: int, process_id: int, quantity: Decimal, *, user: Any
    ) -> V2QuotationProcess:
        """Este proceso afecta a otras piezas que el resto de la linea."""
        await self._draft(quotation_id)
        proceso = await self._proceso(quotation_id, process_id)
        if quantity < ZERO:
            raise V2ProcessInputInvalid("Las piezas no pueden ser negativas")
        proceso.quantity = quantity
        proceso.quantity_overridden = True
        await self._session.flush()

        tarea = await self._tarea_de(proceso)
        if tarea is not None and not tarea.hours_overridden:
            await self._labor.update_labor(
                quotation_id, tarea.id, {"quantity": quantity}, user=user
            )
        return proceso

    async def remove_process(self, quotation_id: int, process_id: int, *, user: Any) -> None:
        """Quitar el proceso de ESTA cotizacion. El maestro no se entera."""
        quotation = await self._draft(quotation_id)
        proceso = await self._proceso(quotation_id, process_id)
        tarea = await self._tarea_de(proceso)
        if tarea is not None:
            await self._labor.delete_labor(quotation.id, tarea.id, user=user)
        proceso.removed_at = datetime.now(UTC)
        self._audit.record_action(
            entity_type=V2_PROCESS_ENTITY,
            entity_id=str(process_id),
            action=AuditAction.DELETE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        await self._session.flush()

    async def assign_worker(
        self, quotation_id: int, process_id: int, worker_id: int, *, user: Any
    ) -> tuple[V2QuotationLabor, list[str]]:
        """Poner a alguien a hacer este proceso. Aqui aparece el costo.

        Delega en la mano de obra de 010D, que es quien congela jornal, jornada
        y tarifa, y quien rechaza a quien no tiene la tecnica habilitada.
        """
        await self._draft(quotation_id)
        proceso = await self._proceso(quotation_id, process_id)
        tarea = await self._tarea_de(proceso)
        if tarea is not None:
            tarea, avisos = await self._labor.update_labor(
                quotation_id, tarea.id, {"worker_id": worker_id}, user=user
            )
            return tarea, avisos

        # El proceso viaja en el alta: asi la tarea nace ya atada a el y la
        # guardia contra duplicados sabe que esta no es una tarea suelta.
        tarea, avisos = await self._labor.add_labor(
            quotation_id,
            {
                "v2_quotation_process_id": proceso.id,
                "v2_quotation_product_id": proceso.v2_quotation_product_id,
                "worker_id": worker_id,
                "technique_id": proceso.technique_id,
                "quantity": proceso.quantity,
            },
            user=user,
        )
        return tarea, avisos

    async def unassign_worker(self, quotation_id: int, process_id: int, *, user: Any) -> None:
        """Quitar al trabajador sin quitar el proceso: la pieza sigue pidiendolo."""
        await self._draft(quotation_id)
        proceso = await self._proceso(quotation_id, process_id)
        tarea = await self._tarea_de(proceso)
        if tarea is None:
            return
        await self._labor.delete_labor(quotation_id, tarea.id, user=user)

    # ------------------------------------------------------------------
    # Lecturas internas
    # ------------------------------------------------------------------
    async def _tarea_de(self, proceso: V2QuotationProcess) -> V2QuotationLabor | None:
        return (
            await self._session.scalars(
                select(V2QuotationLabor).where(
                    V2QuotationLabor.v2_quotation_process_id == proceso.id
                )
            )
        ).first()

    async def _proceso(self, quotation_id: int, process_id: int) -> V2QuotationProcess:
        proceso = (
            await self._session.scalars(
                select(V2QuotationProcess).where(
                    V2QuotationProcess.id == process_id,
                    # El id de la cotizacion no sobra: sin el, conocer un id de
                    # proceso bastaria para tocar el de otra cotizacion.
                    V2QuotationProcess.v2_quotation_id == quotation_id,
                )
            )
        ).one_or_none()
        if proceso is None or proceso.removed_at is not None:
            raise V2ProcessNotFoundError()
        return proceso

    async def _linea(self, quotation_id: int, linea_id: int) -> V2QuotationProduct:
        linea = (
            await self._session.scalars(
                select(V2QuotationProduct).where(
                    V2QuotationProduct.id == linea_id,
                    V2QuotationProduct.v2_quotation_id == quotation_id,
                )
            )
        ).one_or_none()
        if linea is None:
            raise V2ProcessInputInvalid("Esa linea no es de esta cotizacion")
        return linea

    async def _draft(self, quotation_id: int) -> V2Quotation:
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
