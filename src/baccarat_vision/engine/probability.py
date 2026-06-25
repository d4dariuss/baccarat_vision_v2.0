"""Exact baccarat probability engine.

This module computes *honest* next-hand probabilities directly from the
current shoe composition by exhaustively enumerating every reachable deal
following the official baccarat drawing rules. No Monte-Carlo error: the
numbers are exact (to floating-point) for any given composition.

Design notes
------------
* Cards are tracked by **value** (0-9). Value 0 covers 10/J/Q/K, value 1 is
  the Ace, values 2-9 are themselves. This is all that P/B/T outcomes depend
  on, so it keeps the enumeration to a 10-way branch per draw.
* The full enumeration is cached (``functools.lru_cache``) keyed on the
  composition tuple, so repeated lookups for the same shoe are free.
* "Effects of removal" (Thorp/Griffin, §4.4.1) are derived at runtime by
  diffing the exact full-shoe result against the result with a single card
  removed -- never hard-coded. ``approx_probabilities`` uses these for the
  fast linear estimate that the live loop can afford every hand.

Pair / suited-pair probabilities depend on the 13 *ranks* (and 52 distinct
cards) rather than the 10 values, so they are computed separately with closed
forms in :mod:`.pairs`-style helpers at the bottom of this file.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from math import comb
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------- #
# Baseline constants (8-deck full shoe). Source: Thorp (1984), reproduced in
# the Wizard of Odds 8-deck analysis. These are the values the test-suite pins
# the runtime computation against (§4.1 / §9).
# --------------------------------------------------------------------------- #
BASELINE_P_BANKER = 0.458597
BASELINE_P_PLAYER = 0.446247
BASELINE_P_TIE = 0.095156

CARDS_PER_DECK = 52
VALUE_LABELS = ["0/T/J/Q/K", "A", "2", "3", "4", "5", "6", "7", "8", "9"]


def full_shoe_value_counts(decks: int = 8) -> Tuple[int, ...]:
    """Return the per-value card counts for a full ``decks``-deck shoe.

    Index = card value (0-9). Value 0 (ten/J/Q/K) has 16 cards per deck; each
    of the values 1-9 has 4 cards per deck.
    """
    counts = [0] * 10
    counts[0] = 16 * decks
    for v in range(1, 10):
        counts[v] = 4 * decks
    return tuple(counts)


# --------------------------------------------------------------------------- #
# Drawing rules
# --------------------------------------------------------------------------- #
def _banker_draws(banker_total: int, player_third: int | None) -> bool:
    """Official banker third-card rule.

    ``player_third`` is ``None`` when the player stood (no third card).
    """
    if player_third is None:
        # Player stood -> banker plays like the player: draw on 0-5.
        return banker_total <= 5
    if banker_total <= 2:
        return True
    if banker_total == 3:
        return player_third != 8
    if banker_total == 4:
        return 2 <= player_third <= 7
    if banker_total == 5:
        return 4 <= player_third <= 7
    if banker_total == 6:
        return 6 <= player_third <= 7
    return False  # banker total 7 stands


@dataclass(frozen=True)
class ShoeAnalysis:
    """Aggregated outcome distribution for a given shoe composition."""

    p_player: float
    p_banker: float
    p_tie: float
    # P(banker wins with a two-or-three-card total of exactly 6) -- drives the
    # EZ-Baccarat carve-out and the Super 6 side bet.
    p_banker_win_six: float
    p_natural: float
    # distribution keyed by (winner, player_total, banker_total, is_natural)
    distribution: Dict[Tuple[str, int, int, bool], float]

    @property
    def margin_summary(self) -> str:
        return (
            f"P={self.p_player:.6f} B={self.p_banker:.6f} "
            f"T={self.p_tie:.6f} (B6={self.p_banker_win_six:.6f})"
        )


def _analyze(counts: Tuple[int, ...]) -> ShoeAnalysis:
    """Exhaustively enumerate every reachable deal from ``counts``.

    Returns a :class:`ShoeAnalysis`. This is the exact, expensive computation;
    callers should use the cached :func:`analyze_shoe` wrapper.
    """
    c = list(counts)
    n0 = sum(c)
    dist: Dict[Tuple[str, int, int, bool], float] = defaultdict(float)

    def record(prob: float, ptot: int, btot: int, is_nat: bool) -> None:
        if ptot > btot:
            winner = "P"
        elif btot > ptot:
            winner = "B"
        else:
            winner = "T"
        dist[(winner, ptot, btot, is_nat)] += prob

    # Deal order: P1, B1, P2, B2, (P3), (B3). Each draw is without replacement.
    for p1 in range(10):
        if c[p1] == 0:
            continue
        w1 = c[p1] / n0
        c[p1] -= 1
        n1 = n0 - 1
        for b1 in range(10):
            if c[b1] == 0:
                continue
            w2 = w1 * c[b1] / n1
            c[b1] -= 1
            n2 = n1 - 1
            for p2 in range(10):
                if c[p2] == 0:
                    continue
                w3 = w2 * c[p2] / n2
                c[p2] -= 1
                n3 = n2 - 1
                for b2 in range(10):
                    if c[b2] == 0:
                        continue
                    w4 = w3 * c[b2] / n3
                    c[b2] -= 1
                    n4 = n3 - 1

                    ptot = (p1 + p2) % 10
                    btot = (b1 + b2) % 10
                    is_nat = ptot in (8, 9) or btot in (8, 9)

                    if is_nat:
                        record(w4, ptot, btot, True)
                    elif ptot <= 5:
                        # Player draws a third card.
                        for p3 in range(10):
                            if c[p3] == 0:
                                continue
                            w5 = w4 * c[p3] / n4
                            c[p3] -= 1
                            n5 = n4 - 1
                            new_ptot = (ptot + p3) % 10
                            if _banker_draws(btot, p3):
                                for b3 in range(10):
                                    if c[b3] == 0:
                                        continue
                                    w6 = w5 * c[b3] / n5
                                    record(w6, new_ptot, (btot + b3) % 10, False)
                            else:
                                record(w5, new_ptot, btot, False)
                            c[p3] += 1
                    else:
                        # Player stands (6 or 7); banker may still draw.
                        if _banker_draws(btot, None):
                            for b3 in range(10):
                                if c[b3] == 0:
                                    continue
                                w5 = w4 * c[b3] / n4
                                record(w5, ptot, (btot + b3) % 10, False)
                        else:
                            record(w4, ptot, btot, False)

                    c[b2] += 1
                c[p2] += 1
            c[b1] += 1
        c[p1] += 1

    p_player = p_banker = p_tie = p_b6 = p_nat = 0.0
    for (winner, ptot, btot, is_nat), prob in dist.items():
        if winner == "P":
            p_player += prob
        elif winner == "B":
            p_banker += prob
            if btot == 6:
                p_b6 += prob
        else:
            p_tie += prob
        if is_nat:
            p_nat += prob

    return ShoeAnalysis(
        p_player=p_player,
        p_banker=p_banker,
        p_tie=p_tie,
        p_banker_win_six=p_b6,
        p_natural=p_nat,
        distribution=dict(dist),
    )


@lru_cache(maxsize=256)
def analyze_shoe(counts: Tuple[int, ...]) -> ShoeAnalysis:
    """Cached exact analysis for a shoe given by per-value ``counts`` (len 10)."""
    if len(counts) != 10:
        raise ValueError("counts must have exactly 10 entries (values 0-9)")
    if sum(counts) < 4:
        raise ValueError("need at least 4 cards remaining to deal a hand")
    return _analyze(counts)


# --------------------------------------------------------------------------- #
# Effects of removal (linear approximation, §4.4.1)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=4)
def effects_of_removal(decks: int = 8) -> Tuple[Tuple[float, float, float], ...]:
    """Per-value effect on (P_player, P_banker, P_tie) of removing one card.

    Derived exactly by diffing the full-shoe analysis against the analysis
    with a single card of each value removed. Returned as a 10-tuple of
    ``(dP, dB, dT)`` deltas (probability change per single card removed).
    """
    full = full_shoe_value_counts(decks)
    base = analyze_shoe(full)
    effects: List[Tuple[float, float, float]] = []
    for v in range(10):
        if full[v] == 0:
            effects.append((0.0, 0.0, 0.0))
            continue
        reduced = list(full)
        reduced[v] -= 1
        a = analyze_shoe(tuple(reduced))
        effects.append(
            (
                a.p_player - base.p_player,
                a.p_banker - base.p_banker,
                a.p_tie - base.p_tie,
            )
        )
    return tuple(effects)


def approx_probabilities(
    counts: Tuple[int, ...], decks: int = 8
) -> Tuple[float, float, float]:
    """Fast linear estimate of (P_player, P_banker, P_tie) from removals.

    Uses the runtime-derived effects of removal. Accurate near a full shoe and
    cheap enough to run every hand; for an exact figure call
    :func:`analyze_shoe` directly.
    """
    full = full_shoe_value_counts(decks)
    base = analyze_shoe(full)
    eor = effects_of_removal(decks)
    dp = db = dt = 0.0
    for v in range(10):
        removed = full[v] - counts[v]
        dp += removed * eor[v][0]
        db += removed * eor[v][1]
        dt += removed * eor[v][2]
    p = base.p_player + dp
    b = base.p_banker + db
    t = base.p_tie + dt
    # Renormalise to guard against linear drift summing away from 1.
    total = p + b + t
    return (p / total, b / total, t / total)


# --------------------------------------------------------------------------- #
# Pair / suited-pair probabilities (13 ranks / 52 distinct cards)
# --------------------------------------------------------------------------- #
def pair_probability(decks: int = 8) -> float:
    """P(a given two-card hand is a rank pair), full shoe."""
    per_rank = 4 * decks
    total = CARDS_PER_DECK * decks
    return 13 * comb(per_rank, 2) / comb(total, 2)


def both_pairs_probability(decks: int = 8) -> float:
    """P(player hand AND banker hand are each a rank pair)."""
    per_rank = 4 * decks
    total = CARDS_PER_DECK * decks
    p_player_pair = 13 * comb(per_rank, 2) / comb(total, 2)
    # Given the player pair (2 cards of some rank gone), banker pair from the
    # remaining 4 cards: same rank now has per_rank-2 left, other 12 ranks full.
    second = (comb(per_rank - 2, 2) + 12 * comb(per_rank, 2)) / comb(total - 2, 2)
    return p_player_pair * second


def either_pair_probability(decks: int = 8) -> float:
    """P(at least one of the two hands is a rank pair)."""
    p = pair_probability(decks)
    return 2 * p - both_pairs_probability(decks)


def suited_pair_probability(decks: int = 8) -> float:
    """P(a given two-card hand is a *suited* pair -- same rank and suit)."""
    total = CARDS_PER_DECK * decks
    # 52 distinct cards, each present ``decks`` times.
    return 52 * comb(decks, 2) / comb(total, 2)


def suited_pair_states(decks: int = 8) -> Tuple[float, float, float]:
    """Return (P_neither, P_exactly_one, P_both) suited pairs across both hands."""
    total = CARDS_PER_DECK * decks
    p_one_hand = 52 * comb(decks, 2) / comb(total, 2)
    # Both hands suited pairs: player suited pair of some card, banker suited
    # pair of another (or same) distinct card.
    second_both = (
        comb(decks - 2, 2) + 51 * comb(decks, 2)
    ) / comb(total - 2, 2)
    p_both = p_one_hand * second_both
    p_exactly_one = 2 * p_one_hand - 2 * p_both  # symmetric inclusion-exclusion
    p_neither = 1 - p_exactly_one - p_both
    return (p_neither, p_exactly_one, p_both)
