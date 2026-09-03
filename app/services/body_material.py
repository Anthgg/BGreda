"""Material base de la pieza: que material FISICO forma el cuerpo y cuanto lleva.

Hasta esta fase el Cotizador expresaba el cuerpo de la pieza como «receta +
gramos». Funcionaba por accidente: `recipes.product_id` es UNIQUE, asi que
elegir receta equivalia a elegir el preparado que esa receta fabrica. Pero
decia lo que no era —el usuario tenia que conocer la formula para poder
cotizar— y dejaba fuera el caso real de una pieza hecha directamente con una
materia prima, que no tiene receta ninguna.

Aqui el dato principal pasa a ser el que el taller usa de verdad:

    QUE MATERIAL forma el cuerpo, y CUANTO lleva una pieza.

La receta no desaparece ni cambia de sitio: sigue siendo el dominio de
PREPARACION —como se fabrica un preparado a partir de sus componentes— y viaja
en el snapshot como PROCEDENCIA, nunca como intencion del usuario.

**La unidad no es un campo de entrada.** Sale de `products.base_uom_code` y
solo de ahi. Si el navegador pudiera mandarla, un material que el maestro
lleva en gramos podria cotizarse en mililitros sin que nada lo desmintiera, y
la cantidad guardada dejaria de significar algo. Por el mismo motivo la
cantidad por pieza se expresa SIEMPRE en la unidad base del material: asi no
hay ninguna conversion que hacer ni al costear ni al descontar del almacen.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.masters import Product, ProductType, UnitOfMeasure, UomDimension
from app.models.recipes import Recipe
from app.services.recipes import RecipeService

ZERO = Decimal(0)

#: Clave de `production_snapshot` donde vive la intencion y su plan congelado.
#: Mismo patron que `glaze_plan` de 009D: espacio de nombres propio, nunca
#: claves sueltas junto a los costos de la linea.
SNAPSHOT_KEY = "body_material"

#: Que puede ser el cuerpo de una pieza. Un producto terminado no forma otro
#: producto terminado, y un servicio no tiene masa: ofrecerlos en el selector
#: solo daria formas de equivocarse.
ALLOWED_TYPES = (ProductType.RAW_MATERIAL, ProductType.PREPARED_MATERIAL)

SOURCE_BY_TYPE = {
    ProductType.RAW_MATERIAL: "RAW",
    ProductType.PREPARED_MATERIAL: "PREPARED",
}

#: El material elegido no existe, esta inactivo, o no es un material del que
#: se pueda hacer una pieza.
INVALID = "BODY_MATERIAL_PRODUCT_INVALID"
#: El material no declara unidad base, o su unidad ya no esta en el maestro.
UOM_UNKNOWN = "BODY_MATERIAL_UOM_UNKNOWN"
#: No hay forma de saber cuanto cuesta una unidad de este material.
COST_UNAVAILABLE = "BODY_MATERIAL_COST_UNAVAILABLE"
#: Se sabe lo que cuesta un GRAMO del preparado, pero el material se lleva en
#: una unidad de otra dimension (ml, l, unidad). El puente g <-> ml es
#: `solids_g_per_ml`, que es de un LOTE y no del producto: sin el, cualquier
#: cifra seria inventada. Se bloquea en vez de costear mal en silencio.
UNSUPPORTED_UOM_COSTING = "BODY_MATERIAL_UNSUPPORTED_UOM_COSTING"

BLOCKING_CODES = frozenset({INVALID, UOM_UNKNOWN, COST_UNAVAILABLE, UNSUPPORTED_UOM_COSTING})


@dataclass(frozen=True)
class BodyMaterialResolution:
    """Material base resuelto contra el maestro, con su costo ya calculado."""

    #: Lo que el usuario pidio, exista o no. Se conserva aunque el material
    #: resulte invalido: perderle la eleccion al reabrir el borrador le
    #: borraria el trabajo sin decirle por que.
    requested_product_id: int
    product: Product | None
    quantity_per_piece: Decimal
    uom: str | None
    source: str | None
    recipe_id: int | None
    recipe_version_id: int | None
    recipe_version_fingerprint: str | None
    recipe_name: str | None
    unit_cost: Decimal | None
    required_quantity: Decimal | None
    material_cost: Decimal | None
    warnings: tuple[str, ...]

    @property
    def usable(self) -> bool:
        """El material se resolvio y se pudo costear. Falso bloquea confirmar."""
        return not BLOCKING_CODES.intersection(self.warnings)

    @property
    def grams_per_piece(self) -> Decimal | None:
        """La cantidad por pieza EN GRAMOS, o `None` si no se lleva en masa.

        Es lo unico que puede viajar a la columna legacy
        `material_grams_per_piece`, que no tiene unidad y por tanto solo
        admite gramos. Un material en mililitros deja esa columna en NULL: no
        escribir nada dice la verdad, escribir el numero mentiria sobre la
        unidad.
        """
        return self.quantity_per_piece if self.uom == "g" else None


def _text(value: Decimal | None) -> str | None:
    """Un decimal, como TEXTO exacto.

    `jsonable_encoder` convierte los `Decimal` a `float` al guardar el
    snapshot, y de ahi sale el requerimiento que la orden de produccion
    descuenta del almacen. Un binario que no representa 0,1 exactamente no
    puede ser la ultima palabra sobre cuanto material sacar: se guarda el
    texto, que sobrevive el viaje intacto.
    """
    return None if value is None else format(value, "f")


def snapshot(resolution: BodyMaterialResolution) -> dict[str, Any]:
    """Serializa la eleccion y sus derivados para `production_snapshot`.

    Se guardan las dos cosas, como en `glaze_plan`: la ENTRADA del usuario
    (`product_id`, `quantity_per_piece`) y los DERIVADOS congelados (unidad,
    costo unitario, requerimiento, costo total). Los derivados no son
    redundancia: una cotizacion confirmada tiene que poder explicarse sola
    aunque manana cambie el maestro del material.
    """
    product = resolution.product
    return {
        "product_id": resolution.requested_product_id,
        "product_internal_reference": product.internal_reference if product else None,
        "product_name": product.name if product else None,
        "product_type": product.product_type.value if product else None,
        "quantity_per_piece": _text(resolution.quantity_per_piece),
        "uom": resolution.uom,
        "source": resolution.source,
        # Procedencia, no intencion: de que receta salio este preparado. NULL
        # para una materia prima, que no se fabrica con ninguna.
        "recipe_id_used": resolution.recipe_id,
        "recipe_version_id_used": resolution.recipe_version_id,
        "recipe_version_fingerprint_snapshot": resolution.recipe_version_fingerprint,
        "recipe_name_snapshot": resolution.recipe_name,
        "unit_cost_snapshot": _text(resolution.unit_cost),
        "required_quantity": _text(resolution.required_quantity),
        "material_cost": _text(resolution.material_cost),
    }


def stored_selection(production_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Lee el material base guardado, o `None` si la linea es anterior a esto.

    `None` es la respuesta correcta para las cotizaciones historicas y hay que
    tratarlo como tal: se cae a la lectura legacy por receta. Fabricarles un
    material base leyendo el maestro de hoy les inventaria una historia que
    nadie escribio.
    """
    stored = production_snapshot.get(SNAPSHOT_KEY)
    if not isinstance(stored, dict):
        return None
    if stored.get("product_id") is None:
        return None
    return stored


class BodyMaterialResolver:
    """Resuelve el material base contra el maestro. Unica autoridad del costo."""

    def __init__(self, session: AsyncSession, recipes: RecipeService | None = None) -> None:
        self._session = session
        # Reutiliza el costeo recursivo que ya existe para los componentes de
        # receta en vez de escribir un segundo motor de costos que divergiria
        # del primero en cuanto uno de los dos cambiara.
        self._recipes = recipes or RecipeService(session)

    async def resolve(
        self, product_id: int, quantity_per_piece: Decimal, quantity: int | None
    ) -> BodyMaterialResolution:
        product = await self._session.get(Product, product_id)
        if product is None or not product.active or product.product_type not in ALLOWED_TYPES:
            return BodyMaterialResolution(
                requested_product_id=product_id,
                product=None,
                quantity_per_piece=quantity_per_piece,
                uom=None,
                source=None,
                recipe_id=None,
                recipe_version_id=None,
                recipe_version_fingerprint=None,
                recipe_name=None,
                unit_cost=None,
                required_quantity=None,
                material_cost=None,
                warnings=(INVALID,),
            )

        recipe = await self._recipe_for(product)
        version = recipe.current_version if recipe else None
        unit_cost, warnings = await self._unit_cost(product)

        required = (
            quantity_per_piece * Decimal(quantity)
            if quantity is not None and quantity > 0
            else None
        )
        cost = unit_cost * required if unit_cost is not None and required is not None else None

        return BodyMaterialResolution(
            requested_product_id=product_id,
            product=product,
            quantity_per_piece=quantity_per_piece,
            uom=product.base_uom_code,
            source=SOURCE_BY_TYPE[product.product_type],
            recipe_id=recipe.id if recipe else None,
            recipe_version_id=version.id if version else None,
            recipe_version_fingerprint=version.fingerprint if version else None,
            recipe_name=recipe.name if recipe else None,
            unit_cost=unit_cost,
            required_quantity=required,
            material_cost=cost,
            warnings=tuple(warnings),
        )

    async def list_options(
        self, *, search: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Materiales que pueden ser el cuerpo de una pieza, paginados.

        La regla de QUE puede serlo vive aqui y no en React: el navegador no
        decide que categorias del maestro representan materia. Se buscan por
        codigo o por nombre, que es como el taller los nombra.
        """
        stmt = select(Product).where(
            Product.active.is_(True),
            Product.product_type.in_(ALLOWED_TYPES),
        )
        if search and search.strip():
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                Product.internal_reference.ilike(pattern) | Product.name.ilike(pattern)
            )
        total = await self._session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(Product.internal_reference)
                    .limit(max(1, min(limit, 200)))
                    .offset(max(0, offset))
                )
            )
            .scalars()
            .all()
        )

        options: list[dict[str, Any]] = []
        for product in rows:
            recipe = await self._recipe_for(product)
            unit_cost, warnings = await self._unit_cost(product)
            options.append(
                {
                    "product_id": product.id,
                    "internal_reference": product.internal_reference,
                    "name": product.name,
                    "product_type": product.product_type.value,
                    "source": SOURCE_BY_TYPE[product.product_type],
                    "uom": product.base_uom_code,
                    "recipe_name": recipe.name if recipe else None,
                    # Se listan tambien los que no se pueden costear. Esconder
                    # un material que el taller usa de verdad no lo arregla:
                    # deja al usuario buscando algo que no aparece y sin saber
                    # por que. Se muestra, y se avisa.
                    "costable": unit_cost is not None and not warnings,
                }
            )
        return options, int(total or 0)

    async def _recipe_for(self, product: Product) -> Recipe | None:
        """La receta que fabrica este preparado, si la hay. Solo procedencia.

        `recipes.product_id` es UNIQUE, asi que como mucho hay una y no hay
        nada que elegir. Una materia prima no tiene ninguna, y eso no es un
        dato que falte: es que no existe.
        """
        if product.product_type is not ProductType.PREPARED_MATERIAL:
            return None
        return (
            await self._session.execute(
                select(Recipe)
                .where(Recipe.product_id == product.id, Recipe.active.is_(True))
                # La version vigente se trae en la misma consulta: en sesion
                # asincrona una relacion perezosa no se puede resolver despues.
                .options(selectinload(Recipe.current_version))
            )
        ).scalar_one_or_none()

    async def _unit_cost(self, product: Product) -> tuple[Decimal | None, list[str]]:
        """Cuanto cuesta UNA unidad base del material, o por que no se sabe.

        El costo del maestro manda cuando existe: es el precio real de compra o
        de preparacion, y ya viene expresado por unidad base.
        """
        if product.cost is not None and product.cost > ZERO:
            return product.cost, []

        uom = await self._session.get(UnitOfMeasure, product.base_uom_code or "")
        if uom is None or not uom.active:
            return None, [UOM_UNKNOWN]

        if product.product_type is ProductType.PREPARED_MATERIAL:
            # Un preparado sin costo propio todavia se puede costear por su
            # receta, pero ese calculo devuelve soles por GRAMO. Convertirlo a
            # una unidad de volumen exigiria una densidad que el producto no
            # tiene, asi que ahi se para.
            if uom.dimension is not UomDimension.MASS:
                return None, [UNSUPPORTED_UOM_COSTING]
            per_gram = await self._recipes.material_cost_per_gram(product)
            if per_gram > ZERO:
                # `factor_to_base` son los gramos que hay en una unidad de esta
                # unidad: 1 para g, 1000 para kg.
                return per_gram * uom.factor_to_base, []

        return None, [COST_UNAVAILABLE]
