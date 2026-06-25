"""Probability-aware dynamic bet-spread engine.

Computes a per-hand multi-leg bet spread by blending three independent signals:

  1. **Composition signal** — how far p_banker / p_player has shifted from the
     full-shoe baseline.  Derived from the exact shoe analysis, so it responds
     to actual card removal, not estimation.
  2. **Learner signal** — the online ensemble's recent profit/hand (regime-
     weighted).  Zero until the BET gate unlocks; rises as the learner builds
     a track record in the current shoe.
  3. **Pattern signal** — the weighted vote share the ensemble gave to the
     recommended side this hand.

Combined signal drives a unit multiplier that scales from 1× (flat minimum)
up to a phase-dependent ceiling:

  • Early shoe (penetration < 25%): max 2×  — high uncertainty, conserve chips.
  • Mid shoe   (25% – 55%): max 4×            — composition data accumulating.
  • Late shoe  (55%+): max 6×                 — strongest composition signal.

Companion bets (Super 6, Banker/Player Bonus) are always included alongside
their gameline bet at 1 unit each — they are complements to the main bet, not
independent signals.  Super 6 pays 15:1 on a Banker-wins-by-6; Bonus pays up
to 30:1 on a large-margin win.  EV for both is computed from the live shoe
distribution so the numbers stay honest as the shoe depletes.

Tie and Either Pair are still opportunistic — only added when their probability
is at least SIDE_BET_THRESHOLD above the full-shoe baseline or the learner has
recorded a significant edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .probability import (
    BASELINE_P_BANKER,
    BASELINE_P_PLAYER,
    BASELINE_P_TIE,
    analyze_shoe,
    full_shoe_value_counts,
    live_pair_probabilities,
)

# ── Baseline constants (computed once, cached) ───────────────────────────── #
_FULL_COUNTS = full_shoe_value_counts(8)


def _baseline_b6() -> float:
    return analyze_shoe(_FULL_COUNTS).p_banker_win_six


# ── Tuning knobs ─────────────────────────────────────────────────────────── #
# Composition: 1.2% shift in the favoured side's probability = full signal.
_COMP_SCALE = 0.012

# Learner: profit/hand of 0.30 units = full signal.
_LEARNER_PPH_SCALE = 0.30

# Side bet: probability must exceed baseline by at least this fraction.
SIDE_BET_THRESHOLD = 0.04   # 4 % above baseline

# Unit multiplier ceilings by shoe phase.
_PHASE_CAPS = {"early": 2.0, "mid": 4.0, "late": 6.0}

# Signal weights (must sum to 1.0).
_W_COMP = 0.35
_W_LEARN = 0.40
_W_PATT = 0.25

_LEG_LABELS = {
    "banker": "Banker", "player": "Player", "tie": "Tie",
    "super_6": "Super 6", "either_pair": "Either Pair",
    "player_pair": "P Pair", "banker_pair": "B Pair",
    "p_bonus": "P Bonus", "b_bonus": "B Bonus",
}


# ── Output types ─────────────────────────────────────────────────────────── #
@dataclass
class SpreadLeg:
    """One bet in the recommended spread."""
    bet: str
    label: str
    stake: float    # currency amount
    units: float    # stake / unit
    ev: float       # estimated EV for this leg at the current shoe
    reason: str


@dataclass
class DynamicSpread:
    """Full dynamic bet-spread recommendation for the next hand."""
    main_bet: str
    main_label: str
    legs: List[SpreadLeg]
    total_stake: float
    total_ev: float
    unit: float
    phase: str                   # "early" | "mid" | "late"
    signal: float                # 0-1 combined signal
    composition_signal: float    # 0-1 component
    learner_signal: float        # 0-1 component
    pattern_signal: float        # 0-1 component
    multiplier: float            # units on main bet (snapped to 0.5)
    note: str
    currency: str = ""
    affordable: bool = True
    pair_probs: dict = field(default_factory=dict)   # live pair probabilities
    kelly_stake: float = 0.0                          # 25%-Kelly stake (in currency)
    kelly_fraction: float = 0.0                       # raw Kelly fraction


# ── Bonus payout ladder (default; mirrors PayoutTable defaults) ───────────── #
_BONUS_LADDER = {9: 30.0, 8: 10.0, 7: 6.0, 6: 4.0, 5: 2.0, 4: 1.0}
_BONUS_NATURAL_WIN = 1.0   # natural win pays 1:1


# ── EV helpers (1-unit bets, ties push on gameline) ──────────────────────── #
def _ev_banker(p_b: float, p_p: float) -> float:
    return p_b * 0.95 - p_p


def _ev_player(p_p: float, p_b: float) -> float:
    return p_p - p_b


def _ev_tie(p_t: float) -> float:
    return p_t * 8.0 - (1.0 - p_t)


def _ev_super6(p_b6: float) -> float:
    return p_b6 * 15.0 - (1.0 - p_b6)


def _ev_either_pair(p_ep: float) -> float:
    return p_ep * 5.0 - (1.0 - p_ep)


def _ev_pair(p_pair: float) -> float:
    """EV for player_pair or banker_pair bet (pays 11:1)."""
    return p_pair * 11.0 - (1.0 - p_pair)


def _ev_b_bonus(distribution: dict) -> float:
    """EV per unit for Banker Bonus using the live shoe distribution.

    Pays up to 30:1 on a Banker win by 9 (non-natural); natural Banker win
    pays 1:1; wins by 1-3 lose; non-natural tie loses.
    """
    ev = 0.0
    for (winner, ptot, btot, is_nat), prob in distribution.items():
        if winner == "T":
            if not is_nat:
                ev -= prob
        elif winner != "B":
            ev -= prob
        elif is_nat:
            ev += prob * _BONUS_NATURAL_WIN
        else:
            mult = _BONUS_LADDER.get(abs(btot - ptot))
            ev += prob * mult if mult is not None else -prob
    return ev


def _ev_p_bonus(distribution: dict) -> float:
    """EV per unit for Player Bonus using the live shoe distribution."""
    ev = 0.0
    for (winner, ptot, btot, is_nat), prob in distribution.items():
        if winner == "T":
            if not is_nat:
                ev -= prob
        elif winner != "P":
            ev -= prob
        elif is_nat:
            ev += prob * _BONUS_NATURAL_WIN
        else:
            mult = _BONUS_LADDER.get(abs(ptot - btot))
            ev += prob * mult if mult is not None else -prob
    return ev


def _snap(amount: float, unit: float) -> float:
    """Round amount to the nearest whole unit multiple (floor at 1 unit)."""
    if unit <= 0:
        return amount
    return max(unit, round(amount / unit) * unit)


# ── Main engine ───────────────────────────────────────────────────────────── #
def compute_dynamic_spread(
    *,
    analysis,                    # ShoeAnalysis from analyze_shoe()
    prediction,                  # Prediction from Predictor.predict()
    learning,                    # Scoreboard from OnlineLearner.scoreboard()
    mystic,                      # MysticAdvice from controller.snapshot()
    penetration: float,
    balance: float,
    denoms: List[float],
    min_bet: float,
    max_bet: float,
    currency: str,
    counts: tuple = (),          # current shoe value counts (len 10) for live pair probs
    decks: int = 8,
) -> DynamicSpread:
    """Compute a probability-driven bet spread for the next hand.

    Parameters mirror the data already available in ``AppController.snapshot()``.
    All monetary values are in the table's currency unit (GC or SC).
    """
    # ── Unit + cap ───────────────────────────────────────────────────────── #
    denoms = sorted(d for d in (denoms or []) if d and d > 0)
    unit = denoms[0] if denoms else max(min_bet or 1.0, 1.0)
    cap = max_bet or balance or (unit * 100)
    if balance:
        cap = min(cap, balance)

    # ── Shoe phase ───────────────────────────────────────────────────────── #
    if penetration < 0.25:
        phase = "early"
    elif penetration < 0.55:
        phase = "mid"
    else:
        phase = "late"
    phase_cap = _PHASE_CAPS[phase]

    # ── Main side ────────────────────────────────────────────────────────── #
    # Prefer the mystic pick (it blends composition + pattern); fall back to
    # whichever gameline side the composition currently favours.
    if mystic and mystic.pick in ("banker", "player"):
        main_side = mystic.pick
    else:
        main_side = "banker" if analysis.p_banker >= analysis.p_player else "player"
    main_label = _LEG_LABELS[main_side]

    # ── Signal 1: composition ────────────────────────────────────────────── #
    baseline_main = BASELINE_P_BANKER if main_side == "banker" else BASELINE_P_PLAYER
    current_main = analysis.p_banker if main_side == "banker" else analysis.p_player
    comp_signal = max(0.0, min(1.0, (current_main - baseline_main) / _COMP_SCALE))

    # ── Signal 2: learner (regime-aware recent profit/hand) ──────────────── #
    learner_signal = 0.0
    if learning and learning.actionable:
        pph = max(0.0, learning.profit_per_hand)
        learner_signal = min(1.0, pph / _LEARNER_PPH_SCALE)

    # ── Signal 3: pattern vibe (vote share for the main side) ────────────── #
    pattern_signal = 0.0
    if mystic and mystic.pick == main_side:
        pattern_signal = min(1.0, mystic.vibe)

    # ── Combined signal → multiplier ─────────────────────────────────────── #
    signal = min(1.0, _W_COMP * comp_signal + _W_LEARN * learner_signal + _W_PATT * pattern_signal)
    raw_mult = 1.0 + signal * (phase_cap - 1.0)
    multiplier = max(1.0, round(raw_mult * 2) / 2.0)   # nearest 0.5 unit
    main_stake = min(cap, _snap(multiplier * unit, unit))
    main_stake = max(main_stake, min_bet or unit)

    # ── Main leg ─────────────────────────────────────────────────────────── #
    if main_side == "banker":
        main_ev = _ev_banker(analysis.p_banker, analysis.p_player) * main_stake
    else:
        main_ev = _ev_player(analysis.p_player, analysis.p_banker) * main_stake

    legs: List[SpreadLeg] = [SpreadLeg(
        bet=main_side, label=main_label,
        stake=main_stake, units=multiplier, ev=main_ev,
        reason=f"{phase} shoe · {signal:.0%} signal · {multiplier:.1f}× unit",
    )]

    remaining_cap = cap - main_stake

    # ── Super 6 — always with Banker (pays 15:1 on Banker wins by 6) ─────── #
    if main_side == "banker" and remaining_cap >= unit:
        s6_stake = unit
        legs.append(SpreadLeg(
            bet="super_6", label="Super 6", stake=s6_stake, units=1.0,
            ev=_ev_super6(analysis.p_banker_win_six) * s6_stake,
            reason=f"banker-6 {analysis.p_banker_win_six*100:.2f}% · pays 15:1",
        ))
        remaining_cap -= s6_stake

    # ── Banker Bonus — always with Banker (pays up to 30:1 on large margins) #
    if main_side == "banker" and remaining_cap >= unit:
        bb_ev = _ev_b_bonus(analysis.distribution)
        bb_stake = unit
        legs.append(SpreadLeg(
            bet="b_bonus", label="B Bonus", stake=bb_stake, units=1.0,
            ev=bb_ev * bb_stake,
            reason=f"margin ladder up to 30:1 · EV {bb_ev*100:+.1f}%",
        ))
        remaining_cap -= bb_stake

    # ── Player Bonus — always with Player (pays up to 30:1 on large margins) #
    if main_side == "player" and remaining_cap >= unit:
        pb_ev = _ev_p_bonus(analysis.distribution)
        pb_stake = unit
        legs.append(SpreadLeg(
            bet="p_bonus", label="P Bonus", stake=pb_stake, units=1.0,
            ev=pb_ev * pb_stake,
            reason=f"margin ladder up to 30:1 · EV {pb_ev*100:+.1f}%",
        ))
        remaining_cap -= pb_stake

    # ── Tie — opportunistic: only when probability elevated ≥4% vs baseline ─ #
    tie_ratio = (analysis.p_tie / BASELINE_P_TIE) if BASELINE_P_TIE else 1.0
    if tie_ratio >= 1.0 + SIDE_BET_THRESHOLD and remaining_cap >= unit:
        t_stake = unit
        legs.append(SpreadLeg(
            bet="tie", label="Tie", stake=t_stake, units=1.0,
            ev=_ev_tie(analysis.p_tie) * t_stake,
            reason=f"tie prob {analysis.p_tie*100:.2f}% "
                   f"(+{(tie_ratio-1)*100:.1f}% vs baseline)",
        ))
        remaining_cap -= t_stake

    # ── Live pair probabilities from shoe composition ─────────────────────── #
    if counts and len(counts) == 10:
        pair_probs = live_pair_probabilities(tuple(counts), decks)
    else:
        pair_probs = {}

    live_pp = pair_probs.get("player_pair", 0.0)
    live_ep = pair_probs.get("either_pair", 0.0)
    baseline_pp = pair_probs.get("baseline_player_pair", 0.0)
    baseline_ep = pair_probs.get("baseline_either_pair", 0.0)
    pair_elevated = baseline_ep > 0 and live_ep > 0 and (live_ep / baseline_ep) >= 1.08
    pp_elevated = baseline_pp > 0 and live_pp > 0 and (live_pp / baseline_pp) >= 1.10

    # Learner either-pair edge check
    learner_ep_sig = False
    learner_ep_per100 = 0.0
    if learning:
        for rec in (learning.bets or []):
            if rec.get("bet") == "either_pair" and rec.get("significant"):
                learner_ep_sig = True
                learner_ep_per100 = rec.get("per100", 0.0)
                break

    # ── Either Pair — elevated composition OR learner-significant edge ────── #
    if remaining_cap >= unit and (pair_elevated or learner_ep_sig):
        ep_stake = unit
        if pair_elevated:
            ep_reason = (
                f"pair prob {live_ep*100:.1f}% "
                f"(+{((live_ep / baseline_ep) - 1)*100:.0f}% vs baseline)"
            )
            ep_ev = _ev_either_pair(live_ep)
        else:
            ep_reason = f"learner edge +{learner_ep_per100:.1f}u/100"
            ep_ev = learner_ep_per100 / 100.0
        legs.append(SpreadLeg(
            bet="either_pair", label="Either Pair",
            stake=ep_stake, units=1.0,
            ev=ep_ev * ep_stake,
            reason=ep_reason,
        ))
        remaining_cap -= ep_stake

    # ── Player / Banker Pair — when specific pair probability elevated ≥10% ─ #
    if remaining_cap >= unit and pp_elevated:
        pp_reason = (
            f"pair {live_pp*100:.1f}% "
            f"(+{((live_pp / baseline_pp) - 1)*100:.0f}% vs baseline)"
        )
        if main_side == "player":
            legs.append(SpreadLeg(
                bet="player_pair", label="P Pair",
                stake=unit, units=1.0,
                ev=_ev_pair(live_pp) * unit,
                reason="P " + pp_reason,
            ))
        else:
            legs.append(SpreadLeg(
                bet="banker_pair", label="B Pair",
                stake=unit, units=1.0,
                ev=_ev_pair(live_pp) * unit,
                reason="B " + pp_reason,
            ))
        remaining_cap -= unit

    # ── Kelly criterion (fractional Kelly, 25%) ──────────────────────────── #
    p_win = analysis.p_banker if main_side == "banker" else analysis.p_player
    net_odds = 0.95 if main_side == "banker" else 1.0
    q = 1.0 - p_win
    raw_kelly = (net_odds * p_win - q) / net_odds
    kelly_frac = max(0.0, raw_kelly)
    kelly_stake_val = kelly_frac * 0.25 * (balance or 0.0)

    # ── Affordability ────────────────────────────────────────────────────── #
    total_stake = sum(l.stake for l in legs)
    affordable = (not balance) or total_stake <= balance

    if balance and total_stake > balance:
        # Scale every leg down proportionally, re-snap to whole units.
        scale = balance / total_stake
        legs = [
            SpreadLeg(
                bet=l.bet, label=l.label,
                stake=_snap(l.stake * scale, unit),
                units=round(l.stake * scale / unit, 1),
                ev=l.ev * scale,
                reason=l.reason,
            )
            for l in legs
        ]
        total_stake = sum(l.stake for l in legs)
        affordable = False

    total_ev = sum(l.ev for l in legs)

    # ── Human note ───────────────────────────────────────────────────────── #
    parts: List[str] = []
    if phase == "late":
        parts.append("late shoe")
    if comp_signal >= 0.3:
        parts.append(f"comp +{comp_signal:.0%}")
    if learner_signal >= 0.3 and learning:
        parts.append(f"learner +{learning.profit_per_hand*100:.0f}u/100")
    if pattern_signal >= 0.3 and mystic:
        parts.append(mystic.personality.lower())
    note = " · ".join(parts) if parts else f"{main_label} ({phase})"

    return DynamicSpread(
        main_bet=main_side,
        main_label=main_label,
        legs=legs,
        total_stake=total_stake,
        total_ev=total_ev,
        unit=unit,
        phase=phase,
        signal=signal,
        composition_signal=comp_signal,
        learner_signal=learner_signal,
        pattern_signal=pattern_signal,
        multiplier=multiplier,
        note=note,
        currency=currency,
        affordable=affordable,
        pair_probs=pair_probs,
        kelly_stake=kelly_stake_val,
        kelly_fraction=kelly_frac,
    )
