"""Shoe-composition panel — cards remaining + Big Road mirror (§7)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ..controller import DashboardState
from ..engine.probability import VALUE_LABELS

_ROAD_COLORS = {"P": "#3498db", "B": "#e74c3c"}


class ShoePanel(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Shoe Composition")
        self._bars: list[QProgressBar] = []
        self._counts: list[QLabel] = []

        grid = QGridLayout()
        for v in range(10):
            grid.addWidget(QLabel(VALUE_LABELS[v]), v, 0)
            bar = QProgressBar()
            bar.setTextVisible(False)
            bar.setFixedHeight(12)
            self._bars.append(bar)
            grid.addWidget(bar, v, 1)
            count = QLabel("—")
            count.setMinimumWidth(72)
            count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._counts.append(count)
            grid.addWidget(count, v, 2)
        grid.setColumnStretch(1, 1)
        comp_box = QWidget()
        comp_box.setLayout(grid)

        self._meta = QLabel("")
        self._meta.setStyleSheet("font-family:monospace;color:#bbb;")

        # Big Road mirror (informational only).
        self._road = QLabel("")
        self._road.setStyleSheet("font-family:monospace;")
        self._road.setTextFormat(Qt.TextFormat.RichText)
        road_box = QVBoxLayout()
        road_box.addWidget(QLabel("Big Road (informational only — not predictive)"))
        self._road.setWordWrap(False)
        road_box.addWidget(self._road)

        outer = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(comp_box)
        left.addWidget(self._meta)
        outer.addLayout(left, 1)
        outer.addLayout(road_box, 1)
        self.setLayout(outer)

    def render(self, state: DashboardState) -> None:
        for v in range(10):
            initial = state.shoe_initial[v] or 1
            remaining = state.shoe_counts[v]
            bar = self._bars[v]
            bar.setRange(0, initial)
            bar.setValue(remaining)
            self._counts[v].setText(f"{remaining}/{initial}")

        pen = state.penetration * 100
        self._meta.setText(
            f"{state.total_remaining}/{sum(state.shoe_initial)} cards  ·  "
            f"hand {state.hands_played}  ·  penetration {pen:.0f}%"
            + ("  ·  RESHUFFLE DUE" if state.needs_reshuffle else "")
        )
        self._road.setText(_road_html(state.road_grid))


def _road_html(grid: list[list[str | None]]) -> str:
    """Render the column-major Big Road as a compact HTML grid of dots."""
    rows = 6
    lines = []
    for r in range(rows):
        cells = []
        for col in grid:
            side = col[r] if r < len(col) else None
            if side in _ROAD_COLORS:
                cells.append(f'<span style="color:{_ROAD_COLORS[side]}">●</span>')
            else:
                cells.append('<span style="color:#333">·</span>')
        lines.append("".join(cells) if cells else "·")
    return "<br>".join(lines)
