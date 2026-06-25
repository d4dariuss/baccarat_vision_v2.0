"""Shoe composition tracking (§4.3).

Tracks ``remaining[value] = count`` for card values 0-9 (0 covers 10/J/Q/K,
1 = Ace). Supports two fidelity levels:

* **Exact** -- the user (or, later, card OCR) supplies the actual card values
  dealt in a hand. ``composition_confidence`` stays ``"high"``.
* **Estimated** -- only totals are visible, so we decrement an estimated number
  of cards (4-6, default 5) from a uniform prior and drop confidence to
  ``"low"`` until exact cards are observed again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from .probability import full_shoe_value_counts

# Average cards dealt per baccarat hand is ~4.94; the spec says decrement 4-6.
DEFAULT_ESTIMATED_CARDS_PER_HAND = 5


@dataclass
class ShoeState:
    """Mutable per-value card counts for the live shoe."""

    decks: int = 8
    penetration_pct: float = 75.0
    counts: List[int] = field(default_factory=lambda: list(full_shoe_value_counts(8)))
    hands_played: int = 0
    composition_confidence: str = "high"  # "high" while we see exact cards
    _exact_hands: int = 0
    _estimated_hands: int = 0

    def __post_init__(self) -> None:
        if len(self.counts) != 10:
            raise ValueError("counts must have 10 entries (values 0-9)")
        self._initial_total = sum(full_shoe_value_counts(self.decks))

    # -- queries ----------------------------------------------------------- #
    @property
    def remaining(self) -> Tuple[int, ...]:
        return tuple(self.counts)

    @property
    def total_remaining(self) -> int:
        return sum(self.counts)

    @property
    def cards_dealt(self) -> int:
        return self._initial_total - self.total_remaining

    @property
    def penetration(self) -> float:
        """Fraction of the shoe dealt so far, 0-1."""
        return self.cards_dealt / self._initial_total if self._initial_total else 0.0

    @property
    def needs_reshuffle(self) -> bool:
        return self.penetration * 100.0 >= self.penetration_pct

    @property
    def can_predict(self) -> bool:
        return self.total_remaining >= 6

    # -- mutations --------------------------------------------------------- #
    def remove_card(self, value: int) -> None:
        if not 0 <= value <= 9:
            raise ValueError(f"value must be 0-9, got {value}")
        if self.counts[value] <= 0:
            raise ValueError(f"no cards of value {value} remain to remove")
        self.counts[value] -= 1

    def record_hand_exact(self, card_values: List[int]) -> None:
        """Record a hand whose individual card values are known.

        Tolerant of a depleted/invalid value (a bad read or end-of-shoe must not
        crash the live loop): it removes what it can and skips the rest.
        """
        for v in card_values:
            if 0 <= v <= 9 and self.counts[v] > 0:
                self.counts[v] -= 1
        self.hands_played += 1
        self._exact_hands += 1
        self.composition_confidence = "high"

    def record_hand_estimated(
        self, n_cards: int = DEFAULT_ESTIMATED_CARDS_PER_HAND
    ) -> None:
        """Record a hand without known cards: remove ``n_cards`` proportionally.

        Cards are removed from a uniform prior (proportional to current counts)
        so the composition stays plausible, and confidence drops to ``"low"``.
        """
        self._remove_proportional(n_cards)
        self.hands_played += 1
        self._estimated_hands += 1
        self.composition_confidence = "low"

    def _remove_proportional(self, n: int) -> None:
        total = self.total_remaining
        if total <= n:
            return
        # Largest-remainder apportionment of n removals across values.
        exact = [n * c / total for c in self.counts]
        removals = [int(x) for x in exact]
        shortfall = n - sum(removals)
        # Hand out the remaining removals to the largest fractional parts.
        order = sorted(
            range(10), key=lambda v: exact[v] - removals[v], reverse=True
        )
        for v in order:
            if shortfall <= 0:
                break
            if removals[v] < self.counts[v]:
                removals[v] += 1
                shortfall -= 1
        for v in range(10):
            self.counts[v] = max(0, self.counts[v] - removals[v])

    def burn(self, n: int) -> None:
        """Remove ``n`` cards (e.g. the shoe-start burn) without counting a hand.

        Burned card values are unknown, so composition confidence drops to low.
        """
        if n <= 0:
            return
        self._remove_proportional(n)
        self.composition_confidence = "low"

    def reset(self) -> None:
        """Reshuffle: restore a full shoe."""
        self.counts = list(full_shoe_value_counts(self.decks))
        self.hands_played = 0
        self._exact_hands = 0
        self._estimated_hands = 0
        self.composition_confidence = "high"
