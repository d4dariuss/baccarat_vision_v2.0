"""Configurable side-bet payout logic.

Every bet is encoded as a *pure function* ``f(stake, outcome) -> net`` where
``net`` is the signed profit/loss on that stake for the given hand outcome
(winnings on a win, ``-stake`` on a loss, ``0.0`` on a push). Multipliers come
from a :class:`PayoutTable` built from the YAML config (§3) so a different
casino's rules can be loaded without touching code.

The payout multipliers are *net* (winnings on a $1 bet), matching the config:
e.g. ``tie: 8.0`` means a winning $1 tie bet returns +$8 profit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Dict

if TYPE_CHECKING:  # avoid a circular import; annotations are strings here.
    from .bet_spread_calc import HandOutcome

PUSH = "push"
LOSE = "lose"

# Default bonus ladder (P Bonus / B Bonus), keyed by winning margin.
_DEFAULT_BONUS_LADDER: Dict[int, float] = {9: 30.0, 8: 10.0, 7: 6.0, 6: 4.0, 5: 2.0, 4: 1.0}


@dataclass(frozen=True)
class PayoutTable:
    """Net payout multipliers for every supported bet."""

    player: float = 1.0
    banker: float = 1.0
    banker_six: float = 0.5  # EZ-Baccarat: Banker wins with 6 -> half
    tie: float = 8.0
    super_6: float = 15.0
    player_pair: float = 11.0
    banker_pair: float = 11.0
    either_pair: float = 5.0
    suited_pair_one_hand: float = 25.0
    suited_pair_both_hands: float = 200.0
    # Bonus ladders: margin -> multiplier. ``natural_win`` handled separately.
    p_bonus_natural_win: float = 1.0
    b_bonus_natural_win: float = 1.0
    p_bonus_ladder: Dict[int, float] = field(default_factory=lambda: dict(_DEFAULT_BONUS_LADDER))
    b_bonus_ladder: Dict[int, float] = field(default_factory=lambda: dict(_DEFAULT_BONUS_LADDER))

    @classmethod
    def from_config(cls, payouts: dict) -> "PayoutTable":
        """Build a table from the ``payouts:`` block of the YAML config."""

        def ladder(block: dict | None) -> Dict[int, float]:
            if not block:
                return dict(_DEFAULT_BONUS_LADDER)
            out: Dict[int, float] = {}
            mapping = {
                "win_by_9": 9, "win_by_8": 8, "win_by_7": 7,
                "win_by_6": 6, "win_by_5": 5, "win_by_4": 4,
            }
            for key, margin in mapping.items():
                val = block.get(key)
                if isinstance(val, (int, float)):
                    out[margin] = float(val)
            return out or dict(_DEFAULT_BONUS_LADDER)

        p_block = payouts.get("p_bonus", {}) or {}
        b_block = payouts.get("b_bonus", {}) or {}
        return cls(
            player=float(payouts.get("player", 1.0)),
            banker=float(payouts.get("banker", 1.0)),
            banker_six=float(payouts.get("banker_six", 0.5)),
            tie=float(payouts.get("tie", 8.0)),
            super_6=float(payouts.get("super_6", 15.0)),
            player_pair=float(payouts.get("player_pair", 11.0)),
            banker_pair=float(payouts.get("banker_pair", 11.0)),
            either_pair=float(payouts.get("either_pair", 5.0)),
            suited_pair_one_hand=float(payouts.get("suited_pair_one_hand", 25.0)),
            suited_pair_both_hands=float(payouts.get("suited_pair_both_hands", 200.0)),
            p_bonus_natural_win=float(p_block.get("natural_win", 1.0)),
            b_bonus_natural_win=float(b_block.get("natural_win", 1.0)),
            p_bonus_ladder=ladder(p_block),
            b_bonus_ladder=ladder(b_block),
        )


# --------------------------------------------------------------------------- #
# Per-bet payout functions. Each is pure: (table, stake, outcome) -> net.
# --------------------------------------------------------------------------- #
def payout_player(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    if o.winner == "T":
        return 0.0
    return stake * t.player if o.winner == "P" else -stake


def payout_banker(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    if o.winner == "T":
        return 0.0  # push
    if o.winner == "P":
        return -stake
    # Banker wins -- EZ-Baccarat carve-out on a 6.
    if o.banker_total == 6:
        return stake * t.banker_six
    return stake * t.banker


def payout_super_6(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    if o.winner == "B" and o.banker_total == 6:
        return stake * t.super_6
    return -stake


def payout_tie(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    return stake * t.tie if o.winner == "T" else -stake


def payout_player_pair(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    return stake * t.player_pair if o.p_pair else -stake


def payout_banker_pair(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    return stake * t.banker_pair if o.b_pair else -stake


def payout_either_pair(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    return stake * t.either_pair if (o.p_pair or o.b_pair) else -stake


def payout_suited_pair(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    if o.p_suited_pair and o.b_suited_pair:
        return stake * t.suited_pair_both_hands
    if o.p_suited_pair or o.b_suited_pair:
        return stake * t.suited_pair_one_hand
    return -stake


def _bonus(
    stake: float,
    o: HandOutcome,
    side: str,
    natural_win: float,
    ladder: Dict[int, float],
) -> float:
    """Shared P/B Bonus ("Dragon-style") ladder logic.

    A natural tie pushes; any other tie loses. A natural win pays the flat
    ``natural_win``. A non-natural win pays per the margin ladder; wins by 1-3
    (not in the ladder) lose.
    """
    if o.winner == "T":
        return 0.0 if o.is_natural else -stake
    if o.winner != side:
        return -stake
    if o.is_natural:
        return stake * natural_win
    mult = ladder.get(o.margin)
    return stake * mult if mult is not None else -stake


def payout_p_bonus(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    return _bonus(stake, o, "P", t.p_bonus_natural_win, t.p_bonus_ladder)


def payout_b_bonus(t: PayoutTable, stake: float, o: HandOutcome) -> float:
    return _bonus(stake, o, "B", t.b_bonus_natural_win, t.b_bonus_ladder)


# Registry: canonical bet name -> payout function. Used by the bet-spread
# calculator and the UI bet panel.
PAYOUT_FUNCTIONS: Dict[str, Callable[[PayoutTable, float, HandOutcome], float]] = {
    "player": payout_player,
    "banker": payout_banker,
    "super_6": payout_super_6,
    "tie": payout_tie,
    "player_pair": payout_player_pair,
    "banker_pair": payout_banker_pair,
    "either_pair": payout_either_pair,
    "suited_pair": payout_suited_pair,
    "p_bonus": payout_p_bonus,
    "b_bonus": payout_b_bonus,
}

# Human-readable labels for the UI.
BET_LABELS: Dict[str, str] = {
    "player": "Player",
    "banker": "Banker",
    "super_6": "Super 6",
    "tie": "Tie",
    "player_pair": "Player Pair",
    "banker_pair": "Banker Pair",
    "either_pair": "Either Pair",
    "suited_pair": "Suited Pair",
    "p_bonus": "P Bonus",
    "b_bonus": "B Bonus",
}
