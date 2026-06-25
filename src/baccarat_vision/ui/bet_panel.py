"""Bet-spread panel — stake inputs (with house-edge tooltips) + outcome matrix (§7)."""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QGroupBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..betting.payout_table import BET_LABELS
from ..controller import DashboardState

# Bets ordered as in the dashboard mock; flag the high-edge side bets.
_BET_ORDER = [
    "player", "banker", "tie", "super_6",
    "player_pair", "banker_pair", "either_pair", "suited_pair",
    "p_bonus", "b_bonus",
]


class BetPanel(QGroupBox):
    bet_changed = Signal(str, float)  # (bet_name, amount)

    def __init__(self, house_edges: Dict[str, float], max_bets: Dict[str, float]) -> None:
        super().__init__("Bet Spread")
        self._spins: Dict[str, QDoubleSpinBox] = {}

        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        for row, bet in enumerate(_BET_ORDER):
            label = QLabel(BET_LABELS[bet])
            edge = house_edges.get(bet)
            if edge is not None:
                tip = f"House edge ≈ {edge * 100:.2f}%"
                if edge >= 0.10:
                    tip += "  ⚠ high — payout multiplier hides the cost"
                    label.setStyleSheet("color:#e67e22;")
                label.setToolTip(tip)
            grid.addWidget(label, row, 0)

            spin = QDoubleSpinBox()
            spin.setRange(0.0, float(max_bets.get(bet, 100000)))
            spin.setDecimals(2)
            spin.setSingleStep(1.0)
            spin.setPrefix("$ ")
            spin.valueChanged.connect(
                lambda val, b=bet: self.bet_changed.emit(b, val)
            )
            if edge is not None:
                spin.setToolTip(f"House edge ≈ {edge * 100:.2f}%")
            self._spins[bet] = spin
            grid.addWidget(spin, row, 1)

        bet_box = QWidget()
        bet_box.setLayout(grid)

        # Summary stats.
        self._risk = QLabel("Total at risk:  $0.00")
        self._ev = QLabel("EV:  $0.00")
        self._best = QLabel("Best case:  $0.00")
        self._worst = QLabel("Worst case:  $0.00")
        self._vol = QLabel("Volatility (σ):  $0.00")
        for lbl in (self._risk, self._ev, self._best, self._worst, self._vol):
            lbl.setStyleSheet("font-family:monospace;")

        # Outcome matrix table.
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Outcome", "Probability", "Net"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        outer = QVBoxLayout()
        outer.addWidget(bet_box)
        for lbl in (self._risk, self._ev, self._best, self._worst, self._vol):
            outer.addWidget(lbl)
        outer.addWidget(QLabel("Outcome matrix"))
        outer.addWidget(self._table, 1)
        self.setLayout(outer)

    def render(self, state: DashboardState) -> None:
        s = state.spread
        self._risk.setText(f"Total at risk:  ${s.total_at_risk:,.2f}")
        self._ev.setText(f"EV:  {_money(s.expected_value)}")
        self._best.setText(f"Best case:  {_money(s.best_case)}")
        self._worst.setText(f"Worst case:  {_money(s.worst_case)}")
        self._vol.setText(f"Volatility (σ):  ${s.volatility:,.2f}")

        rows = s.rows
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            name = QTableWidgetItem(("⭐ " if r.is_best else "") + r.label)
            prob = QTableWidgetItem(f"{r.probability * 100:.2f}%")
            net = QTableWidgetItem(_money(r.net))
            prob.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            net.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if r.net > 0:
                net.setForeground(Qt.GlobalColor.green)
            elif r.net < 0:
                net.setForeground(Qt.GlobalColor.red)
            self._table.setItem(i, 0, name)
            self._table.setItem(i, 1, prob)
            self._table.setItem(i, 2, net)


def _money(v: float) -> str:
    sign = "−" if v < 0 else "+"
    return f"{sign}${abs(v):,.2f}"
