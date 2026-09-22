"""Motor determinista de auto-packing físico para hornadas (Fase 010M - M3).

Implementa una heurística First Fit Decreasing (FFD) con búsqueda de posiciones
candidatas por puntos extremos (Extreme Points / Corner Points) y preferencia espacial
hacia el origen (menor Y, luego menor X).

Reglas fundamentales:
1. Determinismo estricto: Mismo input -> mismo output, sin aleatoriedad ni IA/ML.
2. Reutiliza las funciones geométricas de M2 (kiln_layout_geometry).
3. Los placements existentes son obstáculos fijos: no se mueven ni duplican.
4. Solo se empaquetan piezas pendientes: assignment.quantity - placed_quantity.
5. Cada placement representa exactamente 1 unidad física (quantity == 1).
6. La sugerencia NO persiste nada en la base de datos ni muta estado.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.services.kiln_layout_geometry import (
    BoundingBox2D,
    LevelGeometry,
    get_reserved_footprint,
    validate_level_geometry,
)


@dataclass(frozen=True)
class PieceToPack:
    """Representa una unidad física individual pendiente por empaquetar."""

    batch_assignment_id: int
    group_index: int
    unit_index: int | None
    piece_length_cm: Decimal
    piece_width_cm: Decimal
    piece_height_cm: Decimal
    separation_cm: Decimal


@dataclass(frozen=True)
class SuggestedPlacement:
    """Resultado de una unidad empaquetada exitosamente."""

    batch_assignment_id: int
    group_index: int
    unit_index: int | None
    quantity: int
    level_index: int
    x_cm: Decimal
    y_cm: Decimal
    rotation_degrees: int
    piece_length_cm_snapshot: Decimal
    piece_width_cm_snapshot: Decimal
    piece_height_cm_snapshot: Decimal
    separation_cm_snapshot: Decimal


@dataclass(frozen=True)
class UnplacedPiece:
    """Unidad que no pudo ser ubicada en ningún nivel."""

    batch_assignment_id: int
    unit_index: int | None
    quantity: int
    reason: str


@dataclass(frozen=True)
class PackingResult:
    """Resultado completo del motor de auto-packing."""

    suggested_placements: list[SuggestedPlacement]
    unplaced_pieces: list[UnplacedPiece]
    levels_used: list[int]
    total_pending: int
    suggested_count: int
    unplaced_count: int


def _piece_sort_key(p: PieceToPack) -> tuple[Decimal, Decimal, Decimal, Decimal, int, int]:
    """Criterio de ordenamiento First Fit Decreasing estable y determinista:

    1. Mayor altura reservada (-reserved_height)
    2. Mayor área reservada (-reserved_area)
    3. Mayor lado (-max_side)
    4. Mayor segundo lado (-min_side)
    5. batch_assignment_id ascendente (desempate determinista)
    6. unit_index ascendente (desempate determinista)
    """
    res_l = p.piece_length_cm + p.separation_cm
    res_w = p.piece_width_cm + p.separation_cm
    res_h = p.piece_height_cm + p.separation_cm
    area = res_l * res_w
    max_s = max(res_l, res_w)
    min_s = min(res_l, res_w)
    return (
        -res_h,
        -area,
        -max_s,
        -min_s,
        p.batch_assignment_id,
        p.unit_index if p.unit_index is not None else 0,
    )


class LevelPacker:
    """Maneja el espacio y los obstáculos ocupados en un nivel específico."""

    def __init__(
        self,
        level: LevelGeometry,
        kiln_width: Decimal,
        kiln_depth: Decimal,
        existing_boxes: list[BoundingBox2D],
    ) -> None:
        self.level = level
        self.kiln_width = kiln_width
        self.kiln_depth = kiln_depth
        self.boxes: list[BoundingBox2D] = list(existing_boxes)
        self.candidates: set[tuple[Decimal, Decimal]] = set()
        self._init_candidates()

    def _is_inside_any_box(self, pt: tuple[Decimal, Decimal]) -> bool:
        x, y = pt
        for b in self.boxes:
            if b.left <= x < b.right and b.bottom <= y < b.top:
                return True
        return False

    def _init_candidates(self) -> None:
        # Si no hay cajas, el único candidato inicial es el origen (0, 0)
        if not self.boxes:
            self.candidates = {(Decimal("0"), Decimal("0"))}
            return

        cands = {(Decimal("0"), Decimal("0"))}
        for b in self.boxes:
            self._add_box_candidate_points(b, cands)

        # Filtrar candidatos dentro de cajas o fuera de límites
        self.candidates = {
            pt
            for pt in cands
            if 0 <= pt[0] < self.kiln_width
            and 0 <= pt[1] < self.kiln_depth
            and not self._is_inside_any_box(pt)
        }

    def _add_box_candidate_points(
        self,
        b: BoundingBox2D,
        cand_set: set[tuple[Decimal, Decimal]],
    ) -> None:
        if b.right < self.kiln_width:
            cand_set.add((b.right, b.bottom))
            cand_set.add((b.right, Decimal("0")))
            for ob in self.boxes:
                if ob.left <= b.right < ob.right and ob.top <= b.bottom:
                    cand_set.add((b.right, ob.top))

        if b.top < self.kiln_depth:
            cand_set.add((b.left, b.top))
            cand_set.add((Decimal("0"), b.top))
            for ob in self.boxes:
                if ob.bottom <= b.top < ob.top and ob.right <= b.left:
                    cand_set.add((ob.right, b.top))

    def add_box(self, new_box: BoundingBox2D) -> None:
        """Registra una nueva caja colocada y actualiza los puntos candidatos."""
        self.boxes.append(new_box)
        # Invalidar candidatos cubiertos por new_box
        self.candidates = {
            pt
            for pt in self.candidates
            if not (new_box.left <= pt[0] < new_box.right and new_box.bottom <= pt[1] < new_box.top)
        }
        # Generar nuevos candidatos a partir de new_box
        new_cands: set[tuple[Decimal, Decimal]] = set()
        self._add_box_candidate_points(new_box, new_cands)

        for pt in new_cands:
            if (
                0 <= pt[0] < self.kiln_width
                and 0 <= pt[1] < self.kiln_depth
                and not self._is_inside_any_box(pt)
            ):
                self.candidates.add(pt)

    def try_place_piece(
        self,
        piece: PieceToPack,
        placement_index: int,
    ) -> tuple[SuggestedPlacement, BoundingBox2D] | None:
        """Intenta colocar la pieza en este nivel.

        1. Descarta de inmediato si excede la altura útil del nivel.
        2. Recorre candidatos ordenados por (y, x) ascendente (más cercanos al origen).
        3. Evalúa rotaciones 0 y 90.
        4. Elige rotación según criterio determinista y coloca la pieza.
        """
        reserved_height = piece.piece_height_cm + piece.separation_cm
        if reserved_height > self.level.usable_height_cm:
            return None

        fp_0 = get_reserved_footprint(
            piece_length=piece.piece_length_cm,
            piece_width=piece.piece_width_cm,
            piece_height=piece.piece_height_cm,
            separation=piece.separation_cm,
            rotation_degrees=0,
        )
        fp_90 = get_reserved_footprint(
            piece_length=piece.piece_length_cm,
            piece_width=piece.piece_width_cm,
            piece_height=piece.piece_height_cm,
            separation=piece.separation_cm,
            rotation_degrees=90,
        )

        min_w = min(fp_0.x_size, fp_90.x_size)
        min_h = min(fp_0.y_size, fp_90.y_size)

        sorted_candidates = [
            pt
            for pt in sorted(self.candidates, key=lambda pt: (pt[1], pt[0]))
            if pt[0] + min_w <= self.kiln_width and pt[1] + min_h <= self.kiln_depth
        ]

        for cx, cy in sorted_candidates:
            # Evaluar rotación 0
            fits_0 = False
            r0 = cx + fp_0.x_size
            t0 = cy + fp_0.y_size
            if (r0 <= self.kiln_width) and (t0 <= self.kiln_depth):
                if not any(
                    not (r0 <= b.left or b.right <= cx or t0 <= b.bottom or b.top <= cy)
                    for b in self.boxes
                ):
                    fits_0 = True

            # Evaluar rotación 90
            fits_90 = False
            r90 = cx + fp_90.x_size
            t90 = cy + fp_90.y_size
            if (r90 <= self.kiln_width) and (t90 <= self.kiln_depth):
                if not any(
                    not (r90 <= b.left or b.right <= cx or t90 <= b.bottom or b.top <= cy)
                    for b in self.boxes
                ):
                    fits_90 = True

            chosen_rot: int | None = None
            chosen_w: Decimal = Decimal("0")
            chosen_h: Decimal = Decimal("0")

            if fits_0 and fits_90:
                # Ambas rotaciones son válidas en este punto candidato:
                # 1. Preferir menor top (más compacto en profundidad Y)
                # 2. Desempate: menor right (más compacto en ancho X)
                # 3. Desempate: rotación 0
                if t0 < t90:
                    chosen_rot = 0
                    chosen_w, chosen_h = fp_0.x_size, fp_0.y_size
                elif t90 < t0:
                    chosen_rot = 90
                    chosen_w, chosen_h = fp_90.x_size, fp_90.y_size
                else:
                    if r0 < r90:
                        chosen_rot = 0
                        chosen_w, chosen_h = fp_0.x_size, fp_0.y_size
                    elif r90 < r0:
                        chosen_rot = 90
                        chosen_w, chosen_h = fp_90.x_size, fp_90.y_size
                    else:
                        chosen_rot = 0
                        chosen_w, chosen_h = fp_0.x_size, fp_0.y_size
            elif fits_0:
                chosen_rot = 0
                chosen_w, chosen_h = fp_0.x_size, fp_0.y_size
            elif fits_90:
                chosen_rot = 90
                chosen_w, chosen_h = fp_90.x_size, fp_90.y_size

            if chosen_rot is not None:
                chosen_box = BoundingBox2D(
                    placement_index=placement_index,
                    batch_assignment_id=piece.batch_assignment_id,
                    level_index=self.level.level_index,
                    left=cx,
                    right=cx + chosen_w,
                    bottom=cy,
                    top=cy + chosen_h,
                    height=fp_0.z_size,
                )
                self.add_box(chosen_box)
                suggested = SuggestedPlacement(
                    batch_assignment_id=piece.batch_assignment_id,
                    group_index=piece.group_index,
                    unit_index=piece.unit_index,
                    quantity=1,
                    level_index=self.level.level_index,
                    x_cm=chosen_box.left,
                    y_cm=chosen_box.bottom,
                    rotation_degrees=chosen_rot,
                    piece_length_cm_snapshot=piece.piece_length_cm,
                    piece_width_cm_snapshot=piece.piece_width_cm,
                    piece_height_cm_snapshot=piece.piece_height_cm,
                    separation_cm_snapshot=piece.separation_cm,
                )
                return suggested, chosen_box

        return None


def suggest_layout_packing(
    *,
    kiln_width: Decimal,
    kiln_depth: Decimal,
    kiln_height: Decimal,
    levels: Sequence[LevelGeometry],
    existing_boxes: Sequence[BoundingBox2D],
    pieces_to_pack: Sequence[PieceToPack],
) -> PackingResult:
    """Ejecuta el motor de auto-packing físico determinista.

    1. Si no hay niveles definidos: todas las piezas son unplaced con NO_LEVELS.
    2. Ordena los niveles por level_index ascendente y valida geometría M2.
    3. Inicializa un LevelPacker por nivel con las cajas existentes de ese nivel.
    4. Ordena las piezas por criterio FFD estable.
    5. Para cada pieza, intenta ubicarla en los niveles en orden.
    6. Si no cabe en ningún nivel, la clasifica en unplaced_pieces.
    7. Retorna PackingResult.
    """
    total_pending = len(pieces_to_pack)
    if not levels:
        unplaced = [
            UnplacedPiece(
                batch_assignment_id=p.batch_assignment_id,
                unit_index=p.unit_index,
                quantity=1,
                reason="NO_LEVELS",
            )
            for p in pieces_to_pack
        ]
        return PackingResult(
            suggested_placements=[],
            unplaced_pieces=unplaced,
            levels_used=[],
            total_pending=total_pending,
            suggested_count=0,
            unplaced_count=total_pending,
        )

    sorted_levels = sorted(levels, key=lambda lvl: lvl.level_index)

    # Validar niveles con motor M2 (bounds, z >= 0, height > 0, no vertical overlap)
    validate_level_geometry(sorted_levels, kiln_height)

    boxes_by_level: dict[int, list[BoundingBox2D]] = {lvl.level_index: [] for lvl in sorted_levels}
    for box in existing_boxes:
        if box.level_index in boxes_by_level:
            boxes_by_level[box.level_index].append(box)

    packers = [
        LevelPacker(
            level=lvl,
            kiln_width=kiln_width,
            kiln_depth=kiln_depth,
            existing_boxes=boxes_by_level[lvl.level_index],
        )
        for lvl in sorted_levels
    ]

    sorted_pieces = sorted(pieces_to_pack, key=_piece_sort_key)

    suggested_placements: list[SuggestedPlacement] = []
    unplaced_pieces: list[UnplacedPiece] = []
    levels_used_set: set[int] = set()

    placement_idx_counter = len(existing_boxes) + 1

    for piece in sorted_pieces:
        placed = False
        res_h = piece.piece_height_cm + piece.separation_cm
        all_height_exceeded = all(res_h > lvl.usable_height_cm for lvl in sorted_levels)

        for packer in packers:
            res = packer.try_place_piece(piece, placement_index=placement_idx_counter)
            if res is not None:
                suggested, _ = res
                suggested_placements.append(suggested)
                levels_used_set.add(packer.level.level_index)
                placement_idx_counter += 1
                placed = True
                break

        if not placed:
            reason = "HEIGHT_EXCEEDED" if all_height_exceeded else "NO_VALID_POSITION"
            unplaced_pieces.append(
                UnplacedPiece(
                    batch_assignment_id=piece.batch_assignment_id,
                    unit_index=piece.unit_index,
                    quantity=1,
                    reason=reason,
                )
            )

    levels_used = sorted(levels_used_set)
    return PackingResult(
        suggested_placements=suggested_placements,
        unplaced_pieces=unplaced_pieces,
        levels_used=levels_used,
        total_pending=total_pending,
        suggested_count=len(suggested_placements),
        unplaced_count=len(unplaced_pieces),
    )
