"""Dynamic bet-spread panel — probability-driven stake sizing (§ dynamic_spread).

Shows the engine's per-hand recommendation: main bet × dynamic unit multiplier,
triggered side bets with their activation reason, EV estimates, and the three
component signals that drove the size decision.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..controller import DashboardState

_PHASE_COLOR = {"early": "#888", "mid": "#f1c40f", "late": "#2ecc71"}
_BET_COLOR = {"banker": "#e74c3c", "player": "#3498db", "tie": "#2ecc71",
              "super_6": "#9b59b6", "either_pair": "#e67e22", "player_pair": "#3498db",
              "banker_pair": "#e74c3c", "p_bonus": "#3498db", "b_bonus": "#e74c3c"}


def _signal_bar(color: str = "#9ad") -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setTextVisible(False)
    bar.setFixedHeight(10)
    bar.setStyleSheet(
        f"QProgressBar{{background:#222;border:1px solid #333;border-radius:2px;}}"
        f"QProgressBar::chunk{{background:{color};border-radius:2px;}}"
    )
    return bar


class SpreadPanel(QGroupBox):
    """Probability-driven dynamic bet-spread recommendation."""

    def __init__(self) -> None:
        super().__init__("Dynamic Bet Spread")

        # ── Headline ──────────────────────────────────────────────────────── #
        self._headline = QLabel("—")
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._headline.setStyleSheet("font-size:16px;font-weight:bold;padding:4px;")

        self._note = QLabel("")
        self._note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._note.setStyleSheet("color:#aaa;font-size:11px;")
        self._note.setWordWrap(True)

        # ── Phase + signal bars ───────────────────────────────────────────── #
        sig_grid = QGridLayout()
        sig_grid.setColumnStretch(1, 1)

        self._phase_lbl = QLabel("Phase: —")
        self._phase_lbl.setStyleSheet("color:#888;")
        sig_grid.addWidget(self._phase_lbl, 0, 0, 1, 3)

        labels = ["Composition", "Learner", "Pattern", "Combined"]
        colors = ["#3498db", "#2ecc71", "#f1c40f", "#9ad"]
        self._sig_bars: list[QProgressBar] = []
        self._sig_lbls: list[QLabel] = []
        for i, (lbl, col) in enumerate(zip(labels, colors), start=1):
            sig_grid.addWidget(QLabel(lbl), i, 0)
            bar = _signal_bar(col)
            self._sig_bars.append(bar)
            sig_grid.addWidget(bar, i, 1)
            pct = QLabel("0%")
            pct.setMinimumWidth(36)
            pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            pct.setStyleSheet("color:#aaa;font-size:11px;")
            self._sig_lbls.append(pct)
            sig_grid.addWidget(pct, i, 2)

        # ── Legs table ────────────────────────────────────────────────────── #
        self._legs_widget = QWidget()
        self._legs_layout = QVBoxLayout(self._legs_widget)
        self._legs_layout.setContentsMargins(0, 0, 0, 0)
        self._legs_layout.setSpacing(3)

        # ── Totals ────────────────────────────────────────────────────────── #
        self._total_lbl = QLabel("Total at risk:  —")
        self._total_lbl.setStyleSheet("font-family:monospace;")
        self._ev_lbl = QLabel("Total EV:  —")
        self._ev_lbl.setStyleSheet("font-family:monospace;")
        self._afford_lbl = QLabel("")
        self._afford_lbl.setStyleSheet("color:#e74c3c;font-size:10px;")

        outer = QVBoxLayout()
        outer.setSpacing(6)
        outer.addWidget(self._headline)
        outer.addWidget(self._note)
        outer.addSpacing(4)
        outer.addLayout(sig_grid)
        outer.addSpacing(6)
        outer.addWidget(QLabel("Recommended spread:"))
        outer.addWidget(self._legs_widget)
        outer.addSpacing(4)
        outer.addWidget(self._total_lbl)
        outer.addWidget(self._ev_lbl)
        outer.addWidget(self._afford_lbl)
        outer.addStretch(1)
        self.setLayout(outer)

    # ── Rendering ─────────────────────────────────────────────────────────── #
    def render(self, state: DashboardState) -> None:
        ds = state.dynamic_spread
        if ds is None:
            self._headline.setText("Shoe nearly spent")
            self._headline.setStyleSheet("font-size:16px;font-weight:bold;color:#888;padding:4px;")
            self._note.setText("Reshuffle to get fresh recommendations.")
            self._clear_legs()
            return

        # Headline: main bet + multiplier.
        color = _BET_COLOR.get(ds.main_bet, "#9ad")
        curr = f"{ds.currency} " if ds.currency else ""
        self._headline.setText(
            f"▶  {ds.main_label}  ×{ds.multiplier:.1f}  {curr}{ds.total_stake:,.0f}"
        )
        self._headline.setStyleSheet(
            f"font-size:16px;font-weight:bold;color:{color};padding:4px;"
        )
        self._note.setText(ds.note)

        # Phase label.
        pc = _PHASE_COLOR.get(ds.phase, "#888")
        self._phase_lbl.setText(
            f"Phase: <span style='color:{pc}'>{ds.phase.title()}</span>  "
            f"({state.penetration*100:.0f}% dealt)"
        )
        self._phase_lbl.setTextFormat(Qt.TextFormat.RichText)

        # Signal bars (comp, learner, pattern, combined).
        for bar, lbl, val in zip(
            self._sig_bars, self._sig_lbls,
            [ds.composition_signal, ds.learner_signal, ds.pattern_signal, ds.signal],
        ):
            bar.setValue(int(val * 1000))
            lbl.setText(f"{val*100:.0f}%")

        # Legs.
        self._clear_legs()
        for leg in ds.legs:
            self._legs_layout.addWidget(_LegRow(leg, ds.currency))

        # Totals.
        self._total_lbl.setText(f"Total at risk:  {curr}{ds.total_stake:,.2f}")
        ev_sign = "+" if ds.total_ev >= 0 else "−"
        self._ev_lbl.setText(
            f"Total EV:  {ev_sign}{curr}{abs(ds.total_ev):,.2f}"
        )
        self._afford_lbl.setText(
            "" if ds.affordable else "⚠ scaled down to fit balance"
        )

    def _clear_legs(self) -> None:
        while self._legs_layout.count():
            item = self._legs_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()


class _LegRow(QWidget):
    """One row in the legs table: label · stake · units · EV · reason."""

    def __init__(self, leg, currency: str = "") -> None:
        super().__init__()
        curr = f"{currency} " if currency else ""
        color = _BET_COLOR.get(leg.bet, "#9ad")

        name = QLabel(leg.label)
        name.setStyleSheet(f"color:{color};font-weight:bold;min-width:90px;")

        stake = QLabel(f"{curr}{leg.stake:,.2f}")
        stake.setStyleSheet("font-family:monospace;")
        stake.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        units = QLabel(f"{leg.units:.1f}u")
        units.setStyleSheet("color:#888;font-size:10px;")
        units.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        ev_sign = "+" if leg.ev >= 0 else "−"
        ev_lbl = QLabel(f"EV {ev_sign}{curr}{abs(leg.ev):,.2f}")
        ev_color = "#2ecc71" if leg.ev >= 0 else "#e74c3c"
        ev_lbl.setStyleSheet(f"color:{ev_color};font-size:10px;font-family:monospace;")
        ev_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        reason = QLabel(leg.reason)
        reason.setStyleSheet("color:#777;font-size:9px;")
        reason.setWordWrap(True)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(name)
        row.addWidget(stake)
        row.addWidget(units)
        row.addWidget(ev_lbl)

        col = QVBoxLayout()
        col.setContentsMargins(0, 2, 0, 2)
        col.setSpacing(1)
        col.addLayout(row)
        col.addWidget(reason)
        self.setLayout(col)
