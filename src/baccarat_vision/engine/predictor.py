"""Prediction + confidence scoring (§4.4 / §4.5).

Combines the exact composition-based probabilities with an honest confidence
meter. The confidence meter measures **how far the current shoe deviates from
the full-shoe baseline** -- NOT the chance of winning. A high reading just means
"this shoe is unusual", which is the only mathematically defensible signal
(§0). Road patterns are deliberately excluded by default (``road_weight = 0``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .probability import (
    BASELINE_P_BANKER,
    BASELINE_P_PLAYER,
    BASELINE_P_TIE,
    analyze_shoe,
)
from .shoe_state import ShoeState


@dataclass
class PredictionConfig:
    min_hands_before_predicting: int = 10
    composition_weight: float = 1.0
    road_weight: float = 0.0
    confidence_floor: float = 0.0
    confidence_ceiling: float = 1.0
    # Deviation that maps to ~100% confidence before any larger swing is seen.
    # Seeded so early hands don't read as fully-confident off one tiny shift.
    deviation_scale: float = 0.03


@dataclass
class Prediction:
    p_player: float
    p_banker: float
    p_tie: float
    confidence: float  # 0-1
    lean: str
    composition_confidence: str  # "high" | "low"
    predicting: bool  # False until min_hands threshold reached


def _lean_text(p_b: float, p_p: float, confidence: float) -> str:
    diff = p_b - p_p
    side = "Banker" if diff > 0 else "Player"
    if confidence < 0.30:
        strength = "is near baseline"
    elif confidence < 0.60:
        strength = "shifted slightly toward"
    else:
        strength = "shifted meaningfully toward"
    if confidence < 0.30:
        return f"Shoe {strength}"
    return f"Shoe {strength} {side}"


class Predictor:
    """Stateful predictor that tracks the running max deviation for confidence."""

    def __init__(self, config: PredictionConfig | None = None) -> None:
        self.config = config or PredictionConfig()
        self._max_deviation = self.config.deviation_scale

    def predict(self, shoe: ShoeState) -> Prediction:
        # A nearly-spent shoe can't be analysed (and composition is degenerate);
        # report baseline and prompt a reshuffle instead of crashing.
        if shoe.total_remaining < 20:
            return Prediction(
                p_player=BASELINE_P_PLAYER,
                p_banker=BASELINE_P_BANKER,
                p_tie=BASELINE_P_TIE,
                confidence=0.0,
                lean="Shoe nearly spent — start a new shoe",
                composition_confidence=shoe.composition_confidence,
                predicting=False,
            )
        analysis = analyze_shoe(shoe.remaining)
        p_b, p_p, p_t = analysis.p_banker, analysis.p_player, analysis.p_tie

        deviation = max(
            abs(p_b - BASELINE_P_BANKER),
            abs(p_p - BASELINE_P_PLAYER),
            abs(p_t - BASELINE_P_TIE),
        )
        # Running max (§4.5) -- confidence is deviation relative to the largest
        # swing observed this shoe (seeded with ``deviation_scale``).
        self._max_deviation = max(self._max_deviation, deviation)
        confidence = deviation / self._max_deviation if self._max_deviation else 0.0
        confidence = min(
            self.config.confidence_ceiling,
            max(self.config.confidence_floor, confidence),
        )

        predicting = shoe.hands_played >= self.config.min_hands_before_predicting
        if not predicting:
            # Not enough history: report baseline and zero confidence.
            return Prediction(
                p_player=p_p,
                p_banker=p_b,
                p_tie=p_t,
                confidence=0.0,
                lean=f"Collecting data "
                f"({shoe.hands_played}/{self.config.min_hands_before_predicting} hands)",
                composition_confidence=shoe.composition_confidence,
                predicting=False,
            )

        return Prediction(
            p_player=p_p,
            p_banker=p_b,
            p_tie=p_t,
            confidence=confidence,
            lean=_lean_text(p_b, p_p, confidence),
            composition_confidence=shoe.composition_confidence,
            predicting=True,
        )

    def reset(self) -> None:
        self._max_deviation = self.config.deviation_scale
