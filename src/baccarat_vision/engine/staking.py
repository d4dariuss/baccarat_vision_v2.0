"""Bankroll-aware stake sizing + bet-spread suggestion.

Given the model's confidence, the player's live balance + currency (GC/SC), and
the chip denominations the table offers, this recommends a stake for the main
gameline bet (snapped to a real denomination) plus any side bets that are "due"
or showing a learned edge. Supports confidence-scaled (default), flat, and
Martingale staking, and tracks a running suggested-bankroll as the shoe plays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Named multi-bet spreads, keyed by the favoured gameline side. Each leg is a
# whole number of units (1 unit = the smallest chip). These are the player's
# preferred spreads — a heavy gameline bet plus a couple of long-shot side bets,
# since side bets, when they land, carry a big share of the session's profit.
SPREAD_PRESETS: Dict[str, Dict[str, object]] = {
    "banker": {"label": "Banker spread", "legs": {"banker": 7, "tie": 1, "super_6": 1, "b_bonus": 1}},
    "player": {"label": "Player spread", "legs": {"player": 7, "tie": 2, "p_bonus": 1}},
}
_LEG_LABELS = {
    "banker": "Banker", "player": "Player", "tie": "Tie", "super_6": "Super 6",
    "b_bonus": "B Bonus", "p_bonus": "P Bonus",
    "either_pair": "Either Pair", "player_pair": "P Pair", "banker_pair": "B Pair",
}


@dataclass
class StakeSuggestion:
    main_bet: str
    main_label: str
    stake: float
    unit: float
    strategy: str
    currency: str = ""
    note: str = ""
    side_bets: List[dict] = field(default_factory=list)  # {bet,label,stake,reason}
    spread_label: str = ""
    spread_legs: List[dict] = field(default_factory=list)  # {bet,label,units,stake}
    spread_total: float = 0.0
    spread_affordable: bool = True

    def total(self) -> float:
        return self.stake + sum(s["stake"] for s in self.side_bets)


def kelly_stake(
    p_win: float,
    net_odds: float,
    bankroll: float,
    fraction: float = 0.25,
    min_bet: float = 0.0,
) -> float:
    """Fractional Kelly stake from estimated edge.

    fraction=0.25 (quarter-Kelly) reduces variance for model uncertainty.
    Returns 0 when the Kelly fraction is negative (no edge on this side).
    """
    if p_win <= 0 or bankroll <= 0 or net_odds <= 0:
        return 0.0
    q = 1.0 - p_win
    kelly_f = (net_odds * p_win - q) / net_odds
    if kelly_f <= 0:
        return 0.0
    return max(min_bet, fraction * kelly_f * bankroll)


def build_spread(side: str, unit: float, balance: float) -> Tuple[str, List[dict], float, bool]:
    """Scale the named preset for ``side`` by the chip ``unit``.

    Returns (label, legs, total, affordable). Legs are whole-unit multiples so
    they always land on real chip stacks; the gameline leg is listed first.
    """
    preset = SPREAD_PRESETS.get(side)
    if not preset:
        return "", [], 0.0, True
    legs: List[dict] = []
    total = 0.0
    for bet, units in preset["legs"].items():  # type: ignore[index]
        stake = units * unit
        legs.append({"bet": bet, "label": _LEG_LABELS.get(bet, bet),
                     "units": units, "stake": stake})
        total += stake
    affordable = (not balance) or total <= balance
    return str(preset["label"]), legs, total, affordable


def snap_to_denoms(amount: float, denoms: List[float]) -> float:
    """Largest available denomination <= amount (else the smallest chip)."""
    if not denoms:
        return amount
    usable = [d for d in denoms if d <= amount]
    return max(usable) if usable else min(denoms)


def suggest_stake(
    *,
    main_bet: str,
    main_label: str,
    confident: bool,
    vibe: float,
    balance: float,
    denoms: List[float],
    min_bet: float,
    max_bet: float,
    strategy: str,
    consec_losses: int,
    currency: str,
    side_suggestions: List[Tuple[str, str, str]],
) -> StakeSuggestion:
    denoms = sorted(d for d in denoms if d and d > 0)
    base = denoms[0] if denoms else (min_bet or 1.0)
    cap = max_bet or (balance or base)
    if balance:
        cap = min(cap, balance)

    if strategy == "flat":
        target, note = base, "Flat — 1 unit"
    elif strategy == "martingale":
        target = base * (2 ** max(0, consec_losses))
        note = f"Martingale ×{2 ** max(0, consec_losses)} ({consec_losses} losses)"
    else:  # confidence-scaled (default): small when unsure, bigger on a real edge
        if confident:
            mult = 1.0 + 2.0 * max(0.0, vibe - 0.4)  # ~1x .. ~2.2x
            target = base * (1.0 + mult)
            note = f"Confidence ↑ (~{1.0 + mult:.1f}×)"
        else:
            target, note = base, "Low confidence — min unit"

    # Chips stack, so a stake is a whole multiple of the smallest unit (not a
    # single denomination). Round to the unit, clamp to [min, cap/balance].
    stake = round(min(target, cap) / base) * base
    stake = max(stake, min_bet or base)
    if balance:
        stake = min(stake, balance)

    sides = []
    for bet, label, reason in side_suggestions:
        # Target ~1/7 of the gameline (same ratio as the preset 7:1:1 spread),
        # snapped DOWN to the largest denomination that fits. Hard cap at the
        # gameline stake so a side bet never exceeds the main bet.
        target_s = stake / 7.0
        usable = [d for d in denoms if d <= target_s] if denoms else []
        s = max(usable) if usable else min(base, stake)
        s = min(s, stake)
        if s <= 0:
            continue
        if balance and stake + sum(x["stake"] for x in sides) + s > balance:
            break
        sides.append({"bet": bet, "label": label, "stake": s, "reason": reason})

    # Always attach the named spread for the favoured gameline side, scaled to
    # the player's unit/currency, so the overlay can show it ready to place.
    spread_label, spread_legs, spread_total, affordable = build_spread(
        main_bet, base, balance
    )

    return StakeSuggestion(
        main_bet=main_bet, main_label=main_label, stake=stake, unit=base,
        strategy=strategy, currency=currency, note=note, side_bets=sides,
        spread_label=spread_label, spread_legs=spread_legs,
        spread_total=spread_total, spread_affordable=affordable,
    )
