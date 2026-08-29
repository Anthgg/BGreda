"""Maestros operativos: categorias, unidades, productos y terceros.

Los tres tipos de producto del Plan se amplian a cuatro porque el maestro real
del taller vende clases, que no son ni insumo, ni preparado, ni pieza. Un
servicio puede no tener unidad de medida: no se le inventa una.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.precision import (
    money_numeric,
    percentage_numeric,
    quantity_numeric,
    unit_cost_numeric,
)
from app.db.base import Base, TimestampMixin
from app.db.types import StrEnumType

#: Unidad canonica de masa. El maestro escribe "gr" y la hoja de stock "g":
#: son la misma unidad y se normalizan a esta.
GRAM = "g"
KILOGRAM = "kg"
UNIT = "unit"

#: Alias aceptados durante la importacion. Se resuelven antes de tocar la base;
#: la tabla de unidades solo contiene codigos canonicos.
UOM_ALIASES: dict[str, str] = {
    "g": GRAM,
    "gr": GRAM,
    "gramo": GRAM,
    "gramos": GRAM,
    "kg": KILOGRAM,
    "kilo": KILOGRAM,
    "kilogramo": KILOGRAM,
    "unit": UNIT,
    "unidad": UNIT,
    "unidades": UNIT,
}


class UomDimension(StrEnum):
    """Magnitud fisica. Solo se convierte dentro de la misma dimension.

    Convertir gramos a mililitros NO es una conversion de unidad: depende de la
    concentracion de una preparacion concreta (`recipe_preparations`), no de un
    factor fijo. Por eso MASS y VOLUME siguen siendo dimensiones separadas y el
    puente vive en el dominio, no aqui.
    """

    MASS = "MASS"
    COUNT = "COUNT"
    VOLUME = "VOLUME"


class ProductType(StrEnum):
    RAW_MATERIAL = "RAW_MATERIAL"
    PREPARED_MATERIAL = "PREPARED_MATERIAL"
    FINISHED_PRODUCT = "FINISHED_PRODUCT"
    SERVICE = "SERVICE"


class PartnerRole(StrEnum):
    """Rol comercial. Una misma persona puede ser las dos cosas.

    No existe un rol "sin clasificar": la ausencia de clasificacion es un
    estado de la importacion, no un estado del maestro.
    """

    CLIENT = "CLIENT"
    SUPPLIER = "SUPPLIER"
    BOTH = "BOTH"


class DocumentType(StrEnum):
    RUC = "RUC"
    DNI = "DNI"
    CE = "CE"
    PASSPORT = "PASSPORT"
    OTHER = "OTHER"


class ProductCategory(Base, TimestampMixin):
    """Categoria contable/funcional del producto, jerarquica.

    ``display_path`` es la ruta completa ("Insumos Taller / Pastas") y es la
    clave con la que el maestro de productos referencia su categoria, asi que
    se guarda y se indexa en vez de recalcularla en cada consulta.
    """

    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_categories.id", ondelete="RESTRICT")
    )
    display_path: Mapped[str] = mapped_column(String(400), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(btrim(display_path)) > 0", name="display_path_not_blank"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
        UniqueConstraint("parent_id", "name", name="uq_product_categories_parent_name"),
    )


class PosCategory(Base, TimestampMixin):
    """Categoria de punto de venta.

    Es una jerarquia distinta a la de producto —agrupa por como se vende, no
    por que es— y por eso vive en su propia tabla en lugar de mezclarse.
    """

    __tablename__ = "pos_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("pos_categories.id", ondelete="RESTRICT")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
    )


class UnitOfMeasure(Base, TimestampMixin):
    """Unidad de medida con conversion exacta a la unidad base de su dimension.

    El factor es ``NUMERIC`` y la conversion se hace con ``Decimal``: kg -> g
    debe dar exactamente 1000, no 999.9999999999999.
    """

    __tablename__ = "units_of_measure"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    dimension: Mapped[UomDimension] = mapped_column(StrEnumType(UomDimension, 16), nullable=False)
    #: Cuantas unidades base equivalen a una unidad de esta.
    factor_to_base: Mapped[Decimal] = mapped_column(unit_cost_numeric(), nullable=False)
    is_base: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("factor_to_base > 0", name="factor_positive"),
        CheckConstraint("dimension IN ('MASS', 'COUNT', 'VOLUME')", name="dimension_allowed"),
        CheckConstraint("NOT is_base OR factor_to_base = 1", name="base_factor_is_one"),
        Index("ix_units_of_measure_dimension", "dimension"),
    )


class Product(Base, TimestampMixin):
    """Producto, insumo, preparado o servicio del maestro."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Clave de negocio y clave de deduplicacion de la importacion.
    internal_reference: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    product_type: Mapped[ProductType] = mapped_column(StrEnumType(ProductType, 24), nullable=False)

    product_category_id: Mapped[int] = mapped_column(
        ForeignKey("product_categories.id", ondelete="RESTRICT"), nullable=False
    )
    pos_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("pos_categories.id", ondelete="RESTRICT")
    )

    #: Un servicio puede no tener unidad; el maestro real no la trae.
    base_uom_code: Mapped[str | None] = mapped_column(
        ForeignKey("units_of_measure.code", ondelete="RESTRICT")
    )
    purchase_uom_code: Mapped[str | None] = mapped_column(
        ForeignKey("units_of_measure.code", ondelete="RESTRICT")
    )

    #: Costo por unidad base. Escala fina: un insumo cuesta menos de un centimo
    #: por gramo y con dos decimales se redondearia a cero.
    cost: Mapped[Decimal | None] = mapped_column(unit_cost_numeric())
    sale_price: Mapped[Decimal | None] = mapped_column(money_numeric())
    sale_tax_rate: Mapped[Decimal | None] = mapped_column(percentage_numeric())
    purchase_tax_rate: Mapped[Decimal | None] = mapped_column(percentage_numeric())

    sellable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    purchasable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    available_in_pos: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    notes: Mapped[str | None] = mapped_column(Text)

    #: Dimensiones tecnicas para produccion y cotizador
    material: Mapped[str | None] = mapped_column(String(200))
    grammage: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    width: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    height: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    length: Mapped[Decimal | None] = mapped_column(quantity_numeric())
    depth: Mapped[Decimal | None] = mapped_column(quantity_numeric())

    __table_args__ = (
        CheckConstraint("length(btrim(internal_reference)) > 0", name="reference_not_blank"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint(
            "product_type IN ('RAW_MATERIAL', 'PREPARED_MATERIAL', 'FINISHED_PRODUCT', 'SERVICE')",
            name="product_type_allowed",
        ),
        CheckConstraint("cost IS NULL OR cost >= 0", name="cost_not_negative"),
        CheckConstraint("sale_price IS NULL OR sale_price >= 0", name="sale_price_not_negative"),
        Index("ix_products_name", "name"),
        Index("ix_products_category", "product_category_id"),
        Index("ix_products_type_active", "product_type", "active"),
    )


class Partner(Base, TimestampMixin):
    """Cliente, proveedor o ambos. Un unico maestro, no dos paralelos."""

    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[PartnerRole] = mapped_column(StrEnumType(PartnerRole, 16), nullable=False)

    document_type: Mapped[DocumentType | None] = mapped_column(StrEnumType(DocumentType, 16))
    #: Siempre texto: un DNI con cero inicial deja de ser un DNI si se guarda
    #: como numero.
    document_number: Mapped[str | None] = mapped_column(String(20))

    address: Mapped[str | None] = mapped_column(String(240))
    reference: Mapped[str | None] = mapped_column(String(240))
    ubigeo_code: Mapped[str | None] = mapped_column(
        ForeignKey("ubigeo_districts.code", ondelete="RESTRICT")
    )
    district: Mapped[str | None] = mapped_column(String(120))
    province: Mapped[str | None] = mapped_column(String(100))
    department: Mapped[str | None] = mapped_column(String(80))
    country: Mapped[str | None] = mapped_column(String(80))

    email: Mapped[str | None] = mapped_column(String(160))
    mobile: Mapped[str | None] = mapped_column(String(32))
    phone: Mapped[str | None] = mapped_column(String(32))

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("role IN ('CLIENT', 'SUPPLIER', 'BOTH')", name="role_allowed"),
        CheckConstraint(
            "document_type IS NULL OR document_type IN ('RUC', 'DNI', 'CE', 'PASSPORT', 'OTHER')",
            name="document_type_allowed",
        ),
        CheckConstraint(
            "(document_type IS NULL) = (document_number IS NULL)",
            name="document_pair_complete",
        ),
        # Parcial: los terceros sin documento no colisionan entre si.
        Index(
            "uq_partners_document",
            "document_type",
            "document_number",
            unique=True,
            postgresql_where=text("document_number IS NOT NULL"),
        ),
        Index("ix_partners_name", "name"),
        Index("ix_partners_role_active", "role", "active"),
    )
