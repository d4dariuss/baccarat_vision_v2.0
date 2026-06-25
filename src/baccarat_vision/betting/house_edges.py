"""Runtime house-edge computation (§4.2).

Every edge is *computed*, never hard-coded: bet payouts are integrated against
the exact shoe distribution (for winner/total/natural bets) or the closed-form
pair probabilities (for pair/suited bets). ``house_edge = -EV`` per $1 staked.

Discrepancy note (surfaced in the README): the spec's §4.2 table lists Super 6
at 13.66% and Either Pair at 14.20%. The mathematically correct figures for the
confirmed 8-deck payouts are **13.82%** and **13.71%** respectively (these match
the Wizard of Odds appendix; the spec's own Super 6 arithmetic even works out to
-13.76%). We report the computed truth and keep the honest-disclaimer spirit of
§0 rather than reproduce the transcription errors.
"""

from __future__ import annotations

from typing import Dict

from ..engine.probability import (
    analyze_shoe,
    either_pair_probability,
    full_shoe_value_counts,
    pair_probability,
    suited_pair_states,
)
from .bet_spread_calc import HandOutcome
from .payout_table import (
    PayoutTable,
    payout_b_bonus,
    payout_banker,
    payout_p_bonus,
    payout_player,
    payout_super_6,
    payout_tie,
)


def _integrate_value_bet(fn, table: PayoutTable, analysis) -> float:
    """EV per $1 for a bet that depends only on winner/total/natural."""
    ev = 0.0
    for (winner, ptot, btot, is_nat), prob in analysis.distribution.items():
        o = HandOutcome(
            winner=winner,
            player_total=ptot,
            banker_total=btot,
            is_natural=is_nat,
        )
        ev += prob * fn(table, 1.0, o)
    return ev


def compute_house_edges(table: PayoutTable, decks: int = 8) -> Dict[str, float]:
    """Return ``{bet_name: house_edge_fraction}`` for the full shoe.

    A positive value is the casino's edge (e.g. ``0.0146`` == 1.46%).
    """
    analysis = analyze_shoe(full_shoe_value_counts(decks))

    edges: Dict[str, float] = {}
    edges["player"] = -_integrate_value_bet(payout_player, table, analysis)
    edges["banker"] = -_integrate_value_bet(payout_banker, table, analysis)
    edges["tie"] = -_integrate_value_bet(payout_tie, table, analysis)
    edges["super_6"] = -_integrate_value_bet(payout_super_6, table, analysis)
    edges["p_bonus"] = -_integrate_value_bet(payout_p_bonus, table, analysis)
    edges["b_bonus"] = -_integrate_value_bet(payout_b_bonus, table, analysis)

    p_pair = pair_probability(decks)
    edges["player_pair"] = -(p_pair * table.player_pair - (1 - p_pair))
    edges["banker_pair"] = -(p_pair * table.banker_pair - (1 - p_pair))

    p_either = either_pair_probability(decks)
    edges["either_pair"] = -(p_either * table.either_pair - (1 - p_either))

    p_neither, p_one, p_both = suited_pair_states(decks)
    suited_ev = (
        p_one * table.suited_pair_one_hand
        + p_both * table.suited_pair_both_hands
        - p_neither
    )
    edges["suited_pair"] = -suited_ev

    return edges
