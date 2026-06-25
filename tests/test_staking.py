"""Staking / bankroll suggestion tests."""

from baccarat_vision.controller import AppController, HandInput
from baccarat_vision.engine.staking import build_spread, snap_to_denoms, suggest_stake


def test_snap_to_denoms():
    d = [1, 5, 25, 100, 500, 2500]
    assert snap_to_denoms(7, d) == 5
    assert snap_to_denoms(3000, d) == 2500
    assert snap_to_denoms(0.5, d) == 1  # below smallest -> smallest


def _suggest(**kw):
    base = dict(
        main_bet="banker", main_label="Banker", confident=False, vibe=0.5,
        balance=10000.0, denoms=[1, 5, 25, 100, 500, 2500], min_bet=1.0,
        max_bet=5000.0, strategy="confidence", consec_losses=0, currency="SC",
        side_suggestions=[],
    )
    base.update(kw)
    return suggest_stake(**base)


def test_confidence_staking_scales_with_confidence():
    low = _suggest(confident=False)
    high = _suggest(confident=True, vibe=0.9)
    assert high.stake > low.stake
    assert low.stake == low.unit  # min unit when unsure


def test_martingale_doubles_after_losses():
    s0 = _suggest(strategy="martingale", consec_losses=0)
    s3 = _suggest(strategy="martingale", consec_losses=3)
    assert s3.stake > s0.stake
    assert s3.stake <= 5000  # capped by max_bet


def test_side_bets_suggested_and_snapped():
    s = _suggest(side_suggestions=[("either_pair", "Either Pair", "pair due")])
    assert len(s.side_bets) == 1
    assert s.side_bets[0]["bet"] == "either_pair"
    assert s.side_bets[0]["stake"] in (1, 5, 25, 100, 500, 2500)


def test_build_spread_scales_preset_to_unit():
    # Banker spread = 7u B + 1u Tie + 1u Super 6 + 1u B Bonus, scaled by chip unit.
    label, legs, total, affordable = build_spread("banker", 10000, 742118)
    assert label == "Banker spread"
    assert [(g["bet"], g["units"], g["stake"]) for g in legs] == [
        ("banker", 7, 70000), ("tie", 1, 10000), ("super_6", 1, 10000), ("b_bonus", 1, 10000)]
    assert total == 100000 and affordable is True
    # Player spread = 7u P + 2u Tie + 1u P Bonus, in SC units.
    label, legs, total, _ = build_spread("player", 1, 65)
    assert label == "Player spread" and total == 10
    assert [(g["bet"], g["units"]) for g in legs] == [("player", 7), ("tie", 2), ("p_bonus", 1)]
    # Too small a balance -> flagged unaffordable, still reported.
    _, _, _, affordable = build_spread("banker", 10000, 50000)
    assert affordable is False


def test_suggest_stake_attaches_favoured_side_spread():
    s = _suggest(main_bet="banker", confident=True, vibe=0.6)
    assert s.spread_label == "Banker spread"
    assert s.spread_legs[0]["bet"] == "banker"
    assert s.spread_total == sum(g["stake"] for g in s.spread_legs)


def test_controller_tracks_bankroll_and_staking():
    ctrl = AppController()
    ctrl.set_context(currency="SC", balance=1000.0, denoms=[1, 5, 25, 100], strategy="confidence")
    for w in list("BBPBBPB"):
        ctrl.enter_hand(HandInput(w, 5, 7))
    snap = ctrl.snapshot()
    assert snap.staking is not None
    assert snap.staking.main_bet in ("player", "banker", "tie")
    assert snap.bankroll["currency"] == "SC"
    assert snap.bankroll["shoe_start"] == 1000.0
