"""Simple, reliable region calibration (replaces the fullscreen overlay).

Why this design: the old approach used a translucent fullscreen overlay +
``QScreen.grabWindow``, which on Retina/macOS produced misaligned, chopped
backdrops and stray windows. This dialog instead:

* grabs the screen with the **same ``mss`` path the live capture uses**, so the
  pixels you box are exactly the pixels the pipeline will read (no coordinate
  drift, no Retina skew);
* shows that screenshot in **one ordinary window** and lets you drag boxes on
  it — no overlays, no extra windows;
* lets you **test the counter read** right there before saving.

Regions are saved as absolute pixels and the capture region is the full frame,
so live capture (full-screen grab) and these boxes share one coordinate space.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# (config key, menu label). Counter is the only one required for live to work.
REGION_CHOICES = [
    ("shoe_counter", "Counter  ·  #N P.. B.. T..   (REQUIRED)"),
    ("table_result", "Result panel (center)"),
    ("card_player", "Player cards (bottom result panel)"),
    ("card_banker", "Banker cards (bottom result panel)"),
]


def _bgr_to_pixmap(img: np.ndarray) -> QPixmap:
    import cv2

    rgb = np.ascontiguousarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    h, w = rgb.shape[:2]
    qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)


class _ImageCanvas(QWidget):
    """Shows the screenshot and lets the user drag a box per region."""

    def __init__(self) -> None:
        super().__init__()
        self._pix: Optional[QPixmap] = None
        self.scale = 1.0  # full-screen px per displayed px
        self.rects: Dict[str, QRect] = {}  # region -> rect in DISPLAY coords
        self.target = REGION_CHOICES[0][0]
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None
        self.setMinimumSize(320, 200)

    def set_image(self, pix: QPixmap, scale: float) -> None:
        self._pix = pix
        self.scale = scale
        self.setFixedSize(pix.size())
        self.update()

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if self._pix is not None and e.button() == Qt.MouseButton.LeftButton:
            self._origin = e.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._origin is not None:
            self._current = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self._origin is not None:
            r = QRect(self._origin, e.position().toPoint()).normalized()
            if r.width() > 3 and r.height() > 3:
                self.rects[self.target] = r
            self._origin = self._current = None
            self.update()

    def full_rects(self) -> Dict[str, Tuple[int, int, int, int]]:
        out: Dict[str, Tuple[int, int, int, int]] = {}
        for name, r in self.rects.items():
            out[name] = (
                round(r.x() * self.scale),
                round(r.y() * self.scale),
                round(r.width() * self.scale),
                round(r.height() * self.scale),
            )
        return out

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        if self._pix is not None:
            p.drawPixmap(0, 0, self._pix)
        for name, r in self.rects.items():
            on = name == self.target
            p.setPen(QPen(QColor(120, 200, 255) if on else QColor(90, 210, 130), 2))
            p.drawRect(r)
            p.setPen(QColor(255, 255, 255))
            p.drawText(r.topLeft() + QPoint(3, -4), name)
        if self._origin is not None and self._current is not None:
            p.setPen(QPen(QColor(255, 220, 80), 2))
            p.drawRect(QRect(self._origin, self._current).normalized())


class CalibrationDialog(QDialog):
    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._full: Optional[np.ndarray] = None
        self.setWindowTitle("Calibrate capture regions")

        self.canvas = _ImageCanvas()
        self.combo = QComboBox()
        for key, label in REGION_CHOICES:
            self.combo.addItem(label, key)
        self.combo.currentIndexChanged.connect(
            lambda: setattr(self.canvas, "target", self.combo.currentData())
        )

        grab = QPushButton("📷 Grab screen")
        grab.clicked.connect(self._grab)
        test = QPushButton("Test counter read")
        test.clicked.connect(self._test_counter)
        save = QPushButton("Save")
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)

        self._status = QLabel("")
        self._status.setWordWrap(True)

        steps = QLabel(
            "1) Click Grab screen.  2) Choose a region in the dropdown.  "
            "3) Drag a box on the image.  Re-drag to redo.  The Counter box is "
            "required; Result and card boxes are optional."
        )
        steps.setWordWrap(True)

        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(False)

        toolbar = QHBoxLayout()
        toolbar.addWidget(grab)
        toolbar.addWidget(QLabel("Drawing:"))
        toolbar.addWidget(self.combo, 1)
        toolbar.addWidget(test)

        actions = QHBoxLayout()
        actions.addWidget(self._status, 1)
        actions.addWidget(save)
        actions.addWidget(cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(steps)
        layout.addLayout(toolbar)
        layout.addWidget(scroll, 1)
        layout.addLayout(actions)
        self.resize(1120, 800)
        self._status.setText("Click 📷 Grab screen to begin.")

    # -- actions ----------------------------------------------------------- #
    def _grab(self) -> None:
        # Hide our window so it isn't in the shot, then grab via mss.
        win = self.window()
        parent = self.parent()
        try:
            self.hide()
            if parent is not None:
                parent.hide()
            from PySide6.QtWidgets import QApplication

            QApplication.processEvents()
            import time

            time.sleep(0.2)
            from ..capture.screen_grabber import MSSGrabber

            grabber = MSSGrabber()
            img = grabber.grab_full()
            grabber.close()
        except Exception as exc:
            self._status.setText(f"Capture failed: {exc}")
            img = None
        finally:
            if parent is not None:
                parent.show()
            self.show()
            self.raise_()
            self.activateWindow()
        if img is None:
            return
        self._full = img
        fw = img.shape[1]
        pix = _bgr_to_pixmap(img)
        display_w = min(1000, fw)
        scaled = pix.scaledToWidth(display_w, Qt.TransformationMode.SmoothTransformation)
        self.canvas.set_image(scaled, fw / scaled.width())
        self._status.setText(
            f"Captured {img.shape[1]}×{img.shape[0]} px. Draw the Counter box first."
        )

    def _test_counter(self) -> None:
        if self._full is None or "shoe_counter" not in self.canvas.rects:
            self._status.setText("Draw the Counter box first, then test.")
            return
        x, y, w, h = self.canvas.full_rects()["shoe_counter"]
        crop = self._full[y : y + h, x : x + w]
        try:
            from ..vision.counter_reader import read_counter
            from ..vision.ocr_backend import get_ocr_backend

            backend = get_ocr_backend(self.controller.config.vision.ocr_backend)
            reading = read_counter(crop, backend)
        except Exception as exc:
            self._status.setText(f"OCR error: {exc}")
            return
        if reading is None:
            self._status.setText("Counter NOT read — make the box tight around '#N P.. B.. T..'.")
        else:
            ok = "✓ consistent" if reading.consistent else "⚠ inconsistent"
            self._status.setText(
                f"Read #{reading.hand_number} P{reading.player_wins} "
                f"B{reading.banker_wins} T{reading.ties}  {ok}"
            )

    def _save(self) -> None:
        rects = self.canvas.full_rects()
        if "shoe_counter" not in rects:
            QMessageBox.warning(self, "Counter required", "Draw a box around the counter strip.")
            return
        from ..settings import SubRegion, save_config

        cfg = self.controller.config
        fh, fw = self._full.shape[:2]
        cfg.capture.region.x = 0
        cfg.capture.region.y = 0
        cfg.capture.region.width = fw
        cfg.capture.region.height = fh
        cfg.regions = {
            name: SubRegion(x=x, y=y, w=w, h=h) for name, (x, y, w, h) in rects.items()
        }
        cfg.vision.read_cards = any(n.startswith("card_") for n in cfg.regions)
        try:
            save_config(cfg)
        except Exception as exc:
            self._status.setText(f"Saved in-memory, but writing config failed: {exc}")
        self.accept()
