"""Reglas de normalizacion de los maestros.

Son las decisiones aprobadas para Fase 3, congeladas como pruebas: si alguien
cambia el redondeo del costo o decide "arreglar" un DNI automaticamente, esto
falla.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.masters import (
    convert_quantity,
    fold,
    normalize_document,
    normalize_location,
    normalize_uom,
    parse_boolean,
    parse_decimal,
    product_type_for_path,
    quantize_cost,
)
from app.models.masters import ProductType


class TestParseDecimal:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("320.00", Decimal("320.00")),
            ("3,500.00", Decimal("3500.00")),
            (Decimal("1.5"), Decimal("1.5")),
            (42, Decimal(42)),
        ],
    )
    def test_acepta_texto_y_numeros(self, value: object, expected: Decimal) -> None:
        assert parse_decimal(value) == expected

    def test_un_float_no_arrastra_el_error_binario(self) -> None:
        # Decimal(0.1) daria 0.1000000000000000055511151231257827021181583404541015625.
        assert parse_decimal(0.1) == Decimal("0.1")

    def test_no_toca_el_separador_decimal(self) -> None:
        assert parse_decimal("1,234.56") == Decimal("1234.56")

    @pytest.mark.parametrize("value", ["", "  ", "no soy un numero", True])
    def test_rechaza_lo_que_no_es_numero(self, value: object) -> None:
        with pytest.raises(ValueError):
            parse_decimal(value)


class TestQuantizeCost:
    def test_redondea_a_doce_decimales_y_avisa(self) -> None:
        value, rounded = quantize_cost(Decimal("0.0169068431372549"))
        assert value == Decimal("0.016906843137")
        assert rounded is True

    def test_un_costo_que_ya_cabe_no_se_marca(self) -> None:
        value, rounded = quantize_cost(Decimal("0.002541200"))
        assert value == Decimal("0.002541200")
        assert rounded is False

    def test_usa_half_up_y_no_bankers_rounding(self) -> None:
        value, _ = quantize_cost(Decimal("0.0000000000005"))
        assert value == Decimal("0.000000000001")

    def test_conserva_costos_por_debajo_del_centimo(self) -> None:
        """Un insumo cuesta menos de S/ 0.01 por gramo: no puede irse a cero."""
        value, _ = quantize_cost(Decimal("0.0007564"))
        assert value > 0


class TestUnidades:
    @pytest.mark.parametrize("literal", ["g", "gr", "GR", " Gramos "])
    def test_gramo_y_sus_alias(self, literal: str) -> None:
        assert normalize_uom(literal) == "g"

    def test_unidad_discreta(self) -> None:
        assert normalize_uom("Unidad") == "unit"

    def test_una_unidad_desconocida_no_se_inventa(self) -> None:
        assert normalize_uom("cucharadas") is None

    def test_kilo_a_gramo_es_exacto(self) -> None:
        result = convert_quantity(Decimal("2.5"), Decimal(1000), Decimal(1))
        assert result == Decimal(2500)

    def test_gramo_a_kilo_es_exacto(self) -> None:
        result = convert_quantity(Decimal(2500), Decimal(1), Decimal(1000))
        assert result == Decimal("2.5")


class TestDocumentos:
    def test_un_ruc_completo_pasa_sin_revision(self) -> None:
        value, suggestion, review = normalize_document("RUC", "20101194991")
        assert (value, suggestion, review) == ("20101194991", None, False)

    def test_excel_convirtio_el_dni_en_numero(self) -> None:
        value, suggestion, review = normalize_document("DNI", "40903769.0")
        assert value == "40903769"
        assert suggestion is None
        assert review is False

    def test_un_dni_de_siete_digitos_se_sugiere_pero_no_se_corrige(self) -> None:
        value, suggestion, review = normalize_document("DNI", 1234567)
        assert value == "1234567"
        assert suggestion == "01234567"
        assert review is True

    def test_un_documento_de_tipo_desconocido_queda_en_revision(self) -> None:
        _, _, review = normalize_document("PARTIDA", "123")
        assert review is True


class TestUbicaciones:
    def test_el_alias_aprobado_se_normaliza(self) -> None:
        assert normalize_location("Marino pastor") == ("Mariano Pastor", True)

    def test_el_nombre_canonico_no_se_marca(self) -> None:
        assert normalize_location("Mariano Pastor") == ("Mariano Pastor", False)

    def test_una_ubicacion_ajena_no_se_fusiona(self) -> None:
        assert normalize_location("Tienda barranco") == ("Tienda barranco", False)


class TestTipoDeProducto:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("Insumos Taller / Pastas", ProductType.RAW_MATERIAL),
            (
                "Productos terminados Taller / Esmaltes / Bases y neutros",
                ProductType.PREPARED_MATERIAL,
            ),
            (
                "Productos terminados Taller / Artesanias Greda",
                ProductType.FINISHED_PRODUCT,
            ),
            ("Servicios / Clases / Adultos", ProductType.SERVICE),
        ],
    )
    def test_se_deriva_de_la_categoria(self, path: str, expected: ProductType) -> None:
        assert product_type_for_path(path) == expected

    def test_una_categoria_desconocida_no_adivina_tipo(self) -> None:
        assert product_type_for_path("Otra cosa / Rara") is None

    def test_no_se_deduce_del_nombre_del_producto(self) -> None:
        """El nombre puede decir 'esmalte' y la categoria mandar igualmente."""
        assert product_type_for_path("Insumos Taller / Vidrios") is ProductType.RAW_MATERIAL


class TestVarios:
    def test_fold_ignora_tildes_y_mayusculas(self) -> None:
        assert fold("Óxido de estaño") == fold("OXIDO DE ESTANO")

    @pytest.mark.parametrize(("value", "expected"), [("Si", True), ("No", False), ("", None)])
    def test_booleanos_del_maestro(self, value: str, expected: bool | None) -> None:
        assert parse_boolean(value) is expected
