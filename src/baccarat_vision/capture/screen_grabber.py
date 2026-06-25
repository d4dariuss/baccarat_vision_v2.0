"""Screen capture (§6) — throttled region grabbing via ``mss``.

The :class:`Grabber` protocol abstracts *where pixels come from* so the whole
vision pipeline can run identically against:

* live screen capture (:class:`MSSGrabber`), or
* a saved frame (:class:`StillImageGrabber`) for tests, fixtures and replay.

All images are returned as 3-channel **BGR** ``numpy`` arrays (OpenCV's
convention), regardless of source.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Optional, Protocol

import numpy as np


def _quartz_grab_fullscreen() -> Optional[np.ndarray]:
    """Grab the main display at **native (Retina) resolution** via CoreGraphics.

    ``mss`` on macOS captures at logical (non-Retina) resolution — half the
    pixels on a 2x display — which is too low-res for OCR. Quartz's
    ``CGDisplayCreateImage`` returns the full physical-pixel image. Returns a BGR
    array, or ``None`` if Quartz isn't available / fails (caller falls back).
    """
    try:
        import Quartz
    except Exception:
        return None
    try:
        image = Quartz.CGDisplayCreateImage(Quartz.CGMainDisplayID())
        if image is None:
            return None
        w = Quartz.CGImageGetWidth(image)
        h = Quartz.CGImageGetHeight(image)
        bpr = Quartz.CGImageGetBytesPerRow(image)
        provider = Quartz.CGImageGetDataProvider(image)
        data = Quartz.CGDataProviderCopyData(provider)
        buf = np.frombuffer(data, dtype=np.uint8)
        # The buffer can carry a few trailing pad bytes; clip to exactly h rows
        # of `bytes_per_row` before reshaping (32-bit BGRA, possible row padding).
        buf = buf[: h * bpr]
        arr = buf.reshape((h, bpr // 4, 4))[:, :w, :3]
        return np.ascontiguousarray(arr)  # BGR
    except Exception:
        return None


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in pixels."""

    x: int
    y: int
    w: int
    h: int

    def crop(self, image: np.ndarray) -> np.ndarray:
        """Return the sub-image at this rect (rows y..y+h, cols x..x+w)."""
        return image[self.y : self.y + self.h, self.x : self.x + self.w]


def rect_from_subregion(sub) -> Rect:
    """Build a :class:`Rect` from a settings ``SubRegion`` (x/y/w/h)."""
    return Rect(x=sub.x, y=sub.y, w=sub.w, h=sub.h)


class Grabber(Protocol):
    def grab(self, region: Rect) -> np.ndarray:  # pragma: no cover - protocol
        """Return a BGR image of the screen rectangle ``region``."""
        ...


class StillImageGrabber:
    """A :class:`Grabber` backed by a fixed in-memory frame.

    Used by tests and replay mode. ``grab`` crops the requested region out of
    the held full frame (treating the frame's origin as the region origin).
    """

    def __init__(self, frame: np.ndarray, origin: tuple[int, int] = (0, 0)) -> None:
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("frame must be an HxWx3 BGR image")
        self.frame = frame
        self.origin = origin

    def grab(self, region: Rect) -> np.ndarray:
        ox, oy = self.origin
        rel = Rect(region.x - ox, region.y - oy, region.w, region.h)
        return rel.crop(self.frame).copy()


class MSSGrabber:
    """Live screen capture via ``mss`` (lazy-imported).

    Grabs the **full monitor** and crops the requested region in physical-pixel
    space. This sidesteps mss's ambiguous handling of custom sub-rectangles on
    Retina/macOS: the full-monitor image is always at the display's physical
    resolution, which is the same coordinate space the (screenshot-backed)
    region selector saves into. The crop is then a plain array slice.
    """

    def __init__(self, monitor_index: int = 1) -> None:
        # monitors[0] is the union of all displays; [1] is the primary.
        self._monitor_index = monitor_index
        # Prefer Quartz on macOS (native Retina resolution); mss is the fallback.
        self._use_quartz = sys.platform == "darwin" and _quartz_grab_fullscreen() is not None
        self._sct = None
        if not self._use_quartz:
            import mss  # noqa: F401 - clear error if neither backend is present

            self._sct = mss.mss()

    def _ensure_mss(self):
        if self._sct is None:
            import mss

            self._sct = mss.mss()
        return self._sct

    def grab_full(self) -> np.ndarray:
        if self._use_quartz:
            img = _quartz_grab_fullscreen()
            if img is not None:
                return img
            self._use_quartz = False  # Quartz stopped working -> fall back
        sct = self._ensure_mss()
        shot = sct.grab(sct.monitors[self._monitor_index])
        img = np.asarray(shot)[:, :, :3]  # BGRA -> BGR
        return np.ascontiguousarray(img)

    def grab(self, region: Rect) -> np.ndarray:
        full = self.grab_full()
        h, w = full.shape[:2]
        # Clamp to the captured frame so an over-large region can't error.
        x0, y0 = max(0, region.x), max(0, region.y)
        x1, y1 = min(w, region.x + region.w), min(h, region.y + region.h)
        if x1 <= x0 or y1 <= y0:
            return np.zeros((0, 0, 3), dtype=full.dtype)
        return full[y0:y1, x0:x1].copy()

    def close(self) -> None:
        try:
            if self._sct is not None:
                self._sct.close()
        except Exception:
            pass


class ThrottledLoop:
    """Helper to run a callback at a fixed FPS without a UI event loop.

    Intended for headless capture/replay; the live UI uses a Qt timer instead.
    """

    def __init__(self, fps: float) -> None:
        self.interval = 1.0 / fps if fps > 0 else 0.0
        self._running = False

    def run(self, step, max_iters: Optional[int] = None) -> None:
        self._running = True
        iters = 0
        while self._running:
            start = time.monotonic()
            step()
            iters += 1
            if max_iters is not None and iters >= max_iters:
                break
            elapsed = time.monotonic() - start
            if self.interval > elapsed:
                time.sleep(self.interval - elapsed)

    def stop(self) -> None:
        self._running = False
