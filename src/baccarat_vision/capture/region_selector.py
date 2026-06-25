"""Click-drag region selector overlay (§7).

Shows a **live screenshot of your screen** dimmed behind a bright selection
rectangle, so you can see exactly what you're framing (not a blind grey box).
Drag to select, **Enter** accepts, **Esc** cancels/skips.

Retina handling: Qt reports mouse coordinates in *logical* points, but ``mss``
captures in *physical* pixels. We multiply by the screen's ``devicePixelRatio``
(and add the screen origin) so the saved region lands in the same coordinate
space ``mss`` grabs from. The conversion lives in :func:`logical_to_physical`
and is unit-tested headlessly.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QWidget

from .screen_grabber import Rect


def logical_to_physical(
    x: float, y: float, w: float, h: float, dpr: float,
    origin_x: float = 0.0, origin_y: float = 0.0,
) -> Rect:
    """Map a logical-point selection to a physical-pixel :class:`Rect`.

    ``origin_*`` is the screen's top-left in logical global coordinates (nonzero
    on secondary monitors). ``dpr`` is the device-pixel ratio (2.0 on Retina).
    """
    return Rect(
        x=round((origin_x + x) * dpr),
        y=round((origin_y + y) * dpr),
        w=round(w * dpr),
        h=round(h * dpr),
    )


class RegionSelector(QWidget):
    """Full-screen drag-to-select overlay with a screenshot backdrop."""

    region_selected = Signal(object)  # emits Rect (physical px) or None

    def __init__(self, prompt: str = "Drag to select — Enter accepts, Esc cancels") -> None:
        super().__init__()
        self._prompt = prompt
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None
        self._loop: Optional[QEventLoop] = None
        self._result: Optional[Rect] = None

        self._screen = QGuiApplication.primaryScreen()
        # Grab BEFORE showing so we don't capture our own overlay.
        self._backdrop = self._screen.grabWindow(0) if self._screen else None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        if self._screen:
            self.setGeometry(self._screen.geometry())
        self.setWindowState(Qt.WindowState.WindowFullScreen)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    # -- blocking API (used by guided calibration) ------------------------- #
    def select_blocking(self) -> Optional[Rect]:
        """Show modally and return the selected Rect (physical px) or None."""
        self._loop = QEventLoop()
        self.show()
        self.raise_()
        self.activateWindow()
        self._loop.exec()
        return self._result

    def _finish(self, rect: Optional[Rect]) -> None:
        self._result = rect
        self.region_selected.emit(rect)
        self.close()
        if self._loop is not None:
            self._loop.quit()

    # -- selection geometry ------------------------------------------------ #
    def selected_qrect(self) -> Optional[QRect]:
        if self._origin is None or self._current is None:
            return None
        return QRect(self._origin, self._current).normalized()

    def selected_rect(self) -> Optional[Rect]:
        qr = self.selected_qrect()
        if qr is None or qr.width() < 2 or qr.height() < 2:
            return None
        dpr = self._screen.devicePixelRatio() if self._screen else 1.0
        geo = self._screen.geometry() if self._screen else None
        ox, oy = (geo.x(), geo.y()) if geo else (0, 0)
        return logical_to_physical(qr.x(), qr.y(), qr.width(), qr.height(), dpr, ox, oy)

    # -- mouse ------------------------------------------------------------- #
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    # -- keyboard ---------------------------------------------------------- #
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(None)
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish(self.selected_rect())

    # -- painting ---------------------------------------------------------- #
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # 1) Frozen screenshot backdrop at full brightness (1:1 — the pixmap's
        #    logical size equals the widget size, so no scaling/Retina skew).
        if self._backdrop is not None:
            painter.drawPixmap(self.rect(), self._backdrop)
        else:
            painter.fillRect(self.rect(), QColor(20, 20, 20))

        qr = self.selected_qrect()
        if qr is not None:
            # Dim only OUTSIDE the selection so the chosen area stays crisp.
            dim = QColor(0, 0, 0, 120)
            painter.fillRect(QRect(0, 0, self.width(), qr.top()), dim)
            painter.fillRect(QRect(0, qr.bottom(), self.width(), self.height() - qr.bottom()), dim)
            painter.fillRect(QRect(0, qr.top(), qr.left(), qr.height()), dim)
            painter.fillRect(QRect(qr.right(), qr.top(), self.width() - qr.right(), qr.height()), dim)
            # Bright selection outline + size readout.
            painter.setPen(QPen(QColor(120, 200, 255), 2))
            painter.drawRect(qr)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(
                qr.adjusted(4, -20, 0, 0),
                Qt.AlignmentFlag.AlignLeft,
                f"{qr.width()}×{qr.height()}",
            )

        # Prompt banner (top).
        painter.fillRect(0, 0, self.width(), 44, QColor(0, 0, 0, 200))
        painter.setPen(QColor(235, 235, 235))
        painter.drawText(
            QRect(0, 0, self.width(), 44),
            Qt.AlignmentFlag.AlignCenter,
            self._prompt,
        )
