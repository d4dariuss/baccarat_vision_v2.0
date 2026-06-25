"""Bet-spread calculator (§5).

Given a spread ``{bet_name: stake}`` this produces, for the current shoe
composition, the full outcome matrix: each distinct net result with the
probability mass that produces it, plus total-at-risk, EV, best/worst case and
volatility.

The net-per-outcome math is exact and is what the §5.3 worked examples test.
The probability mass attached to each outcome comes from the exact value-level
distribution (:class:`~baccarat_vision.engine.probability.ShoeAnalysis`); pair
states are layered on with their marginal probabilities, treated as independent
of the value outcome. That independence is an approximation (a first-two-card
pair weakly correlates with the totals) and is flagged in the UI as such -- it
only affects the *probability* column, never the net payouts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Literal, Tuple

Winner = Literal["P", "B", "T"]


@dataclass(frozen=True)
class HandOutcome:
    """A fully-described baccarat hand (§5.1)."""

    winner: Winner
    player_total: int  # 0-9
    banker_total: int  # 0-9
    is_natural: bool  # either side dealt 8 or 9 on the first two cards
    p_pair: bool = False
    b_pair: bool = False
    p_suited_pair: bool = False  # implies p_pair
    b_suited_pair: bool = False  # implies b_pair

    def __post_init__(self) -> None:
        if self.p_suited_pair and not self.p_pair:
            raise ValueError("p_suited_pair implies p_pair")
        if self.b_suited_pair and not self.b_pair:
            raise ValueError("b_suited_pair implies b_pair")
        if self.winner == "T" and self.player_total != self.banker_total:
            raise ValueError("a tie must have equal totals")
        if self.winner == "P" and self.player_total <= self.banker_total:
            raise ValueError("player win requires player_total > banker_total")
        if self.winner == "B" and self.banker_total <= self.player_total:
            raise ValueError("banker win requires banker_total > player_total")

    @property
    def margin(self) -> int:
        return abs(self.player_total - self.banker_total)


@dataclass
class OutcomeRow:
    """One row of the grouped outcome matrix."""

    net: float
    probability: float
    label: str
    is_best: bool = False
    is_worst: bool = False


@dataclass
class SpreadResult:
    """Full calculator output for one bet spread (§5.4)."""

    rows: List[OutcomeRow]
    total_at_risk: float
    expected_value: float
    best_case: float
    worst_case: float
    volatility: float
    bets: Dict[str, float] = field(default_factory=dict)


class BetSpreadCalculator:
    """Computes net payouts and the outcome matrix for a bet spread."""

    def __init__(self, table) -> None:  # table: PayoutTable
        self.table = table

    def net(self, spread: Dict[str, float], outcome: HandOutcome) -> float:
        """Net profit/loss across all bets in ``spread`` for one outcome."""
        from .payout_table import PAYOUT_FUNCTIONS  # lazy: avoids import cycle

        total = 0.0
        for bet, stake in spread.items():
            if not stake:
                continue
            fn = PAYOUT_FUNCTIONS.get(bet)
            if fn is None:
                raise KeyError(f"unknown bet: {bet!r}")
            total += fn(self.table, stake, outcome)
        return total

    def evaluate(
        self,
        spread: Dict[str, float],
        distribution: Iterable[Tuple[HandOutcome, float]],
    ) -> SpreadResult:
        """Build the grouped outcome matrix and summary stats.

        ``distribution`` is an iterable of ``(HandOutcome, probability)``.
        Rows are grouped by identical net result (rounded to the cent).
        """
        active_bonus = any(
            spread.get(b) for b in ("p_bonus", "b_bonus")
        )
        grouped: Dict[int, float] = defaultdict(float)  # net-cents -> prob
        labels: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        ev = 0.0
        for outcome, prob in distribution:
            if prob == 0:
                continue
            net = self.net(spread, outcome)
            ev += prob * net
            key = round(net * 100)
            grouped[key] += prob
            labels[key][_describe(outcome, active_bonus)] += prob

        rows: List[OutcomeRow] = []
        for key, prob in grouped.items():
            net = key / 100.0
            # Pick the most-probable description as the row label.
            label = max(labels[key].items(), key=lambda kv: kv[1])[0]
            rows.append(OutcomeRow(net=net, probability=prob, label=label))

        rows.sort(key=lambda r: r.net, reverse=True)
        variance = sum(r.probability * (r.net - ev) ** 2 for r in rows)
        total_at_risk = sum(s for s in spread.values() if s)

        if rows:
            best = max(rows, key=lambda r: r.net)
            worst = min(rows, key=lambda r: r.net)
            best.is_best = True
            worst.is_worst = True
            best_case, worst_case = best.net, worst.net
        else:
            best_case = worst_case = 0.0

        return SpreadResult(
            rows=rows,
            total_at_risk=total_at_risk,
            expected_value=ev,
            best_case=best_case,
            worst_case=worst_case,
            volatility=variance ** 0.5,
            bets=dict(spread),
        )


def distribution_from_analysis(
    analysis, decks: int = 8
) -> List[Tuple[HandOutcome, float]]:
    """Expand a value-level :class:`ShoeAnalysis` into a full outcome list.

    Each value outcome ``(winner, p_total, b_total, is_natural)`` is split
    across the nine combinations of player/banker pair-state
    (none / unsuited pair / suited pair), weighted by the marginal pair
    probabilities and treated as independent of the value outcome.
    """
    from ..engine.probability import pair_probability, suited_pair_probability

    p_pair = pair_probability(decks)
    p_suited = suited_pair_probability(decks)
    # Per-hand pair-state probabilities.
    states = (
        (False, False, 1.0 - p_pair),          # no pair
        (True, False, p_pair - p_suited),       # unsuited pair
        (True, True, p_suited),                 # suited pair
    )

    out: List[Tuple[HandOutcome, float]] = []
    for (winner, ptot, btot, is_nat), q in analysis.distribution.items():
        for p_has, p_suit, pw in states:
            for b_has, b_suit, bw in states:
                prob = q * pw * bw
                if prob == 0:
                    continue
                out.append(
                    (
                        HandOutcome(
                            winner=winner,
                            player_total=ptot,
                            banker_total=btot,
                            is_natural=is_nat,
                            p_pair=p_has,
                            b_pair=b_has,
                            p_suited_pair=p_suit,
                            b_suited_pair=b_suit,
                        ),
                        prob,
                    )
                )
    return out


def _describe(o: HandOutcome, active_bonus: bool) -> str:
    """Short human label for an outcome, used to name grouped rows."""
    if o.winner == "T":
        base = "Tie (natural)" if o.is_natural else "Tie"
    elif o.winner == "P":
        base = "Player win"
    else:
        base = "Super 6 (Banker 6)" if o.banker_total == 6 else "Banker win"
    if active_bonus and o.winner in ("P", "B") and not o.is_natural:
        base += f" by {o.margin}"
    tags = []
    if o.p_pair:
        tags.append("P pair")
    if o.b_pair:
        tags.append("B pair")
    if tags:
        base += " + " + " & ".join(tags)
    return base
