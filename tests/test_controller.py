"""Controller / manual-hand-entry integration tests (§10 step 3).

Verifies the math works end-to-end with no computer vision: drive the
controller with manually-entered hands and check the dashboard snapshot.
"""

import pytest

from baccarat_vision.controller import AppController, HandInput


def test_manual_entry_updates_shoe_and_road():
    ctrl = AppController()
    ctrl.enter_hand(HandInput("P", 7, 5, card_values=[3, 4, 2, 3]))  # exact
    ctrl.enter_hand(HandInput("B", 2, 7))  # estimated
    ctrl.enter_hand(HandInput("T", 5, 5))  # tie annotates prior entry

    snap = ctrl.snapshot()
    assert snap.hands_played == 3
    # 4 exact cards + ~5 estimated removed.
    assert snap.total_remaining < 416
    # Estimated hand present -> confidence drops to low.
    assert snap.composition_confidence == "low"
    # Road: P then B columns (tie does not open a column).
    assert snap.road_grid[0][0] == "P"
    assert snap.road_grid[1][0] == "B"


def test_bet_spread_snapshot_probabilities_sum_to_one():
    ctrl = AppController()
    ctrl.set_bet("player", 5)
    ctrl.set_bet("tie", 1)
    snap = ctrl.snapshot()
    assert sum(r.probability for r in snap.spread.rows) == pytest.approx(1.0, abs=1e-9)
    assert snap.spread.total_at_risk == pytest.approx(6.0)
    assert snap.spread.best_case == pytest.approx(8.0, abs=1e-4)


def test_predictor_holds_off_until_min_hands():
    ctrl = AppController()
    snap = ctrl.snapshot()
    assert snap.prediction.predicting is False
    for _ in range(10):
        ctrl.enter_hand(HandInput("B", 3, 7))
    assert ctrl.snapshot().prediction.predicting is True


def test_snapshot_survives_depleted_shoe():
    # A flood of hands (e.g. from a bad counter delta) must not crash snapshot.
    ctrl = AppController()
    for _ in range(120):
        try:
            ctrl.enter_hand(HandInput("B", 0, 0))
        except Exception:
            pass
    snap = ctrl.snapshot()  # must not raise
    assert snap.prediction.predicting is False
    assert "new shoe" in snap.prediction.lean.lower()


def test_catch_up_reflects_hands_already_played():
    ctrl = AppController()
    ctrl.catch_up(23, burn_cards=10)
    snap = ctrl.snapshot()
    assert snap.hands_played == 23
    assert snap.total_remaining < 416 - 10  # burn + ~5/hand removed


def test_reshuffle_restores_full_shoe():
    ctrl = AppController()
    for _ in range(5):
        ctrl.enter_hand(HandInput("P", 6, 4))
    ctrl.reshuffle()
    snap = ctrl.snapshot()
    assert snap.total_remaining == 416
    assert snap.hands_played == 0
    assert snap.road_grid == []
