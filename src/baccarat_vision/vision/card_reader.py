"""Card-value OCR for true counting (§6.4 / §10 step 9, stretch goal).

Reads the *rank* of each dealt card from its on-screen region and maps it to a
baccarat value (A=1, 2-9 face, 10/J/Q/K=0). This is the only path to genuine
card-composition counting (§4.3) — but it's inherently unreliable during the
deal animation (motion blur), so the pipeline always **validates** a read
against the independently-known winner (counter delta / road) and falls back to
estimated composition when they disagree. Honesty over false precision (§0).

The OCR step is delegated to an :class:`OcrBackend`; the rank parsing and
value mapping here are pure and unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .ocr_backend import OcrBackend

# Rank string -> baccarat card value.
RANK_TO_VALUE = {
    "A": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "10": 0, "T": 0, "J": 0, "Q": 0, "K": 0,
}

# Match a rank token. '10' must be tried before bare digits; 'O' (letter, a
# common OCR misread of a 10/0) maps to a ten.
_RANK_RE = re.compile(r"(10|[2-9]|[AJQKT]|O)", re.IGNORECASE)


def parse_card_rank(text: str) -> Optional[str]:
    """Extract the first canonical rank token ('A','2'..'9','10','J','Q','K')."""
    if not text:
        return None
    match = _RANK_RE.search(text.upper())
    if not match:
        return None
    token = match.group(1)
    return "10" if token == "O" else token


def extract_card_values(text: str) -> List[int]:
    """Extract *all* card values from OCR text (e.g. '2 7' -> [2, 7]).

    Used to read a whole per-side card band where 2-3 cards appear together,
    so the number of cards doesn't need fixed slot regions.
    """
    if not text:
        return []
    values: List[int] = []
    for match in _RANK_RE.finditer(text.upper()):
        token = match.group(1)
        if token == "O":
            token = "10"
        value = RANK_TO_VALUE.get(token)
        if value is not None:
            values.append(value)
    return values


def rank_to_value(rank: str) -> Optional[int]:
    return RANK_TO_VALUE.get(rank.upper())


def baccarat_total(values: List[int]) -> int:
    return sum(values) % 10


@dataclass
class CardReadResult:
    player_values: List[int]
    banker_values: List[int]
    player_total: int
    banker_total: int
    winner: str  # "P" | "B" | "T"
    all_values: List[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.all_values:
            self.all_values = list(self.player_values) + list(self.banker_values)


def preprocess_card(image: np.ndarray) -> np.ndarray:
    """Upscale + threshold a card crop to help OCR the rank glyph."""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    gray = cv2.resize(gray, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def read_card_values(
    image: np.ndarray, backend: OcrBackend, preprocess: bool = True
) -> List[int]:
    """OCR one crop and return *all* card values found in it (0-9 each).

    A crop may be a single card slot (one value) or a whole per-side band
    (2-3 values); both are handled by extracting every rank token.
    """
    if image is None or image.size == 0:
        return []
    img = preprocess_card(image) if preprocess else image
    return extract_card_values(backend.read_text(img))


def read_card(
    image: np.ndarray, backend: OcrBackend, preprocess: bool = True
) -> Optional[int]:
    """OCR a single card crop -> first baccarat value (0-9), or None."""
    values = read_card_values(image, backend, preprocess)
    return values[0] if values else None


def read_cards(
    player_images: List[np.ndarray],
    banker_images: List[np.ndarray],
    backend: OcrBackend,
    preprocess: bool = True,
) -> Optional[CardReadResult]:
    """Read both hands' card crops into a :class:`CardReadResult`.

    Each side may be given as one wide band region or several slot regions; all
    rank tokens across the crops are collected. Returns ``None`` if fewer than
    the two mandatory initial cards can be read for either side (an unreliable
    read we shouldn't trust for exact counting).
    """
    player: List[int] = []
    for im in player_images:
        player.extend(read_card_values(im, backend, preprocess))
    banker: List[int] = []
    for im in banker_images:
        banker.extend(read_card_values(im, backend, preprocess))
    if len(player) < 2 or len(banker) < 2:
        return None
    ptot = baccarat_total(player)
    btot = baccarat_total(banker)
    if ptot > btot:
        winner = "P"
    elif btot > ptot:
        winner = "B"
    else:
        winner = "T"
    return CardReadResult(
        player_values=player,
        banker_values=banker,
        player_total=ptot,
        banker_total=btot,
        winner=winner,
    )
