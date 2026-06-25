"""Server slot-routing and probe-endpoint tests."""

import importlib
import json
import threading
import time
from http.client import HTTPConnection
from unittest.mock import patch

import pytest

import baccarat_vision.server as srv
from baccarat_vision.controller import AppController, HandInput
from baccarat_vision.engine.probability import full_shoe_value_counts


# ── helpers ────────────────────────────────────────────────────────────────── #

def _fresh_module():
    """Re-import server with a clean global state."""
    import importlib, baccarat_vision.server as m
    # Reset module-level globals so tests don't bleed into each other.
    m._SLOTS.clear()
    m._SLOT_ACCESSED.clear()
    m._LIBRARY = None
    return m


def _make_ctrl():
    return AppController()


# ── _probe unit tests ──────────────────────────────────────────────────────── #

class TestProbe:
    def setup_method(self):
        srv._SLOTS.clear()
        srv._SLOT_ACCESSED.clear()
        srv._LIBRARY = None

    def test_no_slots_returns_lru_slot_no_match(self):
        result = srv._probe(10, 5, 4, 1)
        assert result["match"] is False
        assert result["slot"] in srv._SLOT_NAMES

    def test_matching_slot_returned(self):
        ctrl = _make_ctrl()
        # Inject 10 hands: 5P, 4B, 1T.
        for w in list("PPPPPBBBB"):
            ctrl.enter_hand(HandInput(w, 5, 3))
        ctrl.enter_hand(HandInput("T", 5, 5))
        srv._SLOTS["A"] = ctrl
        srv._SLOT_ACCESSED["A"] = time.time()

        result = srv._probe(total=10, player_wins=5, banker_wins=4, ties=1)
        assert result["match"] is True
        assert result["slot"] == "A"
        assert result["server_hands"] == 10
        assert result["gap"] == 0

    def test_mismatch_outside_tolerance_gives_no_match(self):
        ctrl = _make_ctrl()
        for w in list("BBBBBBBBBB"):   # 10 banker wins
            ctrl.enter_hand(HandInput(w, 3, 7))
        srv._SLOTS["A"] = ctrl
        srv._SLOT_ACCESSED["A"] = time.time()

        # Probe claims 5P, 4B, 1T — very different from 10B.
        result = srv._probe(total=10, player_wins=5, banker_wins=4, ties=1)
        assert result["match"] is False

    def test_gap_computed_correctly(self):
        ctrl = _make_ctrl()
        for w in list("PPPPPBBBBT"):  # 10 hands
            ctrl.enter_hand(HandInput(w, 5 if w in "PT" else 3, 5 if w in "BT" else 3))
        srv._SLOTS["A"] = ctrl
        srv._SLOT_ACCESSED["A"] = time.time()

        # Client says 12 hands (server at 10) — gap = 2.
        result = srv._probe(total=12, player_wins=6, banker_wins=5, ties=1)
        # Tolerance: |12-10|=2 ≤ 5, |6-5|=1 ≤ 3, |5-4|=1 ≤ 3 — should match.
        assert result["match"] is True
        assert result["gap"] == 2

    def test_lru_slot_selected_when_both_slots_present(self):
        srv._SLOTS["A"] = _make_ctrl()
        srv._SLOT_ACCESSED["A"] = time.time() - 100  # accessed longer ago
        srv._SLOTS["B"] = _make_ctrl()
        srv._SLOT_ACCESSED["B"] = time.time()

        # No match — LRU is A (accessed longest ago).
        result = srv._probe(total=50, player_wins=25, banker_wins=20, ties=5)
        assert result["match"] is False
        assert result["slot"] == "A"


# ── Integration: live server ───────────────────────────────────────────────── #

@pytest.fixture(scope="module")
def live_server():
    srv._SLOTS.clear()
    srv._SLOT_ACCESSED.clear()
    srv._LIBRARY = None
    server = srv.make_server(port=18777)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    yield server
    server.shutdown()


def _post(path, body, port=18777):
    conn = HTTPConnection("127.0.0.1", port)
    conn.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    r = conn.getresponse()
    return json.loads(r.read())


def _get(path, port=18777):
    conn = HTTPConnection("127.0.0.1", port)
    conn.request("GET", path)
    r = conn.getresponse()
    return json.loads(r.read())


class TestLiveServer:
    def test_health_returns_slot(self, live_server):
        data = _get("/health?slot=A")
        assert data["ok"] is True
        assert data["slot"] == "A"

    def test_probe_returns_no_match_on_fresh_server(self, live_server):
        srv._SLOTS.clear()
        srv._SLOT_ACCESSED.clear()
        result = _post("/probe", {"total": 5, "player_wins": 3, "banker_wins": 2, "ties": 0})
        assert result["match"] is False
        assert "slot" in result

    def test_reset_and_probe_match(self, live_server):
        srv._SLOTS.clear()
        srv._SLOT_ACCESSED.clear()
        # Reset slot A with 0 hands.
        _post("/reset?slot=A", {"burn_cards": 10, "hands": 0})
        # Send 5 hands: 3B, 2P.
        for w in list("BBBPP"):
            _post("/hand?slot=A", {"winner": w, "player_total": 3, "banker_total": 7})
        # Probe should match slot A.
        result = _post("/probe", {"total": 5, "player_wins": 2, "banker_wins": 3, "ties": 0})
        assert result["match"] is True
        assert result["slot"] == "A"
        assert result["gap"] == 0

    def test_catch_up_adds_hands_without_reset(self, live_server):
        srv._SLOTS.clear()
        srv._SLOT_ACCESSED.clear()
        _post("/reset?slot=B", {"burn_cards": 10, "hands": 0})
        snap_before = _get("/snapshot?slot=B")
        h_before = snap_before["shoe"]["hands_played"]
        # Catch up 3 hands.
        _post("/catch_up?slot=B", {"hands": 3})
        snap_after = _get("/snapshot?slot=B")
        h_after = snap_after["shoe"]["hands_played"]
        assert h_after == h_before + 3

    def test_two_slots_independent(self, live_server):
        srv._SLOTS.clear()
        srv._SLOT_ACCESSED.clear()
        _post("/reset?slot=A", {"burn_cards": 10, "hands": 0})
        _post("/reset?slot=B", {"burn_cards": 10, "hands": 0})
        # Send 3 hands to slot A, 7 to slot B.
        for _ in range(3):
            _post("/hand?slot=A", {"winner": "B", "player_total": 3, "banker_total": 7})
        for _ in range(7):
            _post("/hand?slot=B", {"winner": "P", "player_total": 7, "banker_total": 3})
        snap_a = _get("/snapshot?slot=A")
        snap_b = _get("/snapshot?slot=B")
        assert snap_a["shoe"]["hands_played"] == 3
        assert snap_b["shoe"]["hands_played"] == 7
