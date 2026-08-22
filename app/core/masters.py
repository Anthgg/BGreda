"""Reglas de normalizacion de los maestros.

Viven aparte del importador porque no son "reglas del Excel": son las reglas
del dominio. La API de alta manual aplica exactamente las mismas, de modo que
un producto creado a mano y uno importado no pueden acabar con formatos
distintos.

Todo lo numerico pasa por ``Decimal``. No hay un solo ``float`` en el camino de
costos, precios ni cantidades.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.core.precision import UNIT_COST_SCALE
from app.models.masters import GRAM, KILOGRAM, UNIT, UOM_ALIASES, DocumentType, ProductType

# ---------------------------------------------------------------------------
# Codigos de aviso y error del importador
# ---------------------------------------------------------------------------
WARN_ROUNDED_COST = "ROUNDED_TO_12_DECIMALS"
WARN_VARIABLE_PRICE_ZERO = "VARIABLE_PRICE_ZERO"
WARN_LOCATION_MISMATCH = "SOURCE_LOCATION_MISMATCH"
WARN_CATEGORY_MISMATCH = "POSSIBLE_CATEGORY_MISMATCH"
WARN_NORMALIZED_LOCATION = "NORMALIZED_LOCATION"
WARN_DOCUMENT_FORMAT_LOST = "DOCUMENT_FORMAT_LOST"
WARN_DUPLICATE_NAME = "DUPLICATE_NAME"
WARN_RECIPE_DEFERRED = "RECIPE_DEFERRED_TO_PHASE_3_5"
WARN_SOURCE_UOM_MISSING = "SOURCE_UOM_MISSING"

ERR_UNRESOLVED_PRODUCT = "UNRESOLVED_PRODUCT"
ERR_AMBIGUOUS_PRODUCT = "AMBIGUOUS_PRODUCT"
ERR_UNKNOWN_UOM = "UNKNOWN_UOM"
ERR_UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"
ERR_INVALID_DECIMAL = "INVALID_DECIMAL"
ERR_DUPLICATE_REFERENCE = "DUPLICATE_INTERNAL_REFERENCE"
ERR_CONFLICTING_REFERENCE = "CONFLICTING_INTERNAL_REFERENCE"
ERR_DUPLICATE_DOCUMENT = "DUPLICATE_DOCUMENT"
ERR_UBIGEO_NOT_FOUND = "UBIGEO_NOT_FOUND"
ERR_UBIGEO_AMBIGUOUS = "UBIGEO_AMBIGUOUS"
ERR_MISSING_ROLE = "PARTNER_ROLE_NOT_CLASSIFIED"
ERR_NEGATIVE_STOCK = "NEGATIVE_STOCK"
ERR_MISSING_REQUIRED = "MISSING_REQUIRED_VALUE"

#: Estado de importacion, no rol del maestro: ningun tercero se guarda asi.
ROLE_PENDING = "PENDING_CLASSIFICATION"

#: Marca de quien decidio un valor que el archivo no traia.
RESOLUTION_USER = "USER_DECISION"

#: Raiz de la categoria -> tipo funcional. Se deriva de la categoria, nunca de
#: adivinar por el nombre del producto.
PRODUCT_TYPE_BY_ROOT: dict[str, ProductType] = {
    "INSUMOS TALLER": ProductType.RAW_MATERIAL,
    "PRODUCTOS TERMINADOS TALLER / ESMALTES": ProductType.PREPARED_MATERIAL,
    "PRODUCTOS TERMINADOS TALLER / ARTESANIAS GREDA": ProductType.FINISHED_PRODUCT,
    "SERVICIOS": ProductType.SERVICE,
}

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
_TRAILING_FLOAT_ZERO = re.compile(r"^(-?\d+)\.0$")


def fold(value: object) -> str:
    """Clave de comparacion: sin acentos, sin dobles espacios, en mayusculas.

    Sirve para detectar que "Óxido de estaño" y "OXIDO DE ESTANO" son lo mismo
    sin fusionar cosas que solo se parecen.
    """
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.upper().split())


def parse_decimal(value: object) -> Decimal:
    """Convierte a ``Decimal`` un valor que puede venir como texto o numero.

    El maestro guarda los precios como texto con formato local (``'3,500.00'``)
    y los costos como numero. Se acepta ambos y se elimina unicamente el
    separador de millares, nunca el separador decimal.

    Lanza ``ValueError`` si el valor no es un numero: el importador lo traduce
    a ``INVALID_DECIMAL`` en la fila correspondiente.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("Un booleano no es una cantidad")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # repr() de float ya es la representacion decimal mas corta que
        # round-trip; Decimal(float) arrastraria el error binario completo.
        return Decimal(repr(value))
    text = str(value).strip()
    if not text:
        raise ValueError("Valor vacio")
    text = _THOUSANDS.sub("", text).replace(" ", "")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"No es un numero valido: {text!r}") from exc


def quantize_cost(value: Decimal) -> tuple[Decimal, bool]:
    """Ajusta un costo a la escala aprobada del proyecto.

    Devuelve el valor con doce decimales y si hubo redondeo. Los costos del
    maestro traen hasta dieciseis decimales porque son divisiones en coma
    flotante de Excel, no precision real; se redondea a la escala del proyecto
    con ``ROUND_HALF_UP`` y se avisa fila por fila.
    """
    quantum = Decimal(1).scaleb(-UNIT_COST_SCALE)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return quantized, quantized != value


def normalize_uom(literal: object) -> str | None:
    """Traduce la grafia del archivo al codigo canonico de unidad.

    ``gr`` y ``g`` son la misma unidad; ``Unidad`` es ``unit``. Un literal
    desconocido devuelve ``None`` para que el importador lo marque en vez de
    inventar una unidad.
    """
    if literal is None:
        return None
    key = str(literal).strip().lower()
    if not key:
        return None
    return UOM_ALIASES.get(key)


def convert_quantity(value: Decimal, factor_from: Decimal, factor_to: Decimal) -> Decimal:
    """Convierte entre unidades de la misma dimension con exactitud decimal.

    kg -> g da exactamente 1000, no 999.9999999999999.
    """
    if factor_to <= 0:
        raise ValueError("El factor de destino debe ser positivo")
    return (value * factor_from) / factor_to


def strip_excel_float(text: str) -> str:
    """Quita el ``.0`` que Excel anade al guardar un documento como numero."""
    match = _TRAILING_FLOAT_ZERO.match(text)
    return match.group(1) if match else text


def normalize_document(document_type: object, raw: object) -> tuple[str, str | None, bool]:
    """Normaliza un documento de identidad.

    Devuelve ``(valor, sugerencia, requiere_revision)``.

    No se corrige nada por cuenta propia. Si Excel destruyo el formato —un DNI
    de ocho digitos guardado como numero pierde el cero inicial y llega con
    siete— se propone el valor probable pero la fila queda en revision: un
    documento real no se altera sin que una persona lo confirme.
    """
    text = strip_excel_float(str(raw).strip()).replace(" ", "").replace("-", "").upper()
    if not text:
        return "", None, True

    expected = {DocumentType.RUC: 11, DocumentType.DNI: 8, DocumentType.CE: 9}
    try:
        kind = DocumentType(str(document_type).strip().upper())
    except ValueError:
        return text, None, True

    length = expected.get(kind)
    if length is None or len(text) == length:
        return text, None, False
    # Solo el DNI tiene el caso conocido del cero inicial que Excel se come.
    # Rellenar un CE o un pasaporte seria inventarse un documento.
    if kind is DocumentType.DNI and len(text) < length and text.isdigit():
        return text, text.zfill(length), True
    return text, None, True


def normalize_location(name: object) -> tuple[str, bool]:
    """Nombre canonico de ubicacion.

    Devuelve ``(nombre, se_normalizo)``. Los alias son una lista explicita y
    corta: no se fusionan ubicaciones por parecido tipografico.
    """
    raw = " ".join(str(name).split())
    aliases = {
        "MARIANO PASTOR": "Mariano Pastor",
        "MARINO PASTOR": "Mariano Pastor",
    }
    canonical = aliases.get(fold(raw))
    if canonical is None:
        return raw, False
    return canonical, canonical != raw


def product_type_for_path(display_path: str) -> ProductType | None:
    """Tipo funcional a partir de la ruta de categoria, de la mas especifica."""
    key = fold(display_path)
    for prefix, product_type in sorted(
        PRODUCT_TYPE_BY_ROOT.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if key == prefix or key.startswith(prefix + " /"):
            return product_type
    return None


def parse_boolean(value: object) -> bool | None:
    """``Si``/``No`` del maestro a booleano."""
    if isinstance(value, bool):
        return value
    key = fold(value)
    if key in {"SI", "S", "TRUE", "1", "YES"}:
        return True
    if key in {"NO", "N", "FALSE", "0"}:
        return False
    return None


__all__ = [
    "GRAM",
    "KILOGRAM",
    "PRODUCT_TYPE_BY_ROOT",
    "ROLE_PENDING",
    "UNIT",
    "convert_quantity",
    "fold",
    "normalize_document",
    "normalize_location",
    "normalize_uom",
    "parse_boolean",
    "parse_decimal",
    "product_type_for_path",
    "quantize_cost",
    "strip_excel_float",
]
