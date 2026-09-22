"""Pruebas unitarias del motor geométrico puro del layout del horno (Fase 010M - M2).

Cubre:
1. Cálculo de footprint reservado:
   - Rotación 0° y 90°
   - Separación 0 y separación positiva
   - Rechazo de rotación inválida
2. Validación de niveles:
   - Nivel válido
   - z_cm negativo
   - usable_height_cm <= 0
   - z_cm + usable_height_cm > kiln_height
   - Solapamiento vertical entre niveles
   - Niveles adyacentes en frontera exacta (válido)
3. Validación de límites del placement:
   - Posición válida
   - x_cm negativo / y_cm negativo
   - x + reserved_x > kiln_width (y test de 0.000001 cm fuera)
   - y + reserved_y > kiln_depth (y test de 0.000001 cm fuera)
   - reserved_z > level.usable_height_cm (y test de 0.000001 cm fuera)
   - Exact fit en límites
   - Altura exacta
4. Detección de colisiones 2D:
   - Colisión provocada por separación (cajas reales separadas pero reservadas solapan)
   - Contacto exacto en bordes reservados (válido, no colisión)
   - Separación 0 con contacto borde con borde (válido)
   - Misma coordenada en niveles distintos (válido)
   - Misma coordenada en el mismo nivel (colisión)
5. Semántica de cantidad:
   - quantity == 1 (válido)
   - quantity != 1 (rechazado)
6. Aritmética Decimal pura sin float leakage
7. Medición de rendimiento (100 y 500 placements)
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from app.services.kiln_layout_geometry import (
    BoundingBox2D,
    LevelGeometry,
    PlacementGeometry,
    ReservedFootprint,
    check_collisions,
    get_reserved_footprint,
    placements_overlap,
    validate_layout_geometry,
    validate_level_geometry,
    validate_placement_bounds,
)

# ---------------------------------------------------------------------------
# 1. Footprint Reservado
# ---------------------------------------------------------------------------

def test_reserved_footprint_rotacion_0() -> None:
    """Pieza 20x10x8 con sep=3 y rot=0 -> 23x13x11."""
    fp = get_reserved_footprint(
        piece_length=Decimal("20"),
        piece_width=Decimal("10"),
        piece_height=Decimal("8"),
        separation=Decimal("3"),
        rotation_degrees=0,
    )
    assert fp.x_size == Decimal("23")
    assert fp.y_size == Decimal("13")
    assert fp.z_size == Decimal("11")


def test_reserved_footprint_rotacion_90() -> None:
    """Pieza 20x10x8 con sep=3 y rot=90 -> 13x23x11."""
    fp = get_reserved_footprint(
        piece_length=Decimal("20"),
        piece_width=Decimal("10"),
        piece_height=Decimal("8"),
        separation=Decimal("3"),
        rotation_degrees=90,
    )
    assert fp.x_size == Decimal("13")
    assert fp.y_size == Decimal("23")
    assert fp.z_size == Decimal("11")


def test_reserved_footprint_separacion_0() -> None:
    """Pieza 10x10x8 con sep=0 -> exactamente 10x10x8."""
    fp = get_reserved_footprint(
        piece_length=Decimal("10"),
        piece_width=Decimal("10"),
        piece_height=Decimal("8"),
        separation=Decimal("0"),
        rotation_degrees=0,
    )
    assert fp.x_size == Decimal("10")
    assert fp.y_size == Decimal("10")
    assert fp.z_size == Decimal("8")


def test_reserved_footprint_rotacion_invalida_falla() -> None:
    """Rotación distinta de 0 o 90 lanza ValueError."""
    with pytest.raises(ValueError, match="rotation_degrees debe ser 0 o 90"):
        get_reserved_footprint(
            piece_length=Decimal("10"),
            piece_width=Decimal("10"),
            piece_height=Decimal("8"),
            separation=Decimal("1"),
            rotation_degrees=45,
        )


# ---------------------------------------------------------------------------
# 2. Validación de Niveles
# ---------------------------------------------------------------------------

def test_niveles_validos_y_adyacentes() -> None:
    """Dos niveles adyacentes que tocan en la frontera (top_A == z_B) son válidos."""
    levels = [
        LevelGeometry(level_index=0, z_cm=Decimal("0"), usable_height_cm=Decimal("20")),
        LevelGeometry(level_index=1, z_cm=Decimal("20"), usable_height_cm=Decimal("25")),
    ]
    # Horno de altura 50 cm
    validate_level_geometry(levels, kiln_height=Decimal("50"))


def test_nivel_z_negativo_falla() -> None:
    """Nivel con z_cm negativo lanza LEVEL_OUT_OF_BOUNDS."""
    levels = [
        LevelGeometry(level_index=0, z_cm=Decimal("-1"), usable_height_cm=Decimal("20")),
    ]
    with pytest.raises(ValueError, match="LEVEL_OUT_OF_BOUNDS"):
        validate_level_geometry(levels, kiln_height=Decimal("50"))


def test_nivel_altura_cero_o_negativa_falla() -> None:
    """usable_height_cm <= 0 lanza LEVEL_OUT_OF_BOUNDS."""
    levels = [
        LevelGeometry(level_index=0, z_cm=Decimal("0"), usable_height_cm=Decimal("0")),
    ]
    with pytest.raises(ValueError, match="LEVEL_OUT_OF_BOUNDS"):
        validate_level_geometry(levels, kiln_height=Decimal("50"))


def test_nivel_excede_altura_horno_falla() -> None:
    """z + usable_height > kiln_height lanza LEVEL_OUT_OF_BOUNDS."""
    levels = [
        LevelGeometry(level_index=0, z_cm=Decimal("30"), usable_height_cm=Decimal("25")),
    ]
    with pytest.raises(ValueError, match="LEVEL_OUT_OF_BOUNDS"):
        validate_level_geometry(levels, kiln_height=Decimal("50"))


def test_niveles_solapados_verticalmente_falla() -> None:
    """Dos niveles con solapamiento en Z lanzan LEVEL_OVERLAP."""
    levels = [
        LevelGeometry(level_index=0, z_cm=Decimal("0"), usable_height_cm=Decimal("25")),
        LevelGeometry(level_index=1, z_cm=Decimal("20"), usable_height_cm=Decimal("25")),
    ]
    with pytest.raises(ValueError, match="LEVEL_OVERLAP"):
        validate_level_geometry(levels, kiln_height=Decimal("60"))


# ---------------------------------------------------------------------------
# 3. Límites del Placement (Bounds & Height)
# ---------------------------------------------------------------------------

def test_placement_exact_fit_en_limites() -> None:
    """Pieza reservada 20x20 en horno 100x100 ubicada en (80, 80) pasa exactamente."""
    fp = ReservedFootprint(x_size=Decimal("20"), y_size=Decimal("20"), z_size=Decimal("15"))
    box = validate_placement_bounds(
        placement_index=0,
        assignment_id=1,
        level_index=0,
        footprint=fp,
        x_cm=Decimal("80"),
        y_cm=Decimal("80"),
        kiln_width=Decimal("100"),
        kiln_depth=Decimal("100"),
        level_usable_height=Decimal("15"),
    )
    assert box.right == Decimal("100")
    assert box.top == Decimal("100")


def test_placement_x_fuera_por_un_microcentimetro_falla() -> None:
    """x = 80.000001 para pieza de 20 en horno de 100 lanza OUT_OF_BOUNDS sin epsilon flotante."""
    fp = ReservedFootprint(x_size=Decimal("20"), y_size=Decimal("20"), z_size=Decimal("15"))
    with pytest.raises(ValueError, match="OUT_OF_BOUNDS"):
        validate_placement_bounds(
            placement_index=0,
            assignment_id=1,
            level_index=0,
            footprint=fp,
            x_cm=Decimal("80.000001"),
            y_cm=Decimal("80"),
            kiln_width=Decimal("100"),
            kiln_depth=Decimal("100"),
            level_usable_height=Decimal("20"),
        )


def test_placement_y_fuera_por_un_microcentimetro_falla() -> None:
    """y = 80.000001 para pieza de 20 en horno de 100 lanza OUT_OF_BOUNDS."""
    fp = ReservedFootprint(x_size=Decimal("20"), y_size=Decimal("20"), z_size=Decimal("15"))
    with pytest.raises(ValueError, match="OUT_OF_BOUNDS"):
        validate_placement_bounds(
            placement_index=0,
            assignment_id=1,
            level_index=0,
            footprint=fp,
            x_cm=Decimal("50"),
            y_cm=Decimal("80.000001"),
            kiln_width=Decimal("100"),
            kiln_depth=Decimal("100"),
            level_usable_height=Decimal("20"),
        )


def test_placement_coordenadas_negativas_fallan() -> None:
    """x o y negativos lanzan OUT_OF_BOUNDS."""
    fp = ReservedFootprint(x_size=Decimal("10"), y_size=Decimal("10"), z_size=Decimal("10"))
    with pytest.raises(ValueError, match="OUT_OF_BOUNDS"):
        validate_placement_bounds(
            placement_index=0,
            assignment_id=1,
            level_index=0,
            footprint=fp,
            x_cm=Decimal("-0.000001"),
            y_cm=Decimal("0"),
            kiln_width=Decimal("100"),
            kiln_depth=Decimal("100"),
            level_usable_height=Decimal("20"),
        )


def test_placement_altura_exacta_pasa() -> None:
    """reserved_height == usable_height pasa."""
    fp = ReservedFootprint(x_size=Decimal("10"), y_size=Decimal("10"), z_size=Decimal("25.000000"))
    validate_placement_bounds(
        placement_index=0,
        assignment_id=1,
        level_index=0,
        footprint=fp,
        x_cm=Decimal("0"),
        y_cm=Decimal("0"),
        kiln_width=Decimal("100"),
        kiln_depth=Decimal("100"),
        level_usable_height=Decimal("25.000000"),
    )


def test_placement_altura_excedida_por_un_microcentimetro_falla() -> None:
    """reserved_height = 25.000001 en nivel de 25 lanza HEIGHT_EXCEEDED."""
    fp = ReservedFootprint(x_size=Decimal("10"), y_size=Decimal("10"), z_size=Decimal("25.000001"))
    with pytest.raises(ValueError, match="HEIGHT_EXCEEDED"):
        validate_placement_bounds(
            placement_index=0,
            assignment_id=1,
            level_index=0,
            footprint=fp,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            kiln_width=Decimal("100"),
            kiln_depth=Decimal("100"),
            level_usable_height=Decimal("25.000000"),
        )


# ---------------------------------------------------------------------------
# 4. Detección de Colisiones 2D
# ---------------------------------------------------------------------------

def test_colision_por_separacion_sin_toque_fisico_directo() -> None:
    """Piezas físicas 10x10 con separación 2 cm.
    A en (0, 0) -> reservado [0..12, 0..12].
    B en (11, 0) -> reservado [11..23, 0..12].
    Las piezas físicas no se tocan (A está en 0..10 y B en 11..21),
    pero sus áreas reservadas se superponen en X [11..12].
    Debe detectar COLLISION.
    """
    box_a = BoundingBox2D(
        placement_index=0,
        batch_assignment_id=1,
        level_index=0,
        left=Decimal("0"),
        right=Decimal("12"),
        bottom=Decimal("0"),
        top=Decimal("12"),
        height=Decimal("10"),
    )
    box_b = BoundingBox2D(
        placement_index=1,
        batch_assignment_id=1,
        level_index=0,
        left=Decimal("11"),
        right=Decimal("23"),
        bottom=Decimal("0"),
        top=Decimal("12"),
        height=Decimal("10"),
    )
    assert placements_overlap(box_a, box_b) is True
    with pytest.raises(ValueError, match="COLLISION"):
        check_collisions([box_a, box_b])


def test_contacto_exacto_en_bordes_reservados_pasa() -> None:
    """A.right == B.left: contacto exacto en frontera reservada es VÁLIDO."""
    box_a = BoundingBox2D(
        placement_index=0,
        batch_assignment_id=1,
        level_index=0,
        left=Decimal("0"),
        right=Decimal("20"),
        bottom=Decimal("0"),
        top=Decimal("20"),
        height=Decimal("10"),
    )
    box_b = BoundingBox2D(
        placement_index=1,
        batch_assignment_id=1,
        level_index=0,
        left=Decimal("20"),
        right=Decimal("40"),
        bottom=Decimal("0"),
        top=Decimal("20"),
        height=Decimal("10"),
    )
    assert placements_overlap(box_a, box_b) is False
    check_collisions([box_a, box_b])


def test_misma_coordenada_en_distintos_niveles_pasa() -> None:
    """Dos piezas en x=10, y=10 pero en niveles 0 y 1 NO colisionan."""
    box_a = BoundingBox2D(
        placement_index=0,
        batch_assignment_id=1,
        level_index=0,
        left=Decimal("10"),
        right=Decimal("20"),
        bottom=Decimal("10"),
        top=Decimal("20"),
        height=Decimal("10"),
    )
    box_b = BoundingBox2D(
        placement_index=1,
        batch_assignment_id=2,
        level_index=1,
        left=Decimal("10"),
        right=Decimal("20"),
        bottom=Decimal("10"),
        top=Decimal("20"),
        height=Decimal("10"),
    )
    check_collisions([box_a, box_b])


def test_misma_coordenada_mismo_nivel_colisiona() -> None:
    """Dos piezas en x=10, y=10 en el mismo nivel colisionan."""
    box_a = BoundingBox2D(
        placement_index=0,
        batch_assignment_id=1,
        level_index=0,
        left=Decimal("10"),
        right=Decimal("20"),
        bottom=Decimal("10"),
        top=Decimal("20"),
        height=Decimal("10"),
    )
    box_b = BoundingBox2D(
        placement_index=1,
        batch_assignment_id=2,
        level_index=0,
        left=Decimal("10"),
        right=Decimal("20"),
        bottom=Decimal("10"),
        top=Decimal("20"),
        height=Decimal("10"),
    )
    with pytest.raises(ValueError, match="COLLISION"):
        check_collisions([box_a, box_b])


# ---------------------------------------------------------------------------
# 5. Semántica de Cantidad Física
# ---------------------------------------------------------------------------

def test_placement_quantity_distinto_de_1_falla() -> None:
    """placement.quantity != 1 lanza PHYSICAL_QUANTITY_INVALID."""
    levels = [LevelGeometry(level_index=0, z_cm=Decimal("0"), usable_height_cm=Decimal("30"))]
    placements = [
        PlacementGeometry(
            index=0,
            batch_assignment_id=1,
            quantity=5,
            level_index=0,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("10"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("1"),
        )
    ]
    with pytest.raises(ValueError, match="PHYSICAL_QUANTITY_INVALID"):
        validate_layout_geometry(
            kiln_width=Decimal("60"),
            kiln_depth=Decimal("60"),
            kiln_height=Decimal("60"),
            levels=levels,
            placements=placements,
        )


def test_placement_nivel_inexistente_falla() -> None:
    """Placement con level_index no configurado en levels lanza LEVEL_NOT_FOUND."""
    levels = [LevelGeometry(level_index=0, z_cm=Decimal("0"), usable_height_cm=Decimal("30"))]
    placements = [
        PlacementGeometry(
            index=0,
            batch_assignment_id=1,
            quantity=1,
            level_index=99,
            x_cm=Decimal("0"),
            y_cm=Decimal("0"),
            rotation_degrees=0,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("10"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("1"),
        )
    ]
    with pytest.raises(ValueError, match="LEVEL_NOT_FOUND"):
        validate_layout_geometry(
            kiln_width=Decimal("60"),
            kiln_depth=Decimal("60"),
            kiln_height=Decimal("60"),
            levels=levels,
            placements=placements,
        )


# ---------------------------------------------------------------------------
# 6. Precisión Decimal Pura
# ---------------------------------------------------------------------------

def test_precision_decimal_sin_float_leakage() -> None:
    """Valores con decimales arbitrarios (10.333333, 3.125000) mantienen precisión exacta."""
    fp = get_reserved_footprint(
        piece_length=Decimal("10.333333"),
        piece_width=Decimal("8.125000"),
        piece_height=Decimal("5.500000"),
        separation=Decimal("1.250000"),
        rotation_degrees=0,
    )
    assert fp.x_size == Decimal("11.583333")
    assert fp.y_size == Decimal("9.375000")
    assert fp.z_size == Decimal("6.750000")

    box = validate_placement_bounds(
        placement_index=0,
        assignment_id=1,
        level_index=0,
        footprint=fp,
        x_cm=Decimal("2.416667"),
        y_cm=Decimal("1.625000"),
        kiln_width=Decimal("14.000000"),
        kiln_depth=Decimal("11.000000"),
        level_usable_height=Decimal("10.000000"),
    )
    # 2.416667 + 11.583333 = 14.000000 exacto
    assert box.right == Decimal("14.000000")
    # 1.625000 + 9.375000 = 11.000000 exacto
    assert box.top == Decimal("11.000000")


# ---------------------------------------------------------------------------
# 7. Medición de Rendimiento (100 y 500 Placements)
# ---------------------------------------------------------------------------

def test_rendimiento_100_placements() -> None:
    """Valida 100 placements sin colisión en una cuadrícula 10x10.
    Debe completar la validación completa en menos de 50 ms.
    """
    levels = [LevelGeometry(level_index=0, z_cm=Decimal("0"), usable_height_cm=Decimal("50"))]
    placements: list[PlacementGeometry] = []
    # Cuadrícula 10x10: piezas de 5x5 con sep 1 -> reserved 6x6.
    # En un horno de 70x70 cm.
    idx = 0
    for r in range(10):
        for c in range(10):
            placements.append(
                PlacementGeometry(
                    index=idx,
                    batch_assignment_id=1,
                    quantity=1,
                    level_index=0,
                    x_cm=Decimal(c * 6),
                    y_cm=Decimal(r * 6),
                    rotation_degrees=0,
                    piece_length_cm=Decimal("5"),
                    piece_width_cm=Decimal("5"),
                    piece_height_cm=Decimal("10"),
                    separation_cm=Decimal("1"),
                )
            )
            idx += 1

    t0 = time.perf_counter()
    boxes = validate_layout_geometry(
        kiln_width=Decimal("70"),
        kiln_depth=Decimal("70"),
        kiln_height=Decimal("50"),
        levels=levels,
        placements=placements,
    )
    elapsed = time.perf_counter() - t0

    assert len(boxes) == 100
    # Rendimiento: 100 placements debe tomar < 0.05 segundos
    assert elapsed < 0.05, f"Validación de 100 placements tomó {elapsed:.4f}s (> 0.05s)"


def test_rendimiento_500_placements() -> None:
    """Valida 500 placements en 5 niveles (100 por nivel en cuadrícula 10x10).
    Mide y registra la duración, asegurando que no haya degradación catastrófica (< 0.5s).
    """
    levels = [
        LevelGeometry(level_index=i, z_cm=Decimal(i * 15), usable_height_cm=Decimal("15"))
        for i in range(5)
    ]
    placements: list[PlacementGeometry] = []
    idx = 0
    for lvl in range(5):
        for r in range(10):
            for c in range(10):
                placements.append(
                    PlacementGeometry(
                        index=idx,
                        batch_assignment_id=lvl + 1,
                        quantity=1,
                        level_index=lvl,
                        x_cm=Decimal(c * 6),
                        y_cm=Decimal(r * 6),
                        rotation_degrees=0,
                        piece_length_cm=Decimal("5"),
                        piece_width_cm=Decimal("5"),
                        piece_height_cm=Decimal("10"),
                        separation_cm=Decimal("1"),
                    )
                )
                idx += 1

    t0 = time.perf_counter()
    boxes = validate_layout_geometry(
        kiln_width=Decimal("70"),
        kiln_depth=Decimal("70"),
        kiln_height=Decimal("80"),
        levels=levels,
        placements=placements,
    )
    elapsed = time.perf_counter() - t0

    assert len(boxes) == 500
    # Rendimiento: 500 placements en 5 niveles debe tomar < 0.5 segundos
    assert elapsed < 0.50, f"Validación de 500 placements tomó {elapsed:.4f}s (> 0.5s)"
