"""New-hand detection (§6.3).

Two signals, per the spec:

* **Counter change** (authoritative): the OCR'd hand number increments.
* **Visual cue** (fallback): the centre-table result region changes materially
  vs the previous frame (a lightweight SSIM-style image difference).

The counter is trusted when available; the visual signal is used only to flag a
likely new hand when the counter can't be read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .counter_reader import CounterReading


def image_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Return a 0..1 similarity (1.0 == identical) via normalised MSE.

    A cheap stand-in for SSIM that needs no extra dependency. Shapes must match;
    mismatched shapes return 0.0 (treated as fully changed).
    """
    if a.shape != b.shape or a.size == 0:
        return 0.0
    af = a.astype(np.float32)
    bf = b.astype(np.float32)
    mse = float(np.mean((af - bf) ** 2))
    # 255^2 is the max per-channel squared error; map MSE -> similarity.
    return max(0.0, 1.0 - mse / (255.0 ** 2))


@dataclass
class DetectionResult:
    new_hand: bool
    source: str  # "counter" | "visual" | "none"
    visual_similarity: Optional[float] = None


class ResultDetector:
    """Stateful detector holding the previous counter + result frame."""

    def __init__(self, visual_change_threshold: float = 0.85) -> None:
        # Below this similarity the result region is considered "changed".
        self.visual_change_threshold = visual_change_threshold
        self._last_hand: Optional[int] = None
        self._last_result_frame: Optional[np.ndarray] = None
        self._visual_armed = True  # debounce: one visual trigger per change

    def feed(
        self,
        counter: Optional[CounterReading] = None,
        result_region: Optional[np.ndarray] = None,
    ) -> DetectionResult:
        # 1) Authoritative: counter increment.
        if counter is not None:
            hand = counter.hand_number
            if self._last_hand is None:
                self._last_hand = hand
            elif hand > self._last_hand:
                self._last_hand = hand
                self._sync_visual(result_region)
                return DetectionResult(True, "counter")
            elif hand != self._last_hand:
                # Counter went backwards -> new shoe; resync without firing.
                self._last_hand = hand
            self._sync_visual(result_region)
            return DetectionResult(False, "counter")

        # 2) Fallback: visual change of the result region.
        if result_region is not None:
            sim = None
            if self._last_result_frame is not None:
                sim = image_similarity(result_region, self._last_result_frame)
                changed = sim < self.visual_change_threshold
                if changed and self._visual_armed:
                    self._visual_armed = False
                    self._last_result_frame = result_region.copy()
                    return DetectionResult(True, "visual", sim)
                if not changed:
                    self._visual_armed = True  # re-arm once it settles
            self._last_result_frame = result_region.copy()
            return DetectionResult(False, "visual", sim)

        return DetectionResult(False, "none")

    def _sync_visual(self, result_region: Optional[np.ndarray]) -> None:
        if result_region is not None:
            self._last_result_frame = result_region.copy()
            self._visual_armed = True

    def reset(self) -> None:
        self._last_hand = None
        self._last_result_frame = None
        self._visual_armed = True
