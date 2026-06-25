"""Big Road parsing from pixels (§6.2).

The Big Road is a 6-row, column-major grid: each cell is empty, a blue Player
circle, or a red Banker circle, with a green diagonal slash marking a tie. We
compute the cell grid from the configured region, classify each cell by HSV
colour masks, and reconstruct the P/B sequence (plus tie marks). The result is
cross-validated against the OCR counter by the caller (§6.2).

:func:`render_big_road` draws a synthetic road from a known sequence — used to
generate snapshot-test fixtures without needing real casino screenshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

DEFAULT_ROWS = 6

# HSV thresholds (OpenCV H in 0-179). Tuned for typical bright road markers.
_SAT_MIN = 80
_VAL_MIN = 80
_MIN_PIXELS = 12  # colored pixels in a cell before it counts as filled


@dataclass
class RoadReadResult:
    grid: List[List[Optional[str]]]  # column-major: grid[col][row] in {P,B,None}
    sequence: List[str]              # reconstructed P/B order (column-major)
    tie_marks: int                   # cells carrying a tie slash
    columns: int
    rows: int


def _color_masks(hsv: np.ndarray):
    import cv2

    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    sat_val = (s >= _SAT_MIN) & (v >= _VAL_MIN)
    red = (((h <= 10) | (h >= 170)) & sat_val)
    blue = ((h >= 90) & (h <= 130) & sat_val)
    green = ((h >= 40) & (h <= 85) & sat_val)
    return red, blue, green


def classify_cell(cell_bgr: np.ndarray) -> tuple[Optional[str], bool]:
    """Classify one cell -> (side in {'P','B',None}, has_tie_slash)."""
    import cv2

    if cell_bgr.size == 0:
        return None, False
    hsv = cv2.cvtColor(cell_bgr, cv2.COLOR_BGR2HSV)
    red, blue, green = _color_masks(hsv)
    red_n, blue_n, green_n = int(red.sum()), int(blue.sum()), int(green.sum())
    has_tie = green_n >= _MIN_PIXELS
    if max(red_n, blue_n) < _MIN_PIXELS:
        return None, has_tie
    return ("B" if red_n >= blue_n else "P"), has_tie


def read_big_road(
    region_bgr: np.ndarray,
    rows: int = DEFAULT_ROWS,
    columns: Optional[int] = None,
) -> RoadReadResult:
    """Parse a Big Road region image into a grid + reconstructed sequence."""
    h, w = region_bgr.shape[:2]
    cell = max(1, h // rows)
    if columns is None:
        columns = max(1, w // cell)

    grid: List[List[Optional[str]]] = []
    sequence: List[str] = []
    tie_marks = 0
    for c in range(columns):
        col_cells: List[Optional[str]] = []
        for r in range(rows):
            y0, x0 = r * cell, c * cell
            cell_img = region_bgr[y0 : y0 + cell, x0 : x0 + cell]
            side, tie = classify_cell(cell_img)
            col_cells.append(side)
            if side is not None:
                sequence.append(side)
            if tie:
                tie_marks += 1
        grid.append(col_cells)
    return RoadReadResult(
        grid=grid, sequence=sequence, tie_marks=tie_marks, columns=columns, rows=rows
    )


def render_big_road(
    columns: List[List[str]],
    cell: int = 24,
    rows: int = DEFAULT_ROWS,
    ties: Optional[set[tuple[int, int]]] = None,
) -> np.ndarray:
    """Render a synthetic Big Road image from column-major P/B data.

    ``columns`` is a list of columns, each a list of 'P'/'B' strings (top→down).
    ``ties`` is an optional set of (col, row) cells to mark with a green slash.
    """
    import cv2

    width = max(1, len(columns)) * cell
    img = np.zeros((rows * cell, width, 3), dtype=np.uint8)  # black background
    ties = ties or set()
    for c, col in enumerate(columns):
        for r, side in enumerate(col):
            if r >= rows:
                break
            center = (c * cell + cell // 2, r * cell + cell // 2)
            color = (0, 0, 255) if side == "B" else (255, 0, 0)  # BGR red/blue
            cv2.circle(img, center, cell // 2 - 3, color, 2)
            if (c, r) in ties:
                cv2.line(
                    img,
                    (c * cell + 4, r * cell + cell - 4),
                    (c * cell + cell - 4, r * cell + 4),
                    (0, 255, 0),
                    2,
                )
    return img
