"""Shoe-counter OCR + parsing (§6.1).

Reads the high-contrast ``#24  P 13  B 10  T 1`` strip and parses it with a
tolerant regex, then sanity-checks that ``P + B + T`` is consistent with the
hand number. The OCR step is delegated to an :class:`OcrBackend`; the parsing
and validation here are pure and fully unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from .ocr_backend import OcrBackend

_NUM_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class CounterReading:
    hand_number: int
    player_wins: int
    banker_wins: int
    ties: int
    consistent: bool  # P + B + T matches hand_number within tolerance

    @property
    def total_results(self) -> int:
        return self.player_wins + self.banker_wins + self.ties


def _find_summing_triple(
    nums: List[int], target: int
) -> Optional[Tuple[int, int, int]]:
    """First order-preserving (P, B, T) triple in ``nums`` that sums to target.

    The counter is always ``hand, P, B, T`` with ``P+B+T == hand``. OCR of the
    coloured P/B/T circles sometimes injects a spurious digit (e.g. the red 'B'
    badge read as '8'); picking the triple that sums to the hand number drops
    that noise and recovers the right counts.
    """
    n = len(nums)
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                if nums[i] + nums[j] + nums[k] == target:
                    return nums[i], nums[j], nums[k]
    return None


def parse_counter(text: str, tolerance: int = 0) -> Optional[CounterReading]:
    """Parse a counter string into a :class:`CounterReading`, or ``None``.

    Strategy: the first number is the hand count; among the remaining numbers we
    find the (in-order) triple that sums to it -> P, B, T. If none sums exactly
    we fall back to the first three and mark the reading inconsistent so the
    caller can discard it. This tolerates OCR noise from the coloured badges.
    """
    if not text:
        return None
    nums = [int(m.group()) for m in _NUM_RE.finditer(text)]
    if len(nums) < 4:
        return None  # need at least hand + P + B + T
    hand, rest = nums[0], nums[1:]
    triple = _find_summing_triple(rest, hand)
    if triple is not None:
        p, b, t = triple
        consistent = True
    else:
        p, b, t = rest[0], rest[1], rest[2]
        consistent = abs((p + b + t) - hand) <= tolerance
    return CounterReading(
        hand_number=hand,
        player_wins=p,
        banker_wins=b,
        ties=t,
        consistent=consistent,
    )


def preprocess_counter(image: np.ndarray) -> np.ndarray:
    """Isolate the white counter text from the coloured P/B/T circles.

    The counter shows white digits/letters on blue/red/green circles; plain
    thresholding lets the circle colours confuse OCR (it drops the B/T fields).
    Keeping only near-white pixels, upscaling, and inverting to dark-text-on-
    light makes EasyOCR read "#24 P 14 B 9 T 1" reliably.
    """
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, mask = cv2.threshold(gray, 165, 255, cv2.THRESH_BINARY)  # white text -> 255
    up = cv2.resize(mask, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_NEAREST)
    return cv2.bitwise_not(up)  # dark text on light background


def _ocr_digits(crop: np.ndarray, backend: OcrBackend) -> str:
    import cv2

    if crop.size == 0:
        return ""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    _, mask = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
    up = cv2.resize(mask, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_NEAREST)
    text = backend.read_text(cv2.bitwise_not(up))
    return "".join(ch for ch in text if ch.isdigit())


def _detect_badges(image: np.ndarray):
    """Locate the P(blue)/B(red)/T(green) badges -> {side: (left, center, right)}."""
    import cv2

    h, w = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((sat > 90) & (val > 110)).astype(np.uint8)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    badges: dict = {}
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        ww = stats[i, cv2.CC_STAT_WIDTH]
        hh = stats[i, cv2.CC_STAT_HEIGHT]
        # Compact, roughly square blob that isn't the full-height background.
        if not (80 < area < 3000 and 0.5 < ww / max(hh, 1) < 2.0 and hh < h * 0.85):
            continue
        med = int(np.median(hue[lab == i]))
        if 100 <= med <= 130:
            side = "P"
        elif 40 <= med <= 90:
            side = "T"
        elif med <= 10 or med >= 165:
            side = "B"
        else:
            continue
        left = int(stats[i, cv2.CC_STAT_LEFT])
        right = left + ww
        center = int(cent[i][0])
        if side not in badges or area > badges[side][3]:
            badges[side] = (left, center, right, area)
    return {s: v[:3] for s, v in badges.items()}


def read_counter_by_color(
    image: np.ndarray, backend: OcrBackend
) -> Optional[CounterReading]:
    """Read the counter by locating coloured badges and OCRing each number.

    Each count is OCR'd from the slice immediately right of its own badge, so a
    digit can never be assigned to the wrong field and badge glyphs are never
    mistaken for digits. Returns ``None`` if all three badges aren't found.
    """
    badges = _detect_badges(image)
    if not all(s in badges for s in ("P", "B", "T")):
        return None
    p_l, p_c, p_r = badges["P"]
    b_l, b_c, b_r = badges["B"]
    t_l, t_c, t_r = badges["T"]
    if not (p_c < b_c < t_c):  # expect left-to-right P, B, T
        return None
    w = image.shape[1]
    hand = _ocr_digits(image[:, :p_l], backend)
    p = _ocr_digits(image[:, p_r:b_l], backend)
    b = _ocr_digits(image[:, b_r:t_l], backend)
    t = _ocr_digits(image[:, t_r:w], backend)
    try:
        hand, p, b, t = int(hand), int(p), int(b), int(t)
    except ValueError:
        return None
    return CounterReading(hand, p, b, t, consistent=(p + b + t == hand))


def read_counter(
    image: np.ndarray, backend: OcrBackend, preprocess: bool = True
) -> Optional[CounterReading]:
    """OCR a counter-strip image and parse it.

    Tries the robust colour-badge reader first (maps each number to P/B/T by the
    badge colour); falls back to whole-strip OCR + text parsing.
    """
    by_color = read_counter_by_color(image, backend)
    if by_color is not None and by_color.consistent:
        return by_color
    img = preprocess_counter(image) if preprocess else image
    return parse_counter(backend.read_text(img))
