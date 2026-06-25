"""Prediction panel — P/B/T probabilities + the honest confidence meter (§7)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QGridLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
)

from ..controller import DashboardState

_CONFIDENCE_TOOLTIP = (
    "Confidence measures how far this shoe deviates from the full-shoe "
    "baseline — NOT your chance of winning. High just means 'unusual shoe'."
)


def _bar(color: str) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setTextVisible(False)
    bar.setFixedHeight(16)
    bar.setStyleSheet(
        f"QProgressBar{{background:#222;border:1px solid #444;border-radius:3px;}}"
        f"QProgressBar::chunk{{background:{color};border-radius:3px;}}"
    )
    return bar


class PredictionPanel(QGroupBox):
    def __init__(self) -> None:
        super().__init__("Prediction")
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)

        self._pct: dict[str, QLabel] = {}
        self._bars: dict[str, QProgressBar] = {}
        colors = {"Banker": "#e74c3c", "Player": "#3498db", "Tie": "#2ecc71"}
        for row, (name, color) in enumerate(colors.items()):
            grid.addWidget(QLabel(name), row, 0)
            bar = _bar(color)
            self._bars[name] = bar
            grid.addWidget(bar, row, 1)
            pct = QLabel("—")
            pct.setMinimumWidth(56)
            pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._pct[name] = pct
            grid.addWidget(pct, row, 2)

        # Prominent next-hand headline.
        self._headline = QLabel("—")
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._headline.setStyleSheet("font-size:18px;font-weight:bold;padding:4px;")
        self._sub = QLabel("")
        self._sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub.setStyleSheet("color:#aaa;")

        self._conf_label = QLabel("Confidence: —")
        self._conf_label.setToolTip(_CONFIDENCE_TOOLTIP)
        self._conf_bar = _bar("#888")
        self._conf_bar.setToolTip(_CONFIDENCE_TOOLTIP)
        self._lean = QLabel("")
        self._lean.setWordWrap(True)
        self._lean.setStyleSheet("color:#aaa;font-style:italic;")

        outer = QVBoxLayout()
        outer.addWidget(self._headline)
        outer.addWidget(self._sub)
        outer.addSpacing(6)
        outer.addLayout(grid)
        outer.addSpacing(8)
        outer.addWidget(self._conf_label)
        outer.addWidget(self._conf_bar)
        outer.addWidget(self._lean)
        outer.addStretch(1)
        self.setLayout(outer)

    def render(self, state: DashboardState) -> None:
        p = state.prediction
        values = {"Banker": p.p_banker, "Player": p.p_player, "Tie": p.p_tie}
        for name, val in values.items():
            self._bars[name].setValue(int(val * 1000))
            self._pct[name].setText(f"{val * 100:.1f}%")

        # Headline: the favoured next side (ignore Tie — it's never the pick).
        colors = {"Banker": "#e74c3c", "Player": "#3498db"}
        side = "Banker" if p.p_banker >= p.p_player else "Player"
        if not p.predicting:
            self._headline.setText("Collecting data…")
            self._headline.setStyleSheet("font-size:18px;font-weight:bold;color:#888;padding:4px;")
            self._sub.setText(p.lean)
        else:
            pct = (p.p_banker if side == "Banker" else p.p_player) * 100
            self._headline.setText(f"▶  {side}  {pct:.1f}%")
            self._headline.setStyleSheet(
                f"font-size:18px;font-weight:bold;color:{colors[side]};padding:4px;"
            )
            edge = abs(p.p_banker - p.p_player) * 100
            self._sub.setText(f"favoured by {edge:.1f} pts over {'Player' if side=='Banker' else 'Banker'}")

        conf = p.confidence
        self._conf_bar.setValue(int(conf * 1000))
        # Color thresholds from §4.5: gray <30%, yellow 30-60%, green >60%.
        color = "#888" if conf < 0.30 else ("#f1c40f" if conf < 0.60 else "#2ecc71")
        self._conf_bar.setStyleSheet(
            f"QProgressBar{{background:#222;border:1px solid #444;border-radius:3px;}}"
            f"QProgressBar::chunk{{background:{color};border-radius:3px;}}"
        )
        self._conf_label.setText(f"Confidence: {conf * 100:.0f}%")
        tag = "" if p.composition_confidence == "high" else "  (composition: low)"
        self._lean.setText(f"{p.lean}{tag}")
