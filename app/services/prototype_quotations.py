"""Reglas de la cotizacion de prototipo.

El documento vive en tres tiempos y cada uno tiene una autoridad distinta:

- **Borrador.** Lo que falta se lee de la configuracion VIGENTE en cada
  calculo. Por eso los `*_override` son nulos hasta que alguien pacta un
  precio: copiar el valor por defecto dentro del borrador impediria distinguir
  «lo acordamos asi» de «lo hereda», y al cambiar la tarifa de la casa el
  borrador dejaria de seguirla sin que nadie se enterara.
- **Confirmada.** Se congela todo lo que hizo falta para llegar al numero:
  tarifas efectivas, costos de material, tarifa de horno, IGV, moneda y el
  paso de redondeo con su origen. A partir de ahi el documento no cambia
  aunque cambie el mundo.
- **Pagada.** Habilita la produccion y NO gasta un gramo. El material se
  consume al arrancar la muestra, que es cuando de verdad sale del almacen.

El precio nunca llega del navegador. El browser manda dias, cantidades y que
material se usa; los importes los resuelve el backend leyendo maestros, y por
eso `confirm` recalcula entero en vez de creerse lo que vio la pantalla.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import APIError
from app.core.prototype_pricing import (
    PrototypeCosting,
    PrototypeCostingInput,
    PrototypeMaterialInput,
    PrototypePricingError,
    price_prototype,
)
from app.models.audit import AuditAction
from app.models.firings import FiringType, Kiln, KilnRate
from app.models.masters import Partner, Product
from app.models.prototype_quotations import (
    PrototypeQuotation,
    PrototypeQuotationMaterial,
    PrototypeQuotationPaymentStatus,
    PrototypeQuotationStatus,
)
from app.models.prototypes import (
    Prototype,
    PrototypeApproval,
    PrototypeMaterialLine,
    PrototypeStatus,
)
from app.models.sequence import SequenceType
from app.models.settings import CommercialSettings
from app.schemas.auth import AuthenticatedUser
from app.schemas.prototype_quotations import (
    PrototypeCostBreakdownOut,
    PrototypeQuotationMaterialOut,
    PrototypeQuotationOut,
)
from app.services.audit import AuditRecorder
from app.services.sequences import SequenceService

ZERO = Decimal(0)
ENTITY = "prototype_quotation"

#: De donde salio el paso de redondeo con el que se cerro el documento. Se
#: guarda junto al valor porque «0.50» sin su origen no explica nada dentro de
#: dos anos, cuando la configuracion diga otra cosa.
ROUNDING_SOURCE_SETTINGS = "COMMERCIAL_SETTINGS"


class PrototypeQuotationNotFoundError(APIError):
    status_code = 404
    code = "PROTOTYPE_QUOTATION_NOT_FOUND"
    message = "La cotizacion de prototipo no existe"


class PrototypeQuotationNotEditableError(APIError):
    """Una cotizacion emitida es un documento entregado, no un formulario."""

    status_code = 409
    code = "PROTOTYPE_QUOTATION_NOT_EDITABLE"
    message = "Solo se puede editar una cotizacion de prototipo en borrador"


class PrototypeQuotationNotConfirmableError(APIError):
    status_code = 409
    code = "PROTOTYPE_QUOTATION_NOT_CONFIRMABLE"
    message = "Solo se puede emitir una cotizacion de prototipo en borrador"


class PrototypeQuotationNotPayableError(APIError):
    status_code = 409
    code = "PROTOTYPE_QUOTATION_NOT_PAYABLE"
    message = "Solo se puede cobrar una cotizacion de prototipo ya emitida"


class PrototypeQuotationNotCancellableError(APIError):
    """Una pagada no se anula aqui.

    Deshacer un cobro necesita devolucion o nota de credito, y ninguna de las
    dos existe todavia en el sistema. Inventarlas aqui dejaria dinero cobrado
    sin documento que lo explique.
    """

    status_code = 409
    code = "PROTOTYPE_QUOTATION_NOT_CANCELLABLE"
    message = "Una cotizacion de prototipo pagada no se puede anular"


class PrototypeQuotationIncompleteError(APIError):
    """Falta algo sin lo cual el precio seria una suposicion."""

    status_code = 422
    code = "PROTOTYPE_QUOTATION_INCOMPLETE"
    message = "Faltan datos para emitir la cotizacion de prototipo"


class PrototypeQuotationFiringRateMissingError(APIError):
    """Hay hornadas pero el horno no tiene tarifa vigente para ese tipo.

    Cotizar con cero seria regalar la quema; elegir otra tarifa, inventarla.
    """

    status_code = 422
    code = "PROTOTYPE_QUOTATION_FIRING_RATE_MISSING"
    message = "El horno no tiene tarifa vigente para el tipo de quema elegido"


class PrototypeQuotationMaterialCostMissingError(APIError):
    status_code = 422
    code = "PROTOTYPE_QUOTATION_MATERIAL_COST_MISSING"
    message = "Un material del prototipo no tiene costo en el catalogo"


class PrototypeQuotationStaleError(APIError):
    status_code = 409
    code = "PROTOTYPE_QUOTATION_CONFLICT"
    message = "La cotizacion cambio en otra sesion; vuelva a cargarla"


class PrototypeQuotationService:
    """Cotizar, emitir y cobrar el desarrollo de una muestra."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRecorder,
        sequences: SequenceService,
    ) -> None:
        self._session = session
        self._audit = audit
        self._sequences = sequences

    # -- Lectura -----------------------------------------------------------
    def _base_query(self) -> Select[tuple[PrototypeQuotation]]:
        return select(PrototypeQuotation).options(
            selectinload(PrototypeQuotation.lines).selectinload(PrototypeQuotationMaterial.product)
        )

    async def get(self, quotation_id: int, *, for_update: bool = False) -> PrototypeQuotation:
        statement = self._base_query().where(PrototypeQuotation.id == quotation_id)
        if for_update:
            statement = statement.with_for_update(of=PrototypeQuotation)
        row = await self._session.scalar(statement)
        if row is None:
            raise PrototypeQuotationNotFoundError()
        return row

    async def list(
        self,
        *,
        status: PrototypeQuotationStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PrototypeQuotation], int]:
        statement = self._base_query()
        conteo = select(func.count()).select_from(PrototypeQuotation)
        if status is not None:
            statement = statement.where(PrototypeQuotation.status == status)
            conteo = conteo.where(PrototypeQuotation.status == status)
        statement = statement.order_by(PrototypeQuotation.id.desc()).limit(limit).offset(offset)
        filas = list((await self._session.scalars(statement)).all())
        total = int(await self._session.scalar(conteo) or 0)
        return filas, total

    # -- Resolucion de la politica vigente ---------------------------------
    async def _settings(self) -> CommercialSettings:
        fila = await self._session.scalar(select(CommercialSettings).limit(1))
        if fila is None:
            raise PrototypeQuotationIncompleteError(
                details=[{"code": "COMMERCIAL_SETTINGS_MISSING"}]
            )
        return fila

    async def _firing_rate(
        self, kiln_id: int | None, firing_type: FiringType | None, *, momento: date
    ) -> tuple[Decimal, int]:
        """Tarifa vigente y dias por hornada del horno elegido.

        Ambos salen del maestro. Los «3 dias el chico y 4 el grande» de la
        reunion ya viven en `Kiln.firing_days_per_batch`: repetirlos aqui como
        constante seria una segunda verdad que se desactualiza sola.
        """
        if kiln_id is None or firing_type is None:
            return ZERO, 0
        horno = await self._session.get(Kiln, kiln_id)
        if horno is None:
            raise PrototypeQuotationIncompleteError(details=[{"code": "KILN_NOT_FOUND"}])
        tarifa = await self._session.scalar(
            select(KilnRate.rate)
            .where(
                KilnRate.kiln_id == kiln_id,
                KilnRate.firing_type == firing_type,
                KilnRate.valid_from <= momento,
                or_(KilnRate.valid_to.is_(None), KilnRate.valid_to > momento),
            )
            .order_by(KilnRate.valid_from.desc(), KilnRate.id.desc())
            .limit(1)
        )
        if tarifa is None:
            raise PrototypeQuotationFiringRateMissingError()
        return tarifa, horno.firing_days_per_batch

    async def _material_inputs(
        self, fila: PrototypeQuotation, *, congelado: bool
    ) -> tuple[PrototypeMaterialInput, ...]:
        """Los materiales con su costo unitario.

        Mientras el documento es borrador el costo se lee del catalogo en cada
        calculo, para que refleje el precio de hoy. Una vez emitido se usa el
        que se congelo: regenerar el PDF de un documento historico con el costo
        actual daria otro total que el firmado.
        """
        entradas: list[PrototypeMaterialInput] = []
        for linea in fila.lines:
            if congelado and linea.unit_cost_snapshot is not None:
                costo = linea.unit_cost_snapshot
                nombre = linea.product_name_snapshot or ""
            else:
                producto = linea.product or await self._session.get(Product, linea.product_id)
                if producto is None or producto.cost is None:
                    raise PrototypeQuotationMaterialCostMissingError(
                        details=[{"product_id": linea.product_id}]
                    )
                costo = producto.cost
                nombre = producto.name
            entradas.append(
                PrototypeMaterialInput(
                    product_id=linea.product_id,
                    description=nombre,
                    quantity_per_prototype=linea.quantity_per_prototype,
                    uom_code=linea.uom_code,
                    unit_cost=costo,
                )
            )
        return tuple(entradas)

    async def _costing_input(
        self, fila: PrototypeQuotation, *, congelado: bool
    ) -> PrototypeCostingInput:
        ajustes = await self._settings()
        momento = (fila.created_at or datetime.now(UTC)).date()

        if congelado and fila.cost_snapshot:
            congelados = fila.cost_snapshot.get("effective", {})
            design_rate = Decimal(str(congelados["design_rate"]))
            artist_rate = Decimal(str(congelados["artist_rate"]))
            mold_price = Decimal(str(congelados["mold_maker_price"]))
            fixed_cost = Decimal(str(congelados["fixed_cost"]))
            tax_percent = Decimal(str(congelados["tax_percent"]))
            rounding_step = Decimal(str(congelados["rounding_step"]))
            firing_rate = Decimal(str(congelados["firing_rate"]))
            firing_days_per_batch = int(congelados["firing_days_per_batch"])
        else:
            # Nulo significa «usa lo de la casa». Ver el docstring del modulo.
            design_rate = (
                fila.design_rate_override
                if fila.design_rate_override is not None
                else ajustes.prototype_design_rate
            )
            artist_rate = (
                fila.artist_rate_override
                if fila.artist_rate_override is not None
                else ajustes.prototype_artist_rate
            )
            mold_price = (
                fila.mold_maker_price_override
                if fila.mold_maker_price_override is not None
                else ajustes.prototype_mold_maker_price
            )
            fixed_cost = (
                fila.fixed_cost_override
                if fila.fixed_cost_override is not None
                else ajustes.prototype_fixed_cost
            )
            tax_percent = ajustes.tax_percent if ajustes.tax_percent is not None else ZERO
            rounding_step = ajustes.rounding_step
            firing_rate, firing_days_per_batch = await self._firing_rate(
                fila.kiln_id, fila.firing_type, momento=momento
            )

        return PrototypeCostingInput(
            quantity=fila.quantity,
            design_days=fila.design_days,
            design_rate=design_rate,
            artist_days=fila.artist_days,
            artist_rate=artist_rate,
            mold_maker_price=mold_price,
            mold_maker_days=fila.mold_maker_days,
            materials=await self._material_inputs(fila, congelado=congelado),
            firing_rate=firing_rate,
            firing_batches=fila.firing_batches,
            firing_days_per_batch=firing_days_per_batch,
            drying_days=fila.drying_days,
            adjustment_days=fila.adjustment_days,
            fixed_cost=fixed_cost,
            tax_percent=tax_percent,
            rounding_step=rounding_step,
            requested_at=momento,
        )

    async def cost(self, fila: PrototypeQuotation) -> PrototypeCosting:
        """El costeo vigente del documento.

        Una confirmada se valora con lo que congelo; un borrador, con lo que la
        casa cobra hoy. Es el mismo metodo a proposito: si fueran dos, algun dia
        uno cambiaria y el otro no.
        """
        congelado = fila.status is PrototypeQuotationStatus.CONFIRMED
        try:
            return price_prototype(await self._costing_input(fila, congelado=congelado))
        except PrototypePricingError as error:
            raise PrototypeQuotationIncompleteError(details=[{"reason": str(error)}]) from error

    # -- Presentacion -------------------------------------------------------
    async def present(self, fila: PrototypeQuotation) -> PrototypeQuotationOut:
        """Arma la respuesta con el costeo ya resuelto.

        El desglose viaja porque la pantalla de quien cotiza lo necesita para
        explicar el precio. Es interno: el PDF del cliente lleva el importe, no
        la tarifa del artista.
        """
        ajustes = await self._settings()
        congelado = fila.status is PrototypeQuotationStatus.CONFIRMED
        entrada = await self._costing_input(fila, congelado=congelado)
        costeo = price_prototype(entrada)

        muestra = await self._session.scalar(
            select(Prototype).where(Prototype.prototype_quotation_id == fila.id).limit(1)
        )
        por_producto = {linea.product_id: linea for linea in fila.lines}

        return PrototypeQuotationOut(
            id=fila.id,
            code=fila.code,
            status=fila.status,
            payment_status=fila.payment_status,
            paid_at=fila.paid_at,
            confirmed_at=fila.confirmed_at,
            cancelled_at=fila.cancelled_at,
            customer_id=fila.customer_id,
            customer_name=fila.customer_name_snapshot,
            product_id=fila.product_id,
            description=fila.description,
            quantity=fila.quantity,
            width_cm=fila.width_cm,
            length_cm=fila.length_cm,
            height_cm=fila.height_cm,
            depth_cm=fila.depth_cm,
            technical_specifications=fila.technical_specifications,
            notes=fila.notes,
            design_days=fila.design_days,
            design_rate_override=fila.design_rate_override,
            artist_days=fila.artist_days,
            artist_rate_override=fila.artist_rate_override,
            mold_maker_partner_id=fila.mold_maker_partner_id,
            mold_maker_price_override=fila.mold_maker_price_override,
            mold_maker_days=fila.mold_maker_days,
            kiln_id=fila.kiln_id,
            firing_type=fila.firing_type,
            firing_batches=fila.firing_batches,
            drying_days=fila.drying_days,
            adjustment_days=fila.adjustment_days,
            fixed_cost_override=fila.fixed_cost_override,
            # Una confirmada muestra la moneda con la que se emitio; un borrador,
            # la vigente. El documento firmado no cambia de moneda.
            currency_code=(fila.currency_code_snapshot if congelado else ajustes.currency_code),
            currency_symbol=(
                fila.currency_symbol_snapshot if congelado else ajustes.currency_symbol
            ),
            exchange_rate=fila.exchange_rate_snapshot,
            costing=PrototypeCostBreakdownOut(
                design_cost=costeo.design_cost,
                artist_cost=costeo.artist_cost,
                mold_maker_cost=costeo.mold_maker_cost,
                materials_cost=costeo.materials_cost,
                firing_cost=costeo.firing_cost,
                fixed_cost=costeo.fixed_cost,
                base_cost=costeo.base_cost,
                raw_tax=costeo.raw_tax,
                raw_gross_total=costeo.raw_gross_total,
                commercial_net_total=costeo.commercial_net_total,
                tax_percent=costeo.tax_percent,
                commercial_tax_total=costeo.commercial_tax_total,
                commercial_gross_total=costeo.commercial_gross_total,
                total_per_prototype=costeo.total_per_prototype,
                rounding_step=costeo.rounding_step,
                rounding_source=(
                    fila.rounding_source_snapshot if congelado else ROUNDING_SOURCE_SETTINGS
                ),
                design_rate=entrada.design_rate,
                artist_rate=entrada.artist_rate,
                mold_maker_price=entrada.mold_maker_price,
                firing_rate=entrada.firing_rate,
                firing_days_per_batch=entrada.firing_days_per_batch,
                design_days=costeo.design_days,
                artist_days=costeo.artist_days,
                mold_maker_days=costeo.mold_maker_days,
                drying_days=costeo.drying_days,
                firing_days=costeo.firing_days,
                adjustment_days=costeo.adjustment_days,
                estimated_days=costeo.estimated_days,
                target_date=costeo.target_date,
                materials=[
                    PrototypeQuotationMaterialOut(
                        id=por_producto[material.product_id].id
                        if material.product_id in por_producto
                        else None,
                        product_id=material.product_id,
                        product_name=material.description,
                        quantity_per_prototype=material.quantity_per_prototype,
                        total_quantity=material.total_quantity,
                        uom_code=material.uom_code,
                        unit_cost=material.unit_cost,
                        cost=material.cost,
                        is_body_material=(
                            por_producto[material.product_id].is_body_material
                            if material.product_id in por_producto
                            else False
                        ),
                    )
                    for material in costeo.materials
                ],
            ),
            prototype_id=muestra.id if muestra else None,
            prototype_code=muestra.code if muestra else None,
            updated_at=fila.updated_at,
        )

    # -- Mutaciones ---------------------------------------------------------
    async def preview(
        self, datos: dict[str, Any], *, user: AuthenticatedUser
    ) -> PrototypeQuotationOut:
        """Que costaria y cuanto tardaria, sin guardar nada.

        Se monta una fila en memoria y se valora con la politica VIGENTE. Quien
        llama deshace la transaccion: mirar un precio no puede gastar un
        correlativo ni dejar borradores sueltos cada vez que alguien mueve un
        dia arriba o abajo.
        """
        fila = PrototypeQuotation(
            status=PrototypeQuotationStatus.DRAFT,
            payment_status=PrototypeQuotationPaymentStatus.UNPAID,
            created_by=user.id,
            created_by_name=user.display_name,
            lines=[],
        )
        await self._aplicar(fila, dict(datos))
        # La fila necesita `created_at` para resolver la tarifa vigente del
        # horno, y todavia no ha pasado por la base.
        fila.created_at = datetime.now(UTC)
        fila.id = 0
        return await self.present(fila)

    async def create_draft(
        self, datos: dict[str, Any], *, user: AuthenticatedUser
    ) -> PrototypeQuotation:
        """Abre el borrador. No emite numero: eso ocurre al confirmar."""
        fila = PrototypeQuotation(
            status=PrototypeQuotationStatus.DRAFT,
            payment_status=PrototypeQuotationPaymentStatus.UNPAID,
            created_by=user.id,
            created_by_name=user.display_name,
            # Inicializada aqui, antes del flush: leer una coleccion sin cargar
            # en una fila recien creada deja de ser un acceso en memoria y pasa
            # a ser una consulta, que en sesion asincrona revienta.
            lines=[],
        )
        await self._aplicar(fila, datos)
        self._session.add(fila)
        await self._session.flush()
        self._audit.record_action(
            entity_type=ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"description": fila.description, "quantity": fila.quantity},
        )
        return fila

    async def update_draft(
        self,
        quotation_id: int,
        datos: dict[str, Any],
        *,
        expected_updated_at: datetime | None,
        user: AuthenticatedUser,
    ) -> PrototypeQuotation:
        fila = await self.get(quotation_id, for_update=True)
        if fila.status is not PrototypeQuotationStatus.DRAFT:
            raise PrototypeQuotationNotEditableError()
        if expected_updated_at is not None and fila.updated_at != expected_updated_at:
            # Ultimo-en-escribir-gana perderia el trabajo del otro sin avisar.
            raise PrototypeQuotationStaleError()
        await self._aplicar(fila, datos)
        fila.updated_at = datetime.now(UTC)
        await self._session.flush()
        self._audit.record_action(
            entity_type=ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"description": fila.description},
        )
        return fila

    async def _aplicar(self, fila: PrototypeQuotation, datos: dict[str, Any]) -> None:
        """Escribe las ENTRADAS. Ningun importe: esos los calcula el backend."""
        materiales = datos.pop("materials", None)
        for campo, valor in datos.items():
            setattr(fila, campo, valor)

        if fila.customer_id is not None:
            cliente = await self._session.get(Partner, fila.customer_id)
            if cliente is None:
                raise PrototypeQuotationIncompleteError(details=[{"code": "CUSTOMER_NOT_FOUND"}])
            fila.customer_name_snapshot = cliente.name

        if materiales is None:
            return
        # La lista se reemplaza entera, como en los materiales de la muestra: es
        # un formulario, y conservar a medias daria una lista que nadie escribio.
        fila.lines.clear()
        for orden, material in enumerate(materiales):
            producto = await self._session.get(Product, material["product_id"])
            if producto is None:
                raise PrototypeQuotationIncompleteError(
                    details=[{"code": "PRODUCT_NOT_FOUND", "product_id": material["product_id"]}]
                )
            fila.lines.append(
                PrototypeQuotationMaterial(
                    product_id=producto.id,
                    sort_order=orden,
                    quantity_per_prototype=material["quantity_per_prototype"],
                    # La unidad la manda el catalogo, no el navegador: dejarla
                    # elegir permitiria cotizar kilos de algo que se lleva en
                    # gramos y multiplicar el costo por mil.
                    uom_code=producto.base_uom_code,
                    is_body_material=bool(material.get("is_body_material", False)),
                )
            )

    async def confirm(self, quotation_id: int, *, user: AuthenticatedUser) -> PrototypeQuotation:
        """Emite el documento y congela todo lo que hizo falta para el numero."""
        fila = await self.get(quotation_id, for_update=True)
        if fila.status is not PrototypeQuotationStatus.DRAFT:
            raise PrototypeQuotationNotConfirmableError()
        if fila.customer_id is None:
            raise PrototypeQuotationIncompleteError(details=[{"code": "CUSTOMER_REQUIRED"}])
        if not fila.description.strip():
            raise PrototypeQuotationIncompleteError(details=[{"code": "DESCRIPTION_REQUIRED"}])

        ajustes = await self._settings()
        momento = (fila.created_at or datetime.now(UTC)).date()
        entrada = await self._costing_input(fila, congelado=False)
        try:
            costeo = price_prototype(entrada)
        except PrototypePricingError as error:
            raise PrototypeQuotationIncompleteError(details=[{"reason": str(error)}]) from error

        # El costo de cada material se congela EN SU LINEA: el catalogo puede
        # cambiar manana y el documento firmado no.
        por_producto = {material.product_id: material for material in entrada.materials}
        for linea in fila.lines:
            material = por_producto[linea.product_id]
            linea.unit_cost_snapshot = material.unit_cost
            linea.product_name_snapshot = material.description

        fila.currency_code_snapshot = ajustes.currency_code
        fila.currency_symbol_snapshot = ajustes.currency_symbol
        fila.tax_percent_snapshot = entrada.tax_percent
        fila.rounding_step_snapshot = entrada.rounding_step
        fila.rounding_source_snapshot = ROUNDING_SOURCE_SETTINGS
        fila.cost_snapshot = _snapshot(entrada, costeo)
        fila.commercial_net_total = costeo.commercial_net_total
        fila.commercial_tax_total = costeo.commercial_tax_total
        fila.commercial_gross_total = costeo.commercial_gross_total
        fila.estimated_days = costeo.estimated_days
        fila.target_date = costeo.target_date

        fila.code = await self._sequences.issue(
            SequenceType.PROTOTYPE_QUOTE, user_id=user.id, moment=momento
        )
        fila.status = PrototypeQuotationStatus.CONFIRMED
        fila.confirmed_at = datetime.now(UTC)
        await self._session.flush()

        self._audit.record_action(
            entity_type=ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "event": "CONFIRMED",
                "code": fila.code,
                "total": str(costeo.commercial_gross_total),
            },
        )
        return fila

    async def cancel(self, quotation_id: int, *, user: AuthenticatedUser) -> PrototypeQuotation:
        fila = await self.get(quotation_id, for_update=True)
        if fila.payment_status is PrototypeQuotationPaymentStatus.PAID:
            raise PrototypeQuotationNotCancellableError()
        if fila.status is PrototypeQuotationStatus.CANCELLED:
            return fila
        fila.status = PrototypeQuotationStatus.CANCELLED
        fila.cancelled_at = datetime.now(UTC)
        await self._session.flush()
        self._audit.record_action(
            entity_type=ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"event": "CANCELLED"},
        )
        return fila

    async def mark_paid(
        self, quotation_id: int, *, user: AuthenticatedUser
    ) -> tuple[PrototypeQuotation, Prototype]:
        """Registra el cobro y habilita la muestra para el taller.

        Cobrar NO gasta material. Lo unico que hace es abrir la puerta: la
        muestra fisica queda creada y arrancable, y el consumo ocurre al
        arrancarla, que es cuando el barro sale de verdad del almacen.

        Es idempotente porque un reintento del navegador no puede duplicar ni
        el cobro ni la muestra.
        """
        fila = await self.get(quotation_id, for_update=True)
        if fila.status is not PrototypeQuotationStatus.CONFIRMED:
            raise PrototypeQuotationNotPayableError()

        muestra = await self._session.scalar(
            select(Prototype).where(Prototype.prototype_quotation_id == fila.id).limit(1)
        )
        if fila.payment_status is PrototypeQuotationPaymentStatus.PAID and muestra is not None:
            return fila, muestra

        if fila.payment_status is not PrototypeQuotationPaymentStatus.PAID:
            fila.payment_status = PrototypeQuotationPaymentStatus.PAID
            fila.paid_at = datetime.now(UTC)

        if muestra is None:
            muestra = await self._crear_muestra(fila, user=user)

        await self._session.flush()
        self._audit.record_action(
            entity_type=ENTITY,
            entity_id=str(fila.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"event": "PAID", "prototype_id": muestra.id},
        )
        return fila, muestra

    async def _crear_muestra(
        self, fila: PrototypeQuotation, *, user: AuthenticatedUser
    ) -> Prototype:
        """La muestra fisica que el taller va a fabricar.

        Cruza lo TECNICO y nada de lo comercial: ni totales, ni IGV, ni tarifas.
        El taller necesita saber que pieza es, cuantas y con que material; el
        precio es asunto del documento, y copiarlo aqui crearia una segunda
        copia que algun dia discrepa de la firmada.
        """
        muestra = Prototype(
            code=await self._sequences.issue(SequenceType.PROTOTYPE, user_id=user.id),
            name=fila.description,
            prototype_quotation_id=fila.id,
            product_id=fila.product_id,
            quantity=fila.quantity,
            status=PrototypeStatus.CREATED,
            approval=PrototypeApproval.PENDING,
            requested_at=datetime.now(UTC),
            technical_specifications=fila.technical_specifications,
            created_by=user.id,
            created_by_name=user.display_name,
            lines=[],
        )
        for linea in fila.lines:
            producto = linea.product or await self._session.get(Product, linea.product_id)
            muestra.lines.append(
                PrototypeMaterialLine(
                    product_id=linea.product_id,
                    sort_order=linea.sort_order,
                    # Lo cotizado es por muestra; el taller gasta el total.
                    quantity_planned=linea.quantity_per_prototype * fila.quantity,
                    uom_code=linea.uom_code,
                    product_name_snapshot=(
                        linea.product_name_snapshot or (producto.name if producto else "")
                    ),
                    product_internal_reference_snapshot=(
                        producto.internal_reference if producto else ""
                    ),
                )
            )
        self._session.add(muestra)
        await self._session.flush()
        return muestra


def _snapshot(entrada: PrototypeCostingInput, costeo: PrototypeCosting) -> dict[str, Any]:
    """Lo congelado, en texto.

    Los importes viajan como cadena y no como numero: `jsonable_encoder`
    convierte los `Decimal` a `float`, y un float de dinero pierde centimos por
    el camino sin avisar.
    """
    return {
        "effective": {
            "design_rate": str(entrada.design_rate),
            "artist_rate": str(entrada.artist_rate),
            "mold_maker_price": str(entrada.mold_maker_price),
            "fixed_cost": str(entrada.fixed_cost),
            "tax_percent": str(entrada.tax_percent),
            "rounding_step": str(entrada.rounding_step),
            "firing_rate": str(entrada.firing_rate),
            "firing_days_per_batch": entrada.firing_days_per_batch,
        },
        "breakdown": {
            "design_cost": str(costeo.design_cost),
            "artist_cost": str(costeo.artist_cost),
            "mold_maker_cost": str(costeo.mold_maker_cost),
            "materials_cost": str(costeo.materials_cost),
            "firing_cost": str(costeo.firing_cost),
            "fixed_cost": str(costeo.fixed_cost),
            "base_cost": str(costeo.base_cost),
            "raw_gross_total": str(costeo.raw_gross_total),
        },
        "days": {
            "design": str(costeo.design_days),
            "artist": str(costeo.artist_days),
            "mold_maker": str(costeo.mold_maker_days),
            "drying": str(costeo.drying_days),
            "firing": costeo.firing_days,
            "adjustment": str(costeo.adjustment_days),
            "total": str(costeo.estimated_days),
        },
    }
