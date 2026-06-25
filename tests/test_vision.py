"""Vision tests (§6 / §9).

Counter OCR parsing, Big Road CV (via a synthetic render -> read round-trip,
standing in for the spec's fixture PNGs), and new-hand detection.
"""

import numpy as np
import pytest

from baccarat_vision.vision.counter_reader import parse_counter
from baccarat_vision.vision.result_detector import ResultDetector, image_similarity
from baccarat_vision.vision.road_reader import read_big_road, render_big_road


# Counter parsing (§6.1) -------------------------------------------------- #
def test_parse_counter_reference_string():
    r = parse_counter("#24  P 13  B 10  T 1")
    assert (r.hand_number, r.player_wins, r.banker_wins, r.ties) == (24, 13, 10, 1)
    assert r.consistent  # 13 + 10 + 1 == 24


def test_parse_counter_tolerates_tight_spacing():
    r = parse_counter("#24 P13 B10 T1")
    assert (r.hand_number, r.player_wins, r.banker_wins, r.ties) == (24, 13, 10, 1)


def test_parse_counter_flags_inconsistent():
    r = parse_counter("#30 P 13 B 10 T 1")  # sums to 24, not 30
    assert r is not None and not r.consistent


def test_parse_counter_icon_fallback_numbers_only():
    # SpinQuest shows P/B/T as icons, so OCR may return just "#7 5 2 0".
    r = parse_counter("#7 5 2 0")
    assert (r.hand_number, r.player_wins, r.banker_wins, r.ties) == (7, 5, 2, 0)
    assert r.consistent  # 5 + 2 + 0 == 7


def test_parse_counter_rejects_garbage():
    assert parse_counter("no numbers here") is None
    assert parse_counter("") is None


# Big Road CV (§6.2) — synthetic render -> read round-trip ----------------- #
def test_big_road_roundtrip_simple_sequence():
    # Two columns: a P streak of 2, then a B streak of 3.
    columns = [["P", "P"], ["B", "B", "B"]]
    img = render_big_road(columns, cell=24)
    result = read_big_road(img, rows=6)
    # Reconstructed sequence is column-major.
    assert result.sequence == ["P", "P", "B", "B", "B"]


def test_big_road_detects_ties():
    columns = [["P"], ["B"]]
    img = render_big_road(columns, cell=24, ties={(0, 0)})
    result = read_big_road(img, rows=6)
    assert result.tie_marks >= 1
    assert result.sequence == ["P", "B"]


def test_big_road_empty_region():
    img = np.zeros((144, 240, 3), dtype=np.uint8)
    result = read_big_road(img, rows=6)
    assert result.sequence == []


# New-hand detection (§6.3) ----------------------------------------------- #
def test_detector_fires_on_counter_increment():
    from baccarat_vision.vision.counter_reader import CounterReading

    det = ResultDetector()
    c1 = CounterReading(1, 1, 0, 0, True)
    c2 = CounterReading(2, 1, 1, 0, True)
    assert det.feed(c1).new_hand is False  # first reading just syncs
    res = det.feed(c2)
    assert res.new_hand is True and res.source == "counter"


def test_detector_visual_fallback_when_no_counter():
    det = ResultDetector(visual_change_threshold=0.95)
    a = np.zeros((50, 50, 3), dtype=np.uint8)
    b = np.full((50, 50, 3), 200, dtype=np.uint8)
    assert det.feed(None, a).new_hand is False  # first frame syncs
    assert det.feed(None, b).new_hand is True   # big change -> new hand


def test_image_similarity_bounds():
    a = np.zeros((10, 10, 3), dtype=np.uint8)
    assert image_similarity(a, a) == pytest.approx(1.0)
    b = np.full((10, 10, 3), 255, dtype=np.uint8)
    assert image_similarity(a, b) == pytest.approx(0.0, abs=1e-6)
