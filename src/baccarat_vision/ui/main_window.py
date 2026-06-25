"""Main dashboard window with manual hand entry (§7, §10 step 3).

This is the build-order step-3 skeleton: the full math runs from manual hand
entry alone — no computer vision yet. Steps 4+ will feed the same
:class:`AppController` from the screen-capture / OCR / CV loop instead of the
manual-entry form.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..controller import AppController, HandInput
from ..settings import SubRegion, save_config
from .bet_panel import BetPanel
from .prediction_panel import PredictionPanel
from .shoe_panel import ShoePanel
from .spread_panel import SpreadPanel

DISCLAIMER = (
    "Read-only analysis tool. Roads are informational, not predictive. Every "
    "side bet carries a house edge (hover each bet). The only defensible edge "
    "is small, late-shoe card-composition drift. Not financial advice."
)

_DARK_QSS = """
QMainWindow, QWidget { background:#1b1b1b; color:#ddd; }
QGroupBox { border:1px solid #3a3a3a; border-radius:6px; margin-top:10px; padding-top:8px; }
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#9ad; }
QPushButton { background:#2d2d2d; border:1px solid #444; border-radius:4px; padding:5px 10px; }
QPushButton:hover { background:#383838; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background:#262626; border:1px solid #444; border-radius:3px; padding:2px 4px; }
QTableWidget { background:#202020; gridline-color:#333; }
QHeaderView::section { background:#2a2a2a; border:0; padding:4px; }
"""


class HandEntryPanel(QGroupBox):
    """Manual hand-entry form (the stand-in for the future vision loop)."""

    def __init__(self, controller: AppController, on_change) -> None:
        super().__init__("Manual Hand Entry")
        self._controller = controller
        self._on_change = on_change

        self._winner = QComboBox()
        self._winner.addItems(["P", "B", "T"])
        self._p_total = _spin_0_9()
        self._b_total = _spin_0_9()
        self._natural = QCheckBox("Natural")
        self._p_pair = QCheckBox("P pair")
        self._b_pair = QCheckBox("B pair")
        self._cards = QLineEdit()
        self._cards.setPlaceholderText("card values e.g. 3,4,2 (optional → exact count)")

        enter = QPushButton("Enter hand")
        enter.clicked.connect(self._enter)
        reshuffle = QPushButton("Reshuffle")
        reshuffle.clicked.connect(self._reshuffle)

        # Two compact rows so it fits the narrow window.
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Winner")); row1.addWidget(self._winner)
        row1.addWidget(QLabel("P")); row1.addWidget(self._p_total)
        row1.addWidget(QLabel("B")); row1.addWidget(self._b_total)
        row1.addStretch(1)

        row2 = QHBoxLayout()
        row2.addWidget(self._natural)
        row2.addWidget(self._p_pair)
        row2.addWidget(self._b_pair)
        row2.addStretch(1)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Cards")); row3.addWidget(self._cards, 1)

        row4 = QHBoxLayout()
        row4.addWidget(enter, 1)
        row4.addWidget(reshuffle)

        layout = QVBoxLayout()
        for r in (row1, row2, row3, row4):
            layout.addLayout(r)
        layout.addStretch(1)
        self.setLayout(layout)

    def _enter(self) -> None:
        text = self._cards.text().strip()
        card_values = None
        if text:
            try:
                card_values = [int(x) for x in text.replace(" ", "").split(",") if x]
            except ValueError:
                card_values = None
        self._controller.enter_hand(
            HandInput(
                winner=self._winner.currentText(),
                player_total=self._p_total.value(),
                banker_total=self._b_total.value(),
                is_natural=self._natural.isChecked(),
                p_pair=self._p_pair.isChecked(),
                b_pair=self._b_pair.isChecked(),
                card_values=card_values,
            )
        )
        self._cards.clear()
        self._natural.setChecked(False)
        self._p_pair.setChecked(False)
        self._b_pair.setChecked(False)
        self._on_change()

    def _reshuffle(self) -> None:
        self._controller.reshuffle()
        self._on_change()


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController | None = None) -> None:
        super().__init__()
        self.controller = controller or AppController()
        cfg = self.controller.config

        self.setWindowTitle("Baccarat Vision")
        self.setStyleSheet(_DARK_QSS)
        if cfg.ui.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowOpacity(cfg.ui.opacity)

        self.prediction_panel = PredictionPanel()
        self.bet_panel = BetPanel(
            self.controller._house_edges, cfg.max_bets
        )
        self.bet_panel.bet_changed.connect(self._on_bet_changed)
        self.shoe_panel = ShoePanel()
        self.spread_panel = SpreadPanel()
        self.entry_panel = HandEntryPanel(self.controller, self.refresh)

        self._header = QLabel()
        self._header.setStyleSheet("font-size:14px;font-weight:bold;color:#9ad;")

        # Live-capture controls (steps 4-7).
        self._pipeline = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_live_tick)
        self._live_btn = QPushButton("● Go Live")
        self._live_btn.setCheckable(True)
        self._live_btn.toggled.connect(self._toggle_live)
        self._region_btn = QPushButton("Calibrate (F2)")
        self._region_btn.clicked.connect(self._calibrate)
        self._newshoe_btn = QPushButton("New Shoe")
        self._newshoe_btn.setToolTip("Reset to a fresh 8-deck shoe minus the opening burn")
        self._newshoe_btn.clicked.connect(self._new_shoe)
        self._status = QLabel("Manual mode")
        self._status.setStyleSheet("color:#888;")
        QShortcut(QKeySequence(Qt.Key.Key_F2), self, activated=self._calibrate)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self._live_btn)
        controls.addWidget(self._newshoe_btn)
        controls.addWidget(self._region_btn)
        self._status.setWordWrap(True)

        footer = QLabel(DISCLAIMER)
        footer.setWordWrap(True)
        footer.setStyleSheet("color:#777;font-size:9px;")

        # Compact tabbed layout so the window is a narrow strip you can park
        # beside the game instead of a giant panel covering it.
        tabs = QTabWidget()
        tabs.addTab(_scroll(self.prediction_panel), "Predict")
        tabs.addTab(_scroll(self.spread_panel), "Spread")
        tabs.addTab(_scroll(self.bet_panel), "Bets")
        tabs.addTab(_scroll(self.shoe_panel), "Shoe")
        tabs.addTab(_scroll(self.entry_panel), "Manual")

        central_layout = QVBoxLayout()
        central_layout.setContentsMargins(8, 6, 8, 6)
        central_layout.setSpacing(6)
        central_layout.addWidget(self._header)
        central_layout.addLayout(controls)
        central_layout.addWidget(self._status)
        central_layout.addWidget(tabs, 1)
        central_layout.addWidget(footer)

        central = QWidget()
        central.setLayout(central_layout)
        self.setCentralWidget(central)

        # Small, resizable, parked at the top-right corner.
        self.resize(440, 600)
        self._park_top_right()
        self.refresh()

    def _park_top_right(self) -> None:
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - 12, geo.top() + 12)

    def _on_bet_changed(self, name: str, amount: float) -> None:
        self.controller.set_bet(name, amount)
        self.refresh()

    def _new_shoe(self) -> None:
        burn = self.controller.config.vision.burn_cards
        self.controller.start_new_shoe(burn)
        if self._pipeline is not None:
            self._pipeline.reset()  # re-sync to the casino's new shoe on next tick
        self._status.setText(f"New shoe — fresh 8 decks minus {burn}-card burn.")
        self.refresh()

    # -- live capture ------------------------------------------------------ #
    def _toggle_live(self, on: bool) -> None:
        if on:
            if not self._ensure_pipeline():
                self._live_btn.setChecked(False)
                return
            fps = max(1, self.controller.config.capture.fps)
            self._timer.start(int(1000 / fps))
            self._live_btn.setText("■ Stop")
            self._status.setText("LIVE — reading screen")
        else:
            self._timer.stop()
            self._live_btn.setText("● Go Live")
            self._status.setText("Manual mode")

    def _ensure_pipeline(self) -> bool:
        if self._pipeline is not None:
            return True
        try:
            from ..capture.screen_grabber import MSSGrabber
            from ..pipeline import VisionPipeline
            from ..vision.ocr_backend import get_ocr_backend

            grabber = MSSGrabber()
            ocr = get_ocr_backend(self.controller.config.vision.ocr_backend)
            self._pipeline = VisionPipeline(self.controller, grabber, ocr)
            return True
        except Exception as exc:  # mss / OCR not installed, no permission, etc.
            QMessageBox.warning(
                self,
                "Live capture unavailable",
                f"Could not start screen capture:\n{exc}\n\n"
                "Install the CV extras (pip install -e '.[cv]') and grant Screen "
                "Recording permission. Manual entry still works.",
            )
            return False

    def _on_live_tick(self) -> None:
        if self._pipeline is None:
            return
        try:
            tick = self._pipeline.tick()
        except Exception as exc:
            self._status.setText(f"Capture error: {exc}")
            self._timer.stop()
            self._live_btn.setChecked(False)
            return
        if tick.synced:
            snap = self.controller.snapshot()
            self._status.setText(
                f"LIVE — synced (hand {snap.hands_played}, {snap.total_remaining} cards left)"
            )
        elif tick.new_hand and tick.hands_added > 1:
            self._status.setText(f"LIVE — caught up {tick.hands_added} hands: {tick.winner}")
        elif tick.new_hand:
            mode = "exact cards" if tick.exact_cards else "estimated"
            self._status.setText(f"LIVE — {tick.winner} wins ({mode})")
        elif tick.counter is not None:
            c = tick.counter
            flag = "" if c.consistent else " ⚠"
            self._status.setText(
                f"LIVE — reading #{c.hand_number} P{c.player_wins} "
                f"B{c.banker_wins} T{c.ties}{flag}"
            )
            self._no_read = 0
        else:
            # Counter unreadable this frame — nudge after a few misses.
            self._no_read = getattr(self, "_no_read", 0) + 1
            if self._no_read >= 4:
                self._status.setText(
                    "LIVE — can't read the counter. Press F2 → Test counter read "
                    "and tighten the box around '#N P.. B.. T..'."
                )
        self.refresh()

    def _calibrate(self) -> None:
        """Open the simple in-window calibration dialog (grab → drag boxes)."""
        from .calibrate_dialog import CalibrationDialog

        was_live = self._timer.isActive()
        if was_live:
            self._live_btn.setChecked(False)  # stop live while calibrating

        dialog = CalibrationDialog(self.controller, self)
        if dialog.exec():
            self._pipeline = None  # rebuild with the new region map
            n = len(self.controller.config.regions)
            self._status.setText(f"Calibrated {n} region(s). Click ● Go Live.")
        else:
            self._status.setText("Calibration cancelled")
        self.refresh()

    def refresh(self) -> None:
        state = self.controller.snapshot()
        live = "●LIVE" if self._timer.isActive() else "○ idle"
        self._header.setText(
            f"Baccarat Vision   {live}   Hand {state.hands_played}  ·  "
            f"{state.total_remaining} cards left"
        )
        self.prediction_panel.render(state)
        self.spread_panel.render(state)
        self.bet_panel.render(state)
        self.shoe_panel.render(state)


def _spin_0_9() -> QSpinBox:
    s = QSpinBox()
    s.setRange(0, 9)
    return s


def _scroll(widget: QWidget) -> QScrollArea:
    """Wrap a panel in a scroll area so a narrow window can show tall content."""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(widget)
    return area
