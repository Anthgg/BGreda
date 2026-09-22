"""Pruebas unitarias para el motor de auto-packing determinista (Fase 010M - M3).

Verifica:
1. Determinismo absoluto (mismo input -> mismo output).
2. Casos vacíos (sin piezas, sin niveles).
3. Caso simple (1 pieza en origen).
4. Múltiples piezas sin solapamiento (verificado con motor M2).
5. Múltiples órdenes combinadas (multi-order).
6. Pieza que requiere rotación 90° para encajar.
7. Pieza que excede altura o área (unplaced).
8. Placements existentes respetados como obstáculos fijos.
9. Multi-nivel (nivel 0 lleno -> nivel 1 utilizado).
10. Independencia del orden de entrada (sorting estable).
11. Rendimiento para 100 y 500 piezas.
"""

from __future__ import annotations

import time
from decimal import Decimal

from app.services.kiln_layout_geometry import (
    BoundingBox2D,
    LevelGeometry,
    check_collisions,
    get_reserved_footprint,
)
from app.services.kiln_layout_packing import (
    PieceToPack,
    suggest_layout_packing,
)


def _default_level(
    level_index: int = 0,
    z_cm: Decimal = Decimal("0"),
    usable_height_cm: Decimal = Decimal("30"),
) -> LevelGeometry:
    return LevelGeometry(level_index=level_index, z_cm=z_cm, usable_height_cm=usable_height_cm)


def test_packing_sin_piezas_pendientes_vacio() -> None:
    """Sin piezas pendientes el resultado tiene count=0 y niveles_used vacíos."""
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=[],
    )
    assert result.total_pending == 0
    assert result.suggested_count == 0
    assert result.unplaced_count == 0
    assert result.levels_used == []
    assert len(result.suggested_placements) == 0
    assert len(result.unplaced_pieces) == 0


def test_packing_sin_niveles_todas_unplaced() -> None:
    """Si no hay niveles definidos, todas las piezas resultan unplaced con NO_LEVELS."""
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("10"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("0"),
        )
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.total_pending == 1
    assert result.suggested_count == 0
    assert result.unplaced_count == 1
    assert result.unplaced_pieces[0].reason == "NO_LEVELS"


def test_packing_simple_una_pieza() -> None:
    """Una pieza en un horno vacío se coloca en el origen (0, 0) con rotación 0."""
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("8"),
            piece_height_cm=Decimal("12"),
            separation_cm=Decimal("1"),
        )
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 1
    assert result.unplaced_count == 0
    p = result.suggested_placements[0]
    assert p.x_cm == Decimal("0")
    assert p.y_cm == Decimal("0")
    assert p.rotation_degrees == 0
    assert p.level_index == 0
    assert p.batch_assignment_id == 1
    assert result.levels_used == [0]


def test_packing_determinismo_estricto() -> None:
    """Ejecutar el empaquetador N veces con el mismo input produce idéntico output."""
    pieces = [
        PieceToPack(
            batch_assignment_id=i,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal(f"{5 + (i % 7)}"),
            piece_width_cm=Decimal(f"{4 + (i % 5)}"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("1"),
        )
        for i in range(1, 15)
    ]
    levels = [
        _default_level(0),
        _default_level(1, z_cm=Decimal("30"), usable_height_cm=Decimal("30")),
    ]

    ref = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("70"),
        levels=levels,
        existing_boxes=[],
        pieces_to_pack=pieces,
    )

    for _ in range(10):
        run = suggest_layout_packing(
            kiln_width=Decimal("60"),
            kiln_depth=Decimal("50"),
            kiln_height=Decimal("70"),
            levels=levels,
            existing_boxes=[],
            pieces_to_pack=pieces,
        )
        assert run.suggested_count == ref.suggested_count
        assert run.unplaced_count == ref.unplaced_count
        assert run.levels_used == ref.levels_used
        for a, b in zip(run.suggested_placements, ref.suggested_placements, strict=True):
            assert a == b


def test_packing_multiples_piezas_sin_colisiones() -> None:
    """Múltiples piezas empaquetadas no colisionan entre sí (validado con M2 check_collisions)."""
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=i,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("10"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("2"),
        )
        for i in range(1, 13)
    ]
    # Horno de 60x50. Piezas de 10x10 con sep 2 -> reservado 12x12
    # Capacidad teórica por nivel: 60//12 = 5 en X, 50//12 = 4 en Y -> 20 piezas
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 12
    assert result.unplaced_count == 0

    # Construir BoundingBox2D y verificar con M2 check_collisions
    boxes = []
    for idx, p in enumerate(result.suggested_placements):
        fp = get_reserved_footprint(
            piece_length=p.piece_length_cm_snapshot,
            piece_width=p.piece_width_cm_snapshot,
            piece_height=p.piece_height_cm_snapshot,
            separation=p.separation_cm_snapshot,
            rotation_degrees=p.rotation_degrees,
        )
        boxes.append(
            BoundingBox2D(
                placement_index=idx,
                batch_assignment_id=p.batch_assignment_id,
                level_index=p.level_index,
                left=p.x_cm,
                right=p.x_cm + fp.x_size,
                bottom=p.y_cm,
                top=p.y_cm + fp.y_size,
                height=fp.z_size,
            )
        )
    # No debe lanzar ValueError de colisión
    check_collisions(boxes)


def test_packing_multiorder_distintas_ordenes() -> None:
    """Piezas de diferentes asignaciones se combinan físicamente sin conflicto."""
    pieces = [
        PieceToPack(
            batch_assignment_id=101,  # Orden A
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("15"),
            piece_width_cm=Decimal("15"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("1"),
        ),
        PieceToPack(
            batch_assignment_id=202,  # Orden B
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("10"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("1"),
        ),
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 2
    assignments_suggested = {p.batch_assignment_id for p in result.suggested_placements}
    assert assignments_suggested == {101, 202}


def test_packing_rotacion_requerida_90() -> None:
    """Pieza con dimensiones que solo caben rotadas a 90° se coloca en 90°."""
    # Horno: ancho 30, prof 50
    # Pieza: largo 45, ancho 20, sep 0
    # A rotación 0: x=45 > 30 (no cabe)
    # A rotación 90: x=20 <= 30, y=45 <= 50 (cabe)
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("45"),
            piece_width_cm=Decimal("20"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("0"),
        )
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("30"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 1
    p = result.suggested_placements[0]
    assert p.rotation_degrees == 90
    assert p.x_cm == Decimal("0")
    assert p.y_cm == Decimal("0")


def test_packing_pieza_excede_altura_unplaced() -> None:
    """Pieza cuya altura supera la altura útil de todos los niveles queda unplaced."""
    # Nivel con usable_height = 20
    # Pieza con altura 25 + sep 1 = 26 > 20
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("10"),
            piece_height_cm=Decimal("25"),
            separation_cm=Decimal("1"),
        )
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level(usable_height_cm=Decimal("20"))],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 0
    assert result.unplaced_count == 1
    assert result.unplaced_pieces[0].reason == "HEIGHT_EXCEEDED"


def test_packing_pieza_imposible_por_espacio_unplaced() -> None:
    """Pieza que excede ancho y profundidad del horno no cabe y queda unplaced."""
    # Horno: 60x50. Pieza: 80x80
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("80"),
            piece_width_cm=Decimal("80"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("0"),
        )
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 0
    assert result.unplaced_count == 1
    assert result.unplaced_pieces[0].reason == "NO_VALID_POSITION"


def test_packing_respeta_placements_existentes_como_obstaculos() -> None:
    """Placements existentes son obstáculos fijos: las nuevas piezas no colisionan con ellos."""
    # Obstáculo existente en [0..20, 0..20] en nivel 0
    existing = [
        BoundingBox2D(
            placement_index=1,
            batch_assignment_id=99,
            level_index=0,
            left=Decimal("0"),
            right=Decimal("20"),
            bottom=Decimal("0"),
            top=Decimal("20"),
            height=Decimal("10"),
        )
    ]
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("10"),
            piece_width_cm=Decimal("10"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("0"),
        )
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=existing,
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 1
    p = result.suggested_placements[0]
    # La nueva pieza no puede estar en (0, 0) porque colisionaría con existing [0..20, 0..20]
    assert not (p.x_cm < Decimal("20") and p.y_cm < Decimal("20"))

    # Validar no colisión con el obstáculo
    fp = get_reserved_footprint(
        piece_length=p.piece_length_cm_snapshot,
        piece_width=p.piece_width_cm_snapshot,
        piece_height=p.piece_height_cm_snapshot,
        separation=p.separation_cm_snapshot,
        rotation_degrees=p.rotation_degrees,
    )
    new_box = BoundingBox2D(
        placement_index=2,
        batch_assignment_id=p.batch_assignment_id,
        level_index=p.level_index,
        left=p.x_cm,
        right=p.x_cm + fp.x_size,
        bottom=p.y_cm,
        top=p.y_cm + fp.y_size,
        height=fp.z_size,
    )
    check_collisions([existing[0], new_box])


def test_packing_multinivel() -> None:
    """Cuando el nivel 0 se llena, las siguientes piezas se colocan en el nivel 1."""
    # Horno pequeño de 20x20. Cada pieza es de 20x20
    # Nivel 0 solo puede alojar 1 pieza.
    # Nivel 1 puede alojar la segunda pieza.
    levels = [
        _default_level(level_index=0, z_cm=Decimal("0"), usable_height_cm=Decimal("20")),
        _default_level(level_index=1, z_cm=Decimal("20"), usable_height_cm=Decimal("20")),
    ]
    pieces = [
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("20"),
            piece_width_cm=Decimal("20"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("0"),
        ),
        PieceToPack(
            batch_assignment_id=1,
            group_index=0,
            unit_index=2,
            piece_length_cm=Decimal("20"),
            piece_width_cm=Decimal("20"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("0"),
        ),
    ]
    result = suggest_layout_packing(
        kiln_width=Decimal("20"),
        kiln_depth=Decimal("20"),
        kiln_height=Decimal("40"),
        levels=levels,
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    assert result.suggested_count == 2
    assert result.levels_used == [0, 1]
    assert result.suggested_placements[0].level_index == 0
    assert result.suggested_placements[1].level_index == 1


def test_packing_independencia_orden_input() -> None:
    """El orden en que se entregan las piezas en la lista de entrada no altera el resultado."""
    p1 = PieceToPack(
        batch_assignment_id=10,
        group_index=0,
        unit_index=1,
        piece_length_cm=Decimal("15"),
        piece_width_cm=Decimal("10"),
        piece_height_cm=Decimal("12"),
        separation_cm=Decimal("1"),
    )
    p2 = PieceToPack(
        batch_assignment_id=20,
        group_index=0,
        unit_index=1,
        piece_length_cm=Decimal("20"),
        piece_width_cm=Decimal("12"),
        piece_height_cm=Decimal("15"),
        separation_cm=Decimal("1"),
    )
    p3 = PieceToPack(
        batch_assignment_id=30,
        group_index=0,
        unit_index=1,
        piece_length_cm=Decimal("8"),
        piece_width_cm=Decimal("8"),
        piece_height_cm=Decimal("8"),
        separation_cm=Decimal("0"),
    )

    res_normal = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=[p1, p2, p3],
    )
    res_shuffled = suggest_layout_packing(
        kiln_width=Decimal("60"),
        kiln_depth=Decimal("50"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=[p3, p1, p2],
    )

    assert res_normal.suggested_placements == res_shuffled.suggested_placements


def test_packing_rendimiento_100_piezas() -> None:
    """100 piezas se empaquetan en tiempo razonable (< 0.5 segundos)."""
    # Horno de 100x100
    pieces = [
        PieceToPack(
            batch_assignment_id=i,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("8"),
            piece_width_cm=Decimal("8"),
            piece_height_cm=Decimal("10"),
            separation_cm=Decimal("1"),
        )
        for i in range(1, 101)
    ]
    start = time.perf_counter()
    result = suggest_layout_packing(
        kiln_width=Decimal("100"),
        kiln_depth=Decimal("100"),
        kiln_height=Decimal("40"),
        levels=[_default_level()],
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    duration = time.perf_counter() - start
    assert result.total_pending == 100
    assert duration < 0.5, f"100 piezas tardaron demasiado: {duration:.4f}s"


def test_packing_rendimiento_500_piezas() -> None:
    """500 piezas se empaquetan en tiempo razonable (< 2.0 segundos)."""
    # Horno amplio con 2 niveles
    levels = [
        _default_level(0, z_cm=Decimal("0"), usable_height_cm=Decimal("20")),
        _default_level(1, z_cm=Decimal("20"), usable_height_cm=Decimal("20")),
    ]
    pieces = [
        PieceToPack(
            batch_assignment_id=i,
            group_index=0,
            unit_index=1,
            piece_length_cm=Decimal("4"),
            piece_width_cm=Decimal("4"),
            piece_height_cm=Decimal("8"),
            separation_cm=Decimal("0"),
        )
        for i in range(1, 501)
    ]
    start = time.perf_counter()
    result = suggest_layout_packing(
        kiln_width=Decimal("100"),
        kiln_depth=Decimal("100"),
        kiln_height=Decimal("40"),
        levels=levels,
        existing_boxes=[],
        pieces_to_pack=pieces,
    )
    duration = time.perf_counter() - start
    assert result.total_pending == 500
    assert duration < 2.0, f"500 piezas tardaron demasiado: {duration:.4f}s"
