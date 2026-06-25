"""Tests for the dynamic bet-spread engine."""

from baccarat_vision.engine.dynamic_spread import (
    DynamicSpread,
    SIDE_BET_THRESHOLD,
    compute_dynamic_spread,
)
from baccarat_vision.engine.learning import OnlineLearner
from baccarat_vision.engine.patterns import analyze_patterns
from baccarat_vision.engine.probability import BASELINE_P_TIE, analyze_shoe, full_shoe_value_counts
from baccarat_vision.controller import AppController, HandInput


def _mystic(pick="banker", vibe=0.6, personality="Mixed"):
    from baccarat_vision.engine.patterns import MysticAdvice
    return MysticAdvice(pick=pick, pick_label=pick.title(), vibe=vibe,
                        personality=personality, reasons=[], votes={})


def _learning(actionable=False, pph=0.0, acts=0):
    from baccarat_vision.engine.learning import Scoreboard
    return Scoreboard(
        graded=acts, accuracy=0.5, baseline_accuracy=0.5,
        profit=pph * acts, baseline_profit=0.0,
        recent_accuracy=0.5, best_expert="flat-banker",
        best_expert_hit=0.5, best_expert_profit=0.0,
        profit_per_hand=pph, profit_lb=0.0,
        significant=False, actionable=actionable,
        verdict="", acts=acts, min_hands=12, experts=[], bets=[],
    )


def _spread(penetration=0.1, balance=1000.0, pph=0.0, actionable=False,
            mystic_pick="banker", vibe=0.6, analysis=None):
    if analysis is None:
        analysis = analyze_shoe(full_shoe_value_counts(8))
    return compute_dynamic_spread(
        analysis=analysis,
        prediction=None,
        learning=_learning(actionable=actionable, pph=pph, acts=20),
        mystic=_mystic(pick=mystic_pick, vibe=vibe),
        penetration=penetration,
        balance=balance,
        denoms=[1.0, 5.0, 25.0, 100.0],
        min_bet=1.0,
        max_bet=5000.0,
        currency="GC",
    )


# ── Phase detection ────────────────────────────────────────────────────────── #
def test_phase_early_mid_late():
    assert _spread(penetration=0.10).phase == "early"
    assert _spread(penetration=0.40).phase == "mid"
    assert _spread(penetration=0.70).phase == "late"


# ── Unit snapping ─────────────────────────────────────────────────────────── #
def test_main_stake_is_whole_unit_multiple():
    ds = _spread()
    assert ds.total_stake > 0
    # stake should be a whole multiple of the unit
    assert abs(ds.legs[0].stake % ds.unit) < 0.01


# ── Signal drives multiplier ──────────────────────────────────────────────── #
def test_no_signal_gives_1x():
    ds = _spread(penetration=0.10, pph=0.0, actionable=False, vibe=0.0)
    assert ds.multiplier == 1.0


def test_strong_learner_signal_increases_multiplier():
    ds_weak = _spread(pph=0.0, actionable=False, penetration=0.60)
    ds_strong = _spread(pph=0.5, actionable=True, penetration=0.60)
    assert ds_strong.multiplier >= ds_weak.multiplier


# ── Phase caps ────────────────────────────────────────────────────────────── #
def test_early_phase_max_2x():
    ds = _spread(penetration=0.10, pph=1.0, actionable=True, vibe=1.0)
    assert ds.multiplier <= 2.0


def test_late_phase_allows_higher_multiplier():
    ds = _spread(penetration=0.70, pph=1.0, actionable=True, vibe=1.0)
    assert ds.multiplier > 2.0


# ── Always-on companion bets ──────────────────────────────────────────────── #
def test_super6_always_with_banker():
    ds = _spread(mystic_pick="banker")
    assert any(l.bet == "super_6" for l in ds.legs)


def test_b_bonus_always_with_banker():
    ds = _spread(mystic_pick="banker")
    assert any(l.bet == "b_bonus" for l in ds.legs)


def test_p_bonus_always_with_player():
    ds = _spread(mystic_pick="player")
    assert any(l.bet == "p_bonus" for l in ds.legs)


def test_super6_not_with_player():
    ds = _spread(mystic_pick="player")
    assert not any(l.bet == "super_6" for l in ds.legs)


def test_b_bonus_not_with_player():
    ds = _spread(mystic_pick="player")
    assert not any(l.bet == "b_bonus" for l in ds.legs)


def test_bonus_ev_computed_from_distribution():
    ds = _spread(mystic_pick="banker")
    bb = next(l for l in ds.legs if l.bet == "b_bonus")
    assert bb.ev != 0.0   # real integration, not a placeholder
    assert bb.ev < 0.0    # house edge: EV is negative at full-shoe baseline


# ── Side bet gating ───────────────────────────────────────────────────────── #
def test_tie_not_included_at_baseline():
    ds = _spread()
    assert not any(l.bet == "tie" for l in ds.legs)


def test_tie_included_when_elevated():
    import math
    # Build a shoe with elevated tie probability by removing many 8s and 9s.
    # (More naturals → more ties.)  Use a shoe with many 8/9 cards remaining.
    counts = list(full_shoe_value_counts(8))
    # Remove lots of low cards (values 2-6) to skew toward high cards.
    for v in range(2, 7):
        counts[v] = max(0, counts[v] - 20)
    analysis = analyze_shoe(tuple(counts))
    ds = compute_dynamic_spread(
        analysis=analysis, prediction=None,
        learning=_learning(), mystic=_mystic(),
        penetration=0.5, balance=1000.0, denoms=[1.0],
        min_bet=1.0, max_bet=5000.0, currency="",
    )
    # If tie is elevated enough it appears; if not, test is still meaningful
    # (checks the gate doesn't fire spuriously at baseline).
    if analysis.p_tie > BASELINE_P_TIE * (1 + SIDE_BET_THRESHOLD):
        assert any(l.bet == "tie" for l in ds.legs)
    else:
        assert not any(l.bet == "tie" for l in ds.legs)


# ── Affordability scaling ──────────────────────────────────────────────────── #
def test_spread_stays_within_balance():
    ds = compute_dynamic_spread(
        analysis=analyze_shoe(full_shoe_value_counts(8)),
        prediction=None, learning=_learning(actionable=True, pph=0.5, acts=20),
        mystic=_mystic(vibe=1.0), penetration=0.70,
        balance=3.0, denoms=[1.0], min_bet=1.0, max_bet=5000.0, currency="",
    )
    assert ds.total_stake <= 3.0 + 0.01


# ── Controller integration ────────────────────────────────────────────────── #
def test_controller_snapshot_includes_dynamic_spread():
    ctrl = AppController()
    # No bankroll data yet — dynamic_spread may be None or populated.
    snap = ctrl.snapshot()
    # After a few hands with bankroll context, it should populate.
    ctrl.set_context(balance=500.0, denoms=[1.0, 5.0, 25.0],
                     min_bet=1.0, max_bet=500.0, currency="GC")
    for w in list("BBPBPBPBBB"):
        ctrl.enter_hand(HandInput(w, 5, 7))
    snap = ctrl.snapshot()
    assert snap.dynamic_spread is not None
    assert snap.dynamic_spread.total_stake > 0
    assert len(snap.dynamic_spread.legs) >= 1


# ── Main side follows mystic ───────────────────────────────────────────────── #
def test_main_side_follows_mystic_pick():
    ds_b = _spread(mystic_pick="banker")
    ds_p = _spread(mystic_pick="player")
    assert ds_b.main_bet == "banker"
    assert ds_p.main_bet == "player"
