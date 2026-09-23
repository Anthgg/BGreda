"""Motor geométrico puro para el layout físico del horno (Fase 010M - M2).

Define la matemática de empaquetamiento, dimensiones reservadas, límites
y detección de colisiones. Este módulo es de dominio puro: opera únicamente
con tipos `Decimal` y estructuras de datos estándar, sin dependencias de
FastAPI ni de la base de datos.

Convención de coordenadas:
--------------------------
- `(x_cm, y_cm)` representa la esquina inferior izquierda del ÁREA RESERVADA
  del placement en la superficie útil del nivel.
- El origen `(0, 0)` es la esquina inferior izquierda de la superficie útil.
- Eje X: ancho del horno (`kiln_width_cm`).
- Eje Y: profundidad del horno (`kiln_depth_cm`).
- Eje Z: altura del horno (`kiln_height_cm`).

Área reservada (Reserved Footprint):
------------------------------------
- `reserved_length = piece_length + separation`
- `reserved_width = piece_width + separation`
- `reserved_height = piece_height + separation`
- Con `rotation = 0`:
  `reserved_x_size = reserved_length`
  `reserved_y_size = reserved_width`
- Con `rotation = 90`:
  `reserved_x_size = reserved_width`
  `reserved_y_size = reserved_length`
- La altura reservada no cambia con la rotación:
  `reserved_z_size = reserved_height`
- Si `separation == 0`: las dimensiones reservadas son exactamente las de la pieza.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ReservedFootprint:
    """Dimensiones físicas reservadas por una pieza, incluyendo separación y rotación."""

    x_size: Decimal
    y_size: Decimal
    z_size: Decimal


@dataclass(frozen=True)
class BoundingBox2D:
    """Caja delimitadora 2D en un nivel del horno (coordenadas del área reservada)."""

    placement_index: int
    batch_assignment_id: int
    level_index: int
    left: Decimal
    right: Decimal
    bottom: Decimal
    top: Decimal
    height: Decimal


@dataclass(frozen=True)
class LevelGeometry:
    """Geometría de un nivel del horno para validación."""

    level_index: int
    z_cm: Decimal
    usable_height_cm: Decimal


@dataclass(frozen=True)
class PlacementGeometry:
    """Parámetros de un placement para validación geométrica."""

    index: int
    batch_assignment_id: int
    quantity: int
    level_index: int
    x_cm: Decimal
    y_cm: Decimal
    rotation_degrees: int
    piece_length_cm: Decimal
    piece_width_cm: Decimal
    piece_height_cm: Decimal
    separation_cm: Decimal


def get_reserved_footprint(
    *,
    piece_length: Decimal,
    piece_width: Decimal,
    piece_height: Decimal,
    separation: Decimal,
    rotation_degrees: int,
) -> ReservedFootprint:
    """Calcula el footprint reservado para una pieza considerando separación y rotación.

    No doble cuenta la separación: la separación se suma exactamente una vez
    a cada dimensión lineal (largo, ancho, alto).
    """
    reserved_length = piece_length + separation
    reserved_width = piece_width + separation
    reserved_height = piece_height + separation

    if rotation_degrees == 0:
        x_size = reserved_length
        y_size = reserved_width
    elif rotation_degrees == 90:
        x_size = reserved_width
        y_size = reserved_length
    else:
        raise ValueError(f"rotation_degrees debe ser 0 o 90, recibido: {rotation_degrees}")

    return ReservedFootprint(
        x_size=x_size,
        y_size=y_size,
        z_size=reserved_height,
    )


def validate_level_geometry(
    levels: Sequence[LevelGeometry],
    kiln_height: Decimal,
) -> None:
    """Valida que los niveles estén dentro del horno y no se solapen verticalmente.

    Reglas:
    1. Para cada nivel: `z_cm >= 0`.
    2. Para cada nivel: `usable_height_cm > 0`.
    3. Para cada nivel: `z_cm + usable_height_cm <= kiln_height`.
    4. Para cada par de niveles distintos: los intervalos `[z, z + usable_height]`
       no deben solaparse en su interior (contacto adyacente en borde es válido).

    Lanza `ValueError` con prefijo de código si alguna regla se viola.
    """
    for lvl in levels:
        if lvl.z_cm < Decimal("0"):
            raise ValueError(
                f"LEVEL_OUT_OF_BOUNDS: Nivel {lvl.level_index} tiene z_cm negativo: {lvl.z_cm}"
            )
        if lvl.usable_height_cm <= Decimal("0"):
            raise ValueError(
                f"LEVEL_OUT_OF_BOUNDS: Nivel {lvl.level_index} tiene usable_height_cm <= 0: "
                f"{lvl.usable_height_cm}"
            )
        if (lvl.z_cm + lvl.usable_height_cm) > kiln_height:
            raise ValueError(
                f"LEVEL_OUT_OF_BOUNDS: Nivel {lvl.level_index} excede la altura del horno "
                f"({lvl.z_cm} + {lvl.usable_height_cm} = {lvl.z_cm + lvl.usable_height_cm} > "
                f"{kiln_height})"
            )

    # Validar no solapamiento vertical entre niveles
    # Dos intervalos [z1, top1] y [z2, top2] se solapan si max(z1, z2) < min(top1, top2)
    for i, a in enumerate(levels):
        top_a = a.z_cm + a.usable_height_cm
        for b in levels[i + 1 :]:
            top_b = b.z_cm + b.usable_height_cm
            if max(a.z_cm, b.z_cm) < min(top_a, top_b):
                raise ValueError(
                    f"LEVEL_OVERLAP: Niveles {a.level_index} [z={a.z_cm}, top={top_a}] y "
                    f"{b.level_index} [z={b.z_cm}, top={top_b}] se solapan verticalmente."
                )


def validate_placement_bounds(
    *,
    placement_index: int,
    assignment_id: int,
    level_index: int,
    footprint: ReservedFootprint,
    x_cm: Decimal,
    y_cm: Decimal,
    kiln_width: Decimal,
    kiln_depth: Decimal,
    level_usable_height: Decimal,
) -> BoundingBox2D:
    """Valida los límites X, Y dentro del horno y la altura dentro del nivel.

    Reglas:
    1. `x_cm >= 0`
    2. `x_cm + footprint.x_size <= kiln_width`
    3. `y_cm >= 0`
    4. `y_cm + footprint.y_size <= kiln_depth`
    5. `footprint.z_size <= level_usable_height`

    Lanza `ValueError` con prefijo de código si alguna regla se viola.
    Devuelve el `BoundingBox2D` resultante para verificación de colisiones.
    """
    if x_cm < Decimal("0"):
        raise ValueError(
            f"OUT_OF_BOUNDS: Placement {placement_index} (asignación {assignment_id}) "
            f"tiene x_cm negativo: {x_cm}"
        )
    right = x_cm + footprint.x_size
    if right > kiln_width:
        raise ValueError(
            f"OUT_OF_BOUNDS: Placement {placement_index} (asignación {assignment_id}) "
            f"excede el ancho del horno (x={x_cm} + ancho_reservado={footprint.x_size} = "
            f"{right} > ancho_horno={kiln_width})"
        )

    if y_cm < Decimal("0"):
        raise ValueError(
            f"OUT_OF_BOUNDS: Placement {placement_index} (asignación {assignment_id}) "
            f"tiene y_cm negativo: {y_cm}"
        )
    top = y_cm + footprint.y_size
    if top > kiln_depth:
        raise ValueError(
            f"OUT_OF_BOUNDS: Placement {placement_index} (asignación {assignment_id}) "
            f"excede la profundidad del horno (y={y_cm} + prof_reservada={footprint.y_size} = "
            f"{top} > prof_horno={kiln_depth})"
        )

    if footprint.z_size > level_usable_height:
        raise ValueError(
            f"HEIGHT_EXCEEDED: Placement {placement_index} (asignación {assignment_id}) "
            f"excede la altura útil del nivel {level_index} "
            f"(altura_reservada={footprint.z_size} > altura_nivel={level_usable_height})"
        )

    return BoundingBox2D(
        placement_index=placement_index,
        batch_assignment_id=assignment_id,
        level_index=level_index,
        left=x_cm,
        right=right,
        bottom=y_cm,
        top=top,
        height=footprint.z_size,
    )


def placements_overlap(box_a: BoundingBox2D, box_b: BoundingBox2D) -> bool:
    """Determina si dos cajas delimitadoras 2D en el mismo nivel se superponen.

    Condición de NO colisión:
    `box_a.right <= box_b.left` o
    `box_b.right <= box_a.left` o
    `box_a.top <= box_b.bottom` o
    `box_b.top <= box_a.bottom`.

    Si ninguna se cumple, hay colisión.
    El contacto exacto en bordes (ej: `box_a.right == box_b.left`) es VÁLIDO
    y no se considera colisión porque la separación ya está incluida en la caja.
    """
    if (
        box_a.right <= box_b.left
        or box_b.right <= box_a.left
        or box_a.top <= box_b.bottom
        or box_b.top <= box_a.bottom
    ):
        return False
    return True


def check_collisions(boxes: Sequence[BoundingBox2D]) -> None:
    """Verifica colisiones por pares entre placements en el mismo nivel.

    Lanza `ValueError` con prefijo `COLLISION:` ante el primer solapamiento detectado.
    """
    # Agrupar por nivel
    by_level: dict[int, list[BoundingBox2D]] = {}
    for box in boxes:
        by_level.setdefault(box.level_index, []).append(box)

    for level_idx, level_boxes in by_level.items():
        n = len(level_boxes)
        for i in range(n):
            a = level_boxes[i]
            for j in range(i + 1, n):
                b = level_boxes[j]
                if placements_overlap(a, b):
                    raise ValueError(
                        f"COLLISION: Placements {a.placement_index} (asignación "
                        f"{a.batch_assignment_id}) y {b.placement_index} (asignación "
                        f"{b.batch_assignment_id}) colisionan en nivel {level_idx}: "
                        f"A=[x:{a.left}..{a.right}, y:{a.bottom}..{a.top}] solapa con "
                        f"B=[x:{b.left}..{b.right}, y:{b.bottom}..{b.top}]."
                    )


def validate_layout_geometry(
    *,
    kiln_width: Decimal,
    kiln_depth: Decimal,
    kiln_height: Decimal,
    levels: Sequence[LevelGeometry],
    placements: Sequence[PlacementGeometry],
) -> list[BoundingBox2D]:
    """Orquesta la validación geométrica completa del layout físico.

    Pasos:
    1. Validar niveles (límites del horno y no solapamiento vertical).
    2. Validar cada placement:
       - `quantity == 1`
       - Nivel existe en la lista de niveles
       - Límites X e Y dentro del horno
       - Altura reservada dentro de la altura del nivel
    3. Detectar colisiones 2D por nivel.

    Devuelve la lista de `BoundingBox2D` de todos los placements validados.
    Lanza `ValueError` con prefijo explicativo si alguna regla no se cumple.
    """
    # 1. Validar niveles
    validate_level_geometry(levels, kiln_height)

    # Mapear niveles por level_index
    level_map = {lvl.level_index: lvl for lvl in levels}

    # 2. Validar cada placement individual
    boxes: list[BoundingBox2D] = []
    for p in placements:
        # Semántica de quantity física: exactamente 1 por placement
        if p.quantity != 1:
            raise ValueError(
                f"PHYSICAL_QUANTITY_INVALID: Placement {p.index} (asignación "
                f"{p.batch_assignment_id}) tiene cantidad {p.quantity}; cada placement "
                f"físico debe representar exactamente cantidad = 1."
            )

        if p.level_index not in level_map:
            raise ValueError(
                f"LEVEL_NOT_FOUND: Placement {p.index} referencia level_index {p.level_index} "
                f"que no existe en los niveles definidos del layout."
            )

        lvl = level_map[p.level_index]

        footprint = get_reserved_footprint(
            piece_length=p.piece_length_cm,
            piece_width=p.piece_width_cm,
            piece_height=p.piece_height_cm,
            separation=p.separation_cm,
            rotation_degrees=p.rotation_degrees,
        )

        box = validate_placement_bounds(
            placement_index=p.index,
            assignment_id=p.batch_assignment_id,
            level_index=p.level_index,
            footprint=footprint,
            x_cm=p.x_cm,
            y_cm=p.y_cm,
            kiln_width=kiln_width,
            kiln_depth=kiln_depth,
            level_usable_height=lvl.usable_height_cm,
        )
        boxes.append(box)

    # 3. Detectar colisiones 2D
    check_collisions(boxes)

    return boxes
