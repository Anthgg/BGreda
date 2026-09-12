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
from sqlalchemy.exc import IntegrityError
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
from app.services.quoter_v2_firing import (
    apply_line_geometry,
    copy_master_dimensions,
    refresh_firing,
)

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


class V2MaterialVersionConflictError(APIError):
    """Alguien mas guardo esta valorizacion mientras estaba abierta en pantalla."""

    status_code = 409
    code = "V2_MATERIAL_VERSION_CONFLICT"
    message = "La valorizacion cambio desde que se abrio. Vuelva a cargarla y repita el cambio"


class V2MaterialKindMismatchError(APIError):
    """Una pasta no puede cobrarse como esmalte, ni al reves."""

    status_code = 422
    code = "V2_MATERIAL_KIND_MISMATCH"
    message = "El material no esta valorizado para ese uso"


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
        self,
        product_id: int,
        data: dict[str, Any],
        *,
        expected_version: int | None = None,
        user: AuthenticatedUser,
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

        # `with_for_update`: comprobar la version leyendo sin bloquear no basta.
        # Dos administradores que abran la pantalla a la vez leerian version 1
        # los dos, los dos pasarian la comprobacion y el ultimo en confirmar
        # pisaria al primero sin que nadie viera un conflicto.
        fila = (
            await self._session.scalars(
                select(V2MaterialCost)
                .where(V2MaterialCost.product_id == product_id)
                # `of=`: la relacion con el producto se carga con un LEFT JOIN,
                # y PostgreSQL no deja bloquear el lado anulable de un join.
                # Se bloquea la fila que se va a escribir, que es la unica que
                # hace falta: el maestro no se toca aqui.
                .with_for_update(of=V2MaterialCost)
            )
        ).one_or_none()

        accion = AuditAction.UPDATE
        if fila is None:
            fila = V2MaterialCost(product_id=product_id)
            self._session.add(fila)
            accion = AuditAction.CREATE
        else:
            # La primera valorizacion no tiene version que declarar; cambiar
            # una que ya existe, si. Omitirla seria exactamente el caso que
            # este control existe para impedir.
            if expected_version is None or fila.version != expected_version:
                raise V2MaterialVersionConflictError()
            fila.version += 1

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

        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Dos primeras valorizaciones simultaneas del mismo material: no hay
            # fila que bloquear todavia, asi que las dos pasan la lectura y una
            # choca contra el UNIQUE. Es el mismo conflicto de concurrencia que
            # cubre la version, y merece la misma respuesta y no un 500.
            raise V2MaterialVersionConflictError() from exc

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

    def _product_is_usable(self, product: Product) -> bool:
        """Si el producto sigue pudiendo ser material HOY.

        Se comprueba al usarlo y no solo al valorizarlo porque el maestro es
        editable: un producto valorizado como materia prima puede acabar
        convertido en servicio, quedarse sin unidad base o darse de baja, y la
        valorizacion seguiria ahi, intacta y ya sin sentido.
        """
        return (
            product.active
            and product.product_type in ALLOWED_TYPES
            and product.base_uom_code is not None
        )

    async def _material_for(
        self, product_id: int, kind: V2MaterialKind, *, elegido_ahora: bool
    ) -> tuple[V2MaterialCost, list[str]]:
        """La valorizacion de un material, comprobando que sirve para ESE uso.

        La dureza depende de quien pregunta, y la diferencia importa:

        - si la peticion ESTA eligiendo el material, se rechaza. Elegir hoy una
          pasta que ya no es pasta es un error que hay que decir en la cara;
        - si el material ya estaba puesto en la linea y esta peticion solo
          cambia la cantidad, se AVISA. Bloquear ahi dejaria un borrador
          encallado por una decision que se tomo en otra pantalla, y la linea
          conserva igualmente lo que congelo.
        """
        material = await self.get_material(product_id)
        if material.material_kind is kind and self._product_is_usable(material.product):
            return material, []
        if elegido_ahora:
            if material.material_kind is not kind:
                raise V2MaterialKindMismatchError(
                    f"«{material.product.name}» esta valorizado como"
                    f" {material.material_kind.value}, no como {kind.value}"
                )
            raise V2MaterialProductInvalidError(
                f"«{material.product.name}» ya no puede usarse como material:"
                " esta inactivo, cambio de tipo o se quedo sin unidad base"
            )
        aviso = (
            "V2_BODY_MATERIAL_UNAVAILABLE"
            if kind is V2MaterialKind.BODY
            else "V2_GLAZE_MATERIAL_UNAVAILABLE"
        )
        return material, [aviso]

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
        # Fase 010E. La quema depende del volumen de TODAS las lineas: anadir
        # una pieza puede cambiar el numero de hornadas y el reparto del costo
        # entre productos. Sin este recalculo la cabecera seguiria diciendo las
        # hornadas de antes de esta linea.
        avisos += await refresh_firing(self._session, quotation)

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
        quotation = await self._draft(quotation_id)
        linea = await self._line(quotation_id, line_id)
        if "quantity" in data:
            linea.quantity = int(data["quantity"] or 0)
        avisos = await self._fill_line(linea, data)
        await self._session.flush()
        avisos += await refresh_firing(self._session, quotation)

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
        quotation = await self._draft(quotation_id)
        linea = await self._line(quotation_id, line_id)
        await self._session.delete(linea)
        await self._session.flush()
        # Quitar una pieza tambien cambia la carga del horno: lo que quede
        # tiene que volver a repartirse el costo de la quema entre menos.
        await refresh_firing(self._session, quotation)
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
                # Fase 010E. Mismo criterio con las medidas: se ofrecen las del
                # catalogo y no se pisan las de la linea.
                copy_master_dimensions(linea, producto)
            else:
                linea.product_name_snapshot = None

        # Un nombre suelto, para la pieza que no esta en el catalogo: la mitad
        # del trabajo del taller son encargos que no existen como producto y
        # que aun asi hay que poder nombrar en la cotizacion. Solo se usa si la
        # linea no cuelga de un producto; si cuelga, manda el maestro y tener
        # dos nombres seria tener dos verdades.
        if "product_name" in data and linea.product_id is None:
            nombre = (data["product_name"] or "").strip()
            linea.product_name_snapshot = nombre or None

        # Fase 010E. La geometria se recalcula SIEMPRE, igual que el material y
        # por el mismo motivo: subir la cantidad cambia el volumen total, y un
        # volumen que no se recalcula deja de corresponder a su linea —y con
        # el, las hornadas de toda la cotizacion—.
        apply_line_geometry(linea, data)
        return await self.apply_materials(linea, data)

    async def _draft(self, quotation_id: int) -> V2Quotation:
        """La cotizacion, bloqueada, si todavia admite cambios de material.

        `with_for_update` sobre la CABECERA hace dos trabajos a la vez:

        - convierte «esta en borrador» en una barrera de verdad. Leerlo sin
          bloquear deja una ventana entre la comprobacion y el guardado por la
          que una emision simultanea colaria material en una cotizacion ya
          comprometida;
        - serializa las escrituras de lineas de una misma cotizacion, que es
          lo que necesita `add_line` para que dos altas a la vez no repitan el
          mismo `sort_order`, y lo que hace que dos ediciones parciales de la
          misma linea se apliquen una detras de otra en vez de recalcular las
          dos sobre el mismo estado viejo.
        """
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
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

    @staticmethod
    def _costo_congelado(
        data: dict[str, Any],
        campo: str,
        *,
        derivado: Decimal,
        era_override: bool,
        actual: Decimal | None,
        cambio_de_material: bool,
    ) -> tuple[Decimal, bool]:
        """Que costo por unidad se congela, y si es una decision de la casa.

        El caso que esto existe para evitar no lanza ningun error: la pantalla
        manda solo lo que cambio —`{"quantity": 10}`—, y leer el override con
        `data.get(...)` devolveria `None`, que es indistinguible de «quitalo».
        Un precio pactado con el cliente volveria al del maestro en silencio,
        al cambiar la cantidad.

        Tres situaciones y tres respuestas:

        - el override VIENE en la peticion: manda, sea un valor o un `None`
          explicito que lo retira;
        - no viene, pero la linea CAMBIA de material: la decision se tomo sobre
          otro material y no se hereda;
        - no viene y el material es el mismo: se conserva lo pactado, que es lo
          que la linea ya tenia congelado.
        """
        if campo in data:
            override = data[campo]
            return effective_cost_per_unit(derivado, override), override is not None
        if era_override and not cambio_de_material and actual is not None:
            return actual, True
        return derivado, False

    async def _apply_body(self, line: V2QuotationProduct, data: dict[str, Any]) -> list[str]:
        # Que el material VENGA en la peticion no significa que se haya
        # cambiado: un cliente que reenvia el formulario entero manda el mismo
        # de siempre. Solo un cambio real retira el costo pactado, porque el
        # pacto se tomo sobre ESE material.
        cambio_de_material = (
            "body_material_id" in data and data["body_material_id"] != line.body_material_id
        )
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

        material, avisos = await self._material_for(
            line.body_material_id,
            V2MaterialKind.BODY,
            # El mismo criterio que para el costo pactado: reenviar el mismo
            # material no es elegirlo hoy. Mirarlo por la presencia de la clave
            # bloqueaba una linea cuyo material se retiro DESPUES, que es justo
            # el caso que 010C decidio resolver con un aviso.
            elegido_ahora=cambio_de_material,
        )
        # Si el material ya no sirve para este uso, se conserva lo congelado y
        # NO se recalcula contra su valorizacion actual: seguir cobrando una
        # pasta con el precio de un esmalte —porque alguien corrigio el tipo en
        # el maestro— daria un importe falso que el aviso no arregla. Se
        # recalculan pesos e importes, que dependen de la linea y no del
        # maestro, y el costo por unidad se queda donde estaba.
        if not avisos:
            line.body_material_name_snapshot = material.product.name
            line.body_uom_snapshot = material.product.base_uom_code
            costo, es_override = self._costo_congelado(
                data,
                "body_cost_per_unit_override",
                derivado=material.effective_cost_per_unit,
                actual=line.body_cost_per_unit_snapshot,
                era_override=line.body_cost_is_override,
                cambio_de_material=cambio_de_material,
            )
            line.body_cost_is_override = es_override
            line.body_cost_per_unit_snapshot = costo
        line.body_total_weight = body_total_weight(line.body_unit_weight or ZERO, line.quantity)
        line.body_cost = material_cost(
            line.body_total_weight, line.body_cost_per_unit_snapshot or ZERO
        )

        # Elegir la pasta sin decir cuanta lleva la pieza deja la linea en cero.
        # Es un estado legitimo de un borrador a medio llenar, asi que avisa en
        # vez de bloquear: lo que no puede es pasar desapercibido.
        if line.body_unit_weight is None or line.body_unit_weight <= ZERO:
            return [*avisos, "V2_BODY_WEIGHT_REQUIRED"]
        return avisos

    async def _apply_glaze(self, line: V2QuotationProduct, data: dict[str, Any]) -> list[str]:
        if "requires_glaze" in data:
            line.requires_glaze = bool(data["requires_glaze"])
        cambio_de_esmalte = (
            "glaze_material_id" in data and data["glaze_material_id"] != line.glaze_material_id
        )
        if "glaze_material_id" in data:
            # Nombrarlo es decidir; mandarlo a nulo es devolver la decision al
            # sistema, que volvera a proponer el activo mas caro por gramo.
            line.glaze_material_id = data["glaze_material_id"]
            line.glaze_is_reference = False

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

        # Quien eligio el esmalte se guarda en `glaze_is_reference`, y hay que
        # mirarlo: la seleccion automatica DEJA el id puesto en la linea, asi
        # que fiarse solo del id convertiria la referencia en eleccion manual
        # en cuanto alguien cambiara la cantidad. La linea dejaria de avisar de
        # que es una referencia, y ademas se quedaria clavada en ese esmalte
        # aunque despues se valorizara otro mas caro.
        elegido = line.glaze_material_id
        if elegido is not None and not line.glaze_is_reference:
            material, propios = await self._material_for(
                elegido,
                V2MaterialKind.GLAZE,
                elegido_ahora=cambio_de_esmalte,
            )
            avisos += propios
        else:
            material = await self.most_expensive_glaze()
            line.glaze_is_reference = material is not None
            line.glaze_material_id = material.product_id if material is not None else None

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

        # Mismo criterio que la pasta: un esmalte que dejo de serlo conserva lo
        # que la linea congelo en vez de recalcular con una valorizacion que ya
        # no corresponde a este uso.
        if not avisos:
            line.glaze_material_name_snapshot = material.product.name
            costo, es_override = self._costo_congelado(
                data,
                "glaze_cost_per_unit_override",
                derivado=material.effective_cost_per_unit,
                actual=line.glaze_cost_per_unit_snapshot,
                era_override=line.glaze_cost_is_override,
                cambio_de_material=cambio_de_esmalte,
            )
            line.glaze_cost_is_override = es_override
            line.glaze_cost_per_unit_snapshot = costo
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

        line.glaze_cost = material_cost(
            line.glaze_total_weight, line.glaze_cost_per_unit_snapshot or ZERO
        )

        if await self.stock_for(material.product_id) <= ZERO:
            # Aviso, NUNCA bloqueo: la regla aprobada permite cotizar con un
            # esmalte sin existencia porque lo que se protege es el precio.
            avisos.append("V2_GLAZE_REFERENCE_WITHOUT_STOCK")
        return avisos
