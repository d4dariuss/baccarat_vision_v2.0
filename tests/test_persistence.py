"""Persistence + replay tests (§10 step 8)."""

from baccarat_vision.controller import AppController, HandInput
from baccarat_vision.persistence.db import Database


def _db():
    return Database("sqlite:///:memory:")


def test_log_and_read_hands():
    db = _db()
    shoe_id = db.start_shoe(decks=8)
    db.log_hand(shoe_id, HandInput("P", 7, 5, card_values=[3, 4, 2, 3]))
    db.log_hand(shoe_id, HandInput("B", 2, 7))
    hands = db.get_hands(shoe_id)
    assert [h.seq for h in hands] == [1, 2]
    assert hands[0].winner == "P"
    assert hands[0].card_values == "3,4,2,3"
    assert hands[1].winner == "B" and hands[1].card_values == ""


def test_controller_logs_when_db_attached():
    db = _db()
    ctrl = AppController(db=db)
    ctrl.enter_hand(HandInput("P", 7, 5))
    ctrl.enter_hand(HandInput("B", 2, 7))
    assert ctrl._shoe_id is not None
    assert len(db.get_hands(ctrl._shoe_id)) == 2


def test_replay_reproduces_shoe_state():
    db = _db()
    source = AppController(db=db)
    hands = [
        HandInput("P", 7, 5, card_values=[3, 4, 2, 3]),
        HandInput("B", 2, 7, card_values=[5, 2, 3, 4]),
        HandInput("T", 5, 5, card_values=[2, 3, 1, 4]),
    ]
    for h in hands:
        source.enter_hand(h)
    shoe_id = source._shoe_id

    # Replay into a fresh controller and compare composition + road.
    target = AppController()
    db.replay(shoe_id, target)

    s_src = source.snapshot()
    s_tgt = target.snapshot()
    assert s_tgt.shoe_counts == s_src.shoe_counts
    assert s_tgt.total_remaining == s_src.total_remaining
    assert s_tgt.road_grid == s_src.road_grid


def test_reshuffle_opens_new_shoe_row():
    db = _db()
    ctrl = AppController(db=db)
    ctrl.enter_hand(HandInput("P", 7, 5))
    first = ctrl._shoe_id
    ctrl.reshuffle()
    ctrl.enter_hand(HandInput("B", 2, 7))
    assert ctrl._shoe_id != first
