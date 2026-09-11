"""Materiales del Cotizador V2: valorizarlos, elegirlos y congelarlos.

Fase 010C. Tres responsabilidades, y conviene separarlas al leer:

1. **valorizar** un material del maestro —cuanto se compro, por cuanto, cuanto
   costo traerlo— para obtener su costo por unidad;
2. **elegir** el esmalte de referencia cuando nadie eligio uno: el activo mas
   caro POR GRAMO;
3. **congelar** en la linea de la cotizacion todo lo que hizo falta para
   calcular su costo.

## Lo que este modulo NO hace

**No toca el inventario.** Cotizar consulta el stock —para avisar— y no lo
mueve ni un gramo. El unico camino de escritura de existencias del proyecto es
`InventoryService.apply_movement`, y este servicio no lo llama. Hay una prueba
que compara el saldo antes y despues de cotizar.

**No crea un segundo maestro.** El material es un `Product` y su existencia
vive en `stock_balances`. Aqui solo se anade lo que V2 necesita saber y el
maestro no guarda.

**No escribe en `products.cost`.** Ese campo lo lee el costeo del Cotizador
historico (`app/services/body_material.py`), asi que recalcularlo cambiaria
precios de Legacy sin que nadie lo hubiera pedido.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.errors import APIError
from app.core.quoter_v2_materials import (
    GLAZE_WEIGHT_RATIO,
    MaterialMathError,
    body_total_weight,
    effective_cost_per_unit,
    glaze_unit_weight,
    glaze_volume_ml,
    material_cost,
)
from app.models.audit import AuditAction
from app.models.inventory import StockBalance
from app.models.masters import Product, ProductType
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct, V2QuotationStatus
from app.models.quoter_v2_materials import V2MaterialCost, V2MaterialKind
from app.models.recipes import PreparationStatus, RecipePreparation
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder

ZERO = Decimal(0)

#: Entidad de auditoria propia. Valorizar un material es una decision comercial
#: y merece su propio rastro, separado del alta del producto en el maestro.
V2_MATERIAL_ENTITY = "v2_material_cost"
#: Las lineas se auditan aparte: anadir material a una cotizacion no es
#: lo mismo que cambiar la politica de valorizacion de la casa.
V2_LINE_ENTITY = "v2_quotation_product"

#: De que tipos de producto puede hacerse una pieza o un recubrimiento. Un
#: producto terminado no forma otro producto terminado y un servicio no tiene
#: masa: ofrecerlos solo daria formas de equivocarse. Mismo criterio que el
#: Cotizador historico aplica en `body_material.py`.
ALLOWED_TYPES = (ProductType.RAW_MATERIAL, ProductType.PREPARED_MATERIAL)


class V2MaterialNotFoundError(APIError):
    status_code = 404
    code = "V2_MATERIAL_NOT_FOUND"
    message = "El material no existe o no esta valorizado para el Cotizador V2"


class V2MaterialProductInvalidError(APIError):
    status_code = 422
    code = "V2_MATERIAL_PRODUCT_INVALID"
    message = "El producto no puede usarse como material: debe ser materia prima o preparado"


class V2MaterialUomMissingError(APIError):
    status_code = 422
    code = "V2_MATERIAL_UOM_MISSING"
    message = "El material no declara unidad base en el maestro"


class V2MaterialMathRejected(APIError):
    """Una entrada imposible, traducida a una respuesta que se entiende."""

    status_code = 422
    code = "V2_MATERIAL_INPUT_INVALID"
    message = "Los datos del material no permiten calcular un costo"


class V2QuotationNotEditableError(APIError):
    """Una cotizacion que ya no es borrador no cambia de material."""

    status_code = 409
    code = "V2_QUOTATION_NOT_EDITABLE"
    message = "La cotizacion ya no es un borrador y su material no puede cambiarse"


class V2MaterialService:
    """Valorizacion de materiales y costeo de la linea de cotizacion."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------
    # Valorizacion
    # ------------------------------------------------------------------
    async def list_materials(
        self, *, kind: V2MaterialKind | None = None
    ) -> list[tuple[V2MaterialCost, Decimal]]:
        """Materiales valorizados y su existencia total.

        El stock viaja junto porque la pantalla lo necesita para avisar, no
        para decidir: un material sin existencia se puede cotizar igual.
        """
        consulta = select(V2MaterialCost).options(joinedload(V2MaterialCost.product))
        if kind is not None:
            consulta = consulta.where(V2MaterialCost.material_kind == kind)
        consulta = consulta.order_by(V2MaterialCost.material_kind, V2MaterialCost.id)
        filas = list((await self._session.scalars(consulta)).all())
        return [(fila, await self.stock_for(fila.product_id)) for fila in filas]

    async def stock_for(self, product_id: int) -> Decimal:
        """Existencia total del material, sumando ubicaciones.

        Solo se LEE. Ninguna operacion de cotizacion la modifica.
        """
        total = await self._session.scalar(
            select(func.coalesce(func.sum(StockBalance.quantity), 0)).where(
                StockBalance.product_id == product_id
            )
        )
        return Decimal(total or 0)

    async def upsert_material(
        self, product_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> V2MaterialCost:
        """Da de alta o actualiza la valorizacion de un material.

        El costo por unidad no se escribe: lo deriva la base a partir de los
        hechos de la adquisicion, de modo que no puede quedar desfasado.
        """
        product = await self._session.get(Product, product_id)
        if product is None or not product.active:
            raise V2MaterialNotFoundError("El producto no existe o esta inactivo")
        if product.product_type not in ALLOWED_TYPES:
            raise V2MaterialProductInvalidError()
        if product.base_uom_code is None:
            raise V2MaterialUomMissingError()

        fila = (
            await self._session.scalars(
                select(V2MaterialCost).where(V2MaterialCost.product_id == product_id)
            )
        ).one_or_none()

        accion = AuditAction.UPDATE
        if fila is None:
            fila = V2MaterialCost(product_id=product_id)
            self._session.add(fila)
            accion = AuditAction.CREATE

        for campo in (
            "material_kind",
            "origin",
            "purchase_quantity",
            "purchase_cost",
            "transport_cost",
            "costing_override_per_unit",
            "ml_per_gram",
            "notes",
        ):
            if campo in data:
                setattr(fila, campo, data[campo])

        # Se comprueba aqui, con un mensaje que dice que pasa, en vez de dejar
        # que reviente el CHECK y devuelva un 500 sin explicacion.
        if fila.purchase_quantity is None or fila.purchase_quantity <= ZERO:
            raise V2MaterialMathRejected(
                "La cantidad adquirida tiene que ser mayor que cero",
                code="V2_MATERIAL_QUANTITY_INVALID",
            )

        await self._session.flush()
        # Dos motivos para releer, no uno:
        #
        # - `effective_cost_per_unit` es una columna GENERADA: la calcula la
        #   base y no existe en memoria hasta que se pide;
        # - `product` es una relacion, y en una fila recien creada no esta
        #   cargada. Tocarla al presentar dispararia una carga diferida que en
        #   contexto asincrono no se queda lenta: revienta con MissingGreenlet.
        await self._session.refresh(fila, ["effective_cost_per_unit"])
        fila.product = product

        self._audit.record_action(
            entity_type=V2_MATERIAL_ENTITY,
            entity_id=str(fila.id),
            action=accion,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "product_id": str(product_id),
                "material_kind": fila.material_kind.value,
                "origin": fila.origin.value,
                "effective_cost_per_unit": str(fila.effective_cost_per_unit),
            },
        )
        return fila

    async def get_material(self, product_id: int) -> V2MaterialCost:
        fila = (
            await self._session.scalars(
                select(V2MaterialCost)
                .where(V2MaterialCost.product_id == product_id)
                .options(joinedload(V2MaterialCost.product))
            )
        ).one_or_none()
        if fila is None:
            raise V2MaterialNotFoundError()
        return fila

    # ------------------------------------------------------------------
    # Eleccion del esmalte de referencia
    # ------------------------------------------------------------------
    async def most_expensive_glaze(self) -> V2MaterialCost | None:
        """El esmalte ACTIVO mas caro POR GRAMO, o ninguno si no hay.

        Por gramo, no por envase. Un bote de 20 soles que rinde 100 g cuesta
        0,20 el gramo y es mas caro que uno de 50 soles que rinde 1.000 g, que
        cuesta 0,05: comparar el precio del envase elegiria el equivocado.

        Y el mas caro y no el primero porque una cotizacion preliminar debe
        pecar por arriba: si al final se usa uno mas barato, el taller gana; al
        reves, se pierde dinero sobre un precio ya comprometido.

        **El stock no entra en la comparacion.** Un esmalte sin existencia
        sigue sirviendo como referencia de costeo: la finalidad es proteger el
        precio de la oferta, y produccion elegira el esmalte real.

        El desempate por id descendente hace la eleccion reproducible cuando
        dos esmaltes cuestan exactamente lo mismo.
        """
        return await self._session.scalar(
            select(V2MaterialCost)
            .join(Product, Product.id == V2MaterialCost.product_id)
            .where(
                V2MaterialCost.material_kind == V2MaterialKind.GLAZE,
                Product.active,
                V2MaterialCost.effective_cost_per_unit > ZERO,
            )
            .options(joinedload(V2MaterialCost.product))
            .order_by(
                V2MaterialCost.effective_cost_per_unit.desc(),
                V2MaterialCost.id.desc(),
            )
            .limit(1)
        )

    async def ml_per_gram_for(self, material: V2MaterialCost) -> Decimal | None:
        """La conversion g/ml del material, o `None` si no hay ninguna.

        Se prefiere la declarada en la valorizacion. Si no la hay y el material
        es un preparado, se usa la concentracion del ultimo lote —que es la
        conversion que el proyecto ya calcula en 009D—, porque no existe una
        densidad universal: es propia de cada preparado.

        `None` significa que no hay dato, no que valga 1. Quien reciba `None`
        tendra que declarar que usa la conversion de reserva.
        """
        if material.ml_per_gram is not None:
            return material.ml_per_gram

        concentracion = await self._session.scalar(
            select(RecipePreparation.solids_g_per_ml)
            .where(
                RecipePreparation.prepared_product_id == material.product_id,
                RecipePreparation.status == PreparationStatus.COMPLETED,
                RecipePreparation.solids_g_per_ml > ZERO,
            )
            .order_by(RecipePreparation.id.desc())
            .limit(1)
        )
        if concentracion is None:
            return None
        # `solids_g_per_ml` son gramos por mililitro; lo que hace falta es su
        # inverso. Usarla tal cual invertiria la conversion sin avisar.
        return Decimal(1) / concentracion

    # ------------------------------------------------------------------
    # Lineas de la cotizacion
    # ------------------------------------------------------------------
    async def list_lines(self, quotation_id: int) -> list[V2QuotationProduct]:
        consulta = (
            select(V2QuotationProduct)
            .where(V2QuotationProduct.v2_quotation_id == quotation_id)
            .order_by(V2QuotationProduct.sort_order, V2QuotationProduct.id)
        )
        return list((await self._session.scalars(consulta)).all())

    async def add_line(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> tuple[V2QuotationProduct, list[str]]:
        """Anade una linea y calcula su material.

        Solo sobre borradores: una cotizacion emitida ya comprometio un precio,
        y anadirle material la cambiaria por detras.
        """
        quotation = await self._draft(quotation_id)

        siguiente = await self._session.scalar(
            select(func.coalesce(func.max(V2QuotationProduct.sort_order), -1) + 1).where(
                V2QuotationProduct.v2_quotation_id == quotation_id
            )
        )
        linea = V2QuotationProduct(
            v2_quotation_id=quotation.id,
            sort_order=int(siguiente or 0),
            quantity=int(data.get("quantity") or 0),
        )
        self._session.add(linea)
        avisos = await self._fill_line(linea, data)
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_LINE_ENTITY,
            entity_id=str(linea.id),
            action=AuditAction.CREATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        return linea, avisos

    async def update_line(
        self,
        quotation_id: int,
        line_id: int,
        data: dict[str, Any],
        *,
        user: AuthenticatedUser,
    ) -> tuple[V2QuotationProduct, list[str]]:
        await self._draft(quotation_id)
        linea = await self._line(quotation_id, line_id)
        if "quantity" in data:
            linea.quantity = int(data["quantity"] or 0)
        avisos = await self._fill_line(linea, data)
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_LINE_ENTITY,
            entity_id=str(linea.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        return linea, avisos

    async def delete_line(
        self, quotation_id: int, line_id: int, *, user: AuthenticatedUser
    ) -> None:
        await self._draft(quotation_id)
        linea = await self._line(quotation_id, line_id)
        await self._session.delete(linea)
        self._audit.record_action(
            entity_type=V2_LINE_ENTITY,
            entity_id=str(line_id),
            action=AuditAction.DELETE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={"quotation_id": str(quotation_id)},
        )
        await self._session.flush()

    async def _fill_line(self, linea: V2QuotationProduct, data: dict[str, Any]) -> list[str]:
        """Rellena la pieza y recalcula el material entero.

        Se recalcula SIEMPRE, aunque el cambio parezca no afectar al material:
        subir la cantidad cambia el peso total, y un costo que no se recalcula
        es un costo que deja de corresponder a su linea.
        """
        if "product_id" in data:
            linea.product_id = data["product_id"]
            if linea.product_id is not None:
                producto = await self._session.get(Product, linea.product_id)
                if producto is None:
                    raise V2MaterialNotFoundError("El producto de la linea no existe")
                linea.product_name_snapshot = producto.name
                # El peso canonico del maestro se ofrece como punto de partida
                # cuando la linea todavia no tiene uno propio. No lo pisa: lo
                # que el usuario ya escribio manda.
                if linea.body_unit_weight is None and producto.grammage is not None:
                    linea.body_unit_weight = producto.grammage
            else:
                linea.product_name_snapshot = None
        return await self.apply_materials(linea, data)

    async def _draft(self, quotation_id: int) -> V2Quotation:
        quotation = await self._session.get(V2Quotation, quotation_id)
        if quotation is None:
            raise V2MaterialNotFoundError("La cotizacion V2 no existe")
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2QuotationNotEditableError()
        return quotation

    async def _line(self, quotation_id: int, line_id: int) -> V2QuotationProduct:
        linea = (
            await self._session.scalars(
                select(V2QuotationProduct).where(
                    V2QuotationProduct.id == line_id,
                    # El id de la cotizacion NO sobra: sin el, conocer un id de
                    # linea bastaria para editar la de otra cotizacion.
                    V2QuotationProduct.v2_quotation_id == quotation_id,
                )
            )
        ).one_or_none()
        if linea is None:
            raise V2MaterialNotFoundError("La linea no existe en esta cotizacion")
        return linea

    # ------------------------------------------------------------------
    # Congelado en la linea
    # ------------------------------------------------------------------
    async def apply_materials(self, line: V2QuotationProduct, data: dict[str, Any]) -> list[str]:
        """Calcula el material de una linea y lo deja congelado en ella.

        Devuelve AVISOS, no excepciones, para lo que no impide calcular: un
        borrador tiene que poder guardarse a medias y arreglarse despues. Lo
        que si impide calcular se rechaza con un error.
        """
        avisos: list[str] = []
        try:
            avisos += await self._apply_body(line, data)
            avisos += await self._apply_glaze(line, data)
        except MaterialMathError as error:
            raise V2MaterialMathRejected(str(error)) from error
        return avisos

    async def _apply_body(self, line: V2QuotationProduct, data: dict[str, Any]) -> list[str]:
        if "body_material_id" in data:
            line.body_material_id = data["body_material_id"]
        if "body_unit_weight" in data:
            line.body_unit_weight = data["body_unit_weight"]

        if line.body_material_id is None:
            line.body_material_name_snapshot = None
            line.body_uom_snapshot = None
            line.body_cost_per_unit_snapshot = None
            line.body_cost_is_override = False
            line.body_total_weight = ZERO
            line.body_cost = ZERO
            return []

        material = await self.get_material(line.body_material_id)
        override = data.get("body_cost_per_unit_override")

        line.body_material_name_snapshot = material.product.name
        line.body_uom_snapshot = material.product.base_uom_code
        line.body_cost_is_override = override is not None
        line.body_cost_per_unit_snapshot = effective_cost_per_unit(
            material.effective_cost_per_unit, override
        )
        line.body_total_weight = body_total_weight(line.body_unit_weight or ZERO, line.quantity)
        line.body_cost = material_cost(line.body_total_weight, line.body_cost_per_unit_snapshot)

        # Elegir la pasta sin decir cuanta lleva la pieza deja la linea en cero.
        # Es un estado legitimo de un borrador a medio llenar, asi que avisa en
        # vez de bloquear: lo que no puede es pasar desapercibido.
        if line.body_unit_weight is None or line.body_unit_weight <= ZERO:
            return ["V2_BODY_WEIGHT_REQUIRED"]
        return []

    async def _apply_glaze(self, line: V2QuotationProduct, data: dict[str, Any]) -> list[str]:
        if "requires_glaze" in data:
            line.requires_glaze = bool(data["requires_glaze"])
        if "glaze_material_id" in data:
            line.glaze_material_id = data["glaze_material_id"]

        if not line.requires_glaze:
            # Apagado es apagado: sin peso, sin volumen y sin costo. El CHECK
            # de la tabla lo vuelve a exigir por si alguien escribe por otra via.
            line.glaze_material_id = None
            line.glaze_material_name_snapshot = None
            line.glaze_is_reference = False
            line.glaze_cost_per_unit_snapshot = None
            line.glaze_cost_is_override = False
            line.glaze_percent_snapshot = None
            line.glaze_ml_per_gram_snapshot = None
            line.glaze_conversion_is_fallback = False
            line.glaze_total_weight = ZERO
            line.glaze_volume_ml = ZERO
            line.glaze_cost = ZERO
            return []

        avisos: list[str] = []
        material: V2MaterialCost | None = None
        if line.glaze_material_id is not None:
            material = await self.get_material(line.glaze_material_id)
            line.glaze_is_reference = False
        else:
            material = await self.most_expensive_glaze()
            line.glaze_is_reference = material is not None
            if material is not None:
                line.glaze_material_id = material.product_id

        if material is None:
            # Sin ningun esmalte valorizado y activo no hay con que costear.
            # Se avisa y se deja el borrador abierto en vez de reventar.
            line.glaze_material_name_snapshot = None
            line.glaze_cost_per_unit_snapshot = None
            line.glaze_percent_snapshot = None
            line.glaze_total_weight = ZERO
            line.glaze_volume_ml = ZERO
            line.glaze_cost = ZERO
            return ["V2_GLAZE_NO_ACTIVE_MATERIAL"]

        override = data.get("glaze_cost_per_unit_override")
        line.glaze_material_name_snapshot = material.product.name
        line.glaze_cost_is_override = override is not None
        line.glaze_cost_per_unit_snapshot = effective_cost_per_unit(
            material.effective_cost_per_unit, override
        )
        # Se congela como porcentaje —15, no 0,15— igual que el resto de
        # porcentajes del proyecto.
        line.glaze_percent_snapshot = GLAZE_WEIGHT_RATIO * Decimal(100)

        unitario = glaze_unit_weight(line.body_unit_weight or ZERO, requires_glaze=True)
        line.glaze_total_weight = body_total_weight(unitario, line.quantity)

        conversion = await self.ml_per_gram_for(material)
        volumen, es_fallback = glaze_volume_ml(line.glaze_total_weight, conversion)
        line.glaze_volume_ml = volumen
        line.glaze_conversion_is_fallback = es_fallback
        line.glaze_ml_per_gram_snapshot = conversion

        line.glaze_cost = material_cost(line.glaze_total_weight, line.glaze_cost_per_unit_snapshot)

        if await self.stock_for(material.product_id) <= ZERO:
            # Aviso, NUNCA bloqueo: la regla aprobada permite cotizar con un
            # esmalte sin existencia porque lo que se protege es el precio.
            avisos.append("V2_GLAZE_REFERENCE_WITHOUT_STOCK")
        return avisos
