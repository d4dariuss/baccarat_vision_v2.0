"""Card-value OCR tests (§6.4 / §10 step 9)."""

import numpy as np
import pytest

from baccarat_vision.controller import AppController
from baccarat_vision.pipeline import VisionPipeline
from baccarat_vision.settings import (
    AppConfig,
    CaptureConfig,
    CaptureRegion,
    SubRegion,
    VisionSettings,
)
from baccarat_vision.capture.screen_grabber import StillImageGrabber
from baccarat_vision.vision.card_reader import (
    baccarat_total,
    parse_card_rank,
    rank_to_value,
    read_cards,
)
from baccarat_vision.vision.ocr_backend import CallableBackend


# Rank parsing / value mapping -------------------------------------------- #
@pytest.mark.parametrize(
    "text,rank,value",
    [
        ("A", "A", 1),
        ("A♠", "A", 1),
        ("7", "7", 7),
        ("10", "10", 0),
        ("K", "K", 0),
        ("Q of hearts", "Q", 0),
        ("J", "J", 0),
        ("9", "9", 9),
    ],
)
def test_parse_and_map_ranks(text, rank, value):
    assert parse_card_rank(text) == rank
    assert rank_to_value(rank) == value


def test_parse_rank_rejects_noise():
    assert parse_card_rank("") is None
    assert parse_card_rank("?!") is None


@pytest.mark.parametrize(
    "text,values",
    [
        ("2 7", [2, 7]),            # Player band: 2,7 -> total 9
        ("A K", [1, 0]),            # Banker band: A,K -> total 1
        ("10 5 3", [0, 5, 3]),      # three cards incl. a ten
        ("Q J 8", [0, 0, 8]),       # faces + an 8
        ("", []),
        ("$%^", []),                # no rank glyphs -> nothing
    ],
)
def test_extract_card_values_multi(text, values):
    from baccarat_vision.vision.card_reader import extract_card_values

    assert extract_card_values(text) == values


def test_baccarat_total_wraps_mod_10():
    assert baccarat_total([7, 8]) == 5   # 15 -> 5
    assert baccarat_total([0, 0]) == 0   # two tens
    assert baccarat_total([4, 5]) == 9


# read_cards with a scripted backend -------------------------------------- #
def _scripted_backend(values):
    seq = iter(values)
    return CallableBackend(lambda _img: next(seq, ""))


def test_read_cards_player_natural_win():
    # Player K,9 (=9) vs Banker 5,2 (=7) -> Player wins.
    backend = _scripted_backend(["K", "9", "5", "2"])
    imgs = [np.zeros((10, 10, 3), np.uint8) for _ in range(2)]
    result = read_cards(imgs, list(imgs), backend)
    assert result is not None
    assert result.player_values == [0, 9] and result.player_total == 9
    assert result.banker_values == [5, 2] and result.banker_total == 7
    assert result.winner == "P"
    assert result.all_values == [0, 9, 5, 2]


def test_read_cards_returns_none_when_unreadable():
    backend = _scripted_backend(["", "", "", ""])
    imgs = [np.zeros((10, 10, 3), np.uint8) for _ in range(2)]
    assert read_cards(imgs, list(imgs), backend) is None


# Pipeline integration: exact counting when cards agree with the winner ---- #
def _card_config(read_cards=True):
    return AppConfig(
        capture=CaptureConfig(region=CaptureRegion(x=0, y=0, width=200, height=100)),
        regions={
            "shoe_counter": SubRegion(x=0, y=0, w=120, h=20),
            "card_player_1": SubRegion(x=0, y=30, w=20, h=30),
            "card_player_2": SubRegion(x=20, y=30, w=20, h=30),
            "card_banker_1": SubRegion(x=40, y=30, w=20, h=30),
            "card_banker_2": SubRegion(x=60, y=30, w=20, h=30),
        },
        payouts={},
        vision=VisionSettings(read_cards=read_cards),
    )


def test_pipeline_uses_exact_cards_when_consistent():
    controller = AppController(_card_config(read_cards=True))
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    grabber = StillImageGrabber(frame)

    # Counter says hand 2, player count went up -> winner P.
    # Card crops (4 regions) read K,9 / 5,2 -> player 9 vs banker 7 -> P. Agree.
    counters = iter(["#1 P 1 B 0 T 0", "#2 P 2 B 0 T 0"])
    card_seq = iter(["K", "9", "5", "2"])  # only consumed on the new-hand tick

    def fake_ocr(img):
        # Counter region is the full-width 120x20 strip; cards are 20x30.
        h, w = img.shape[:2] if hasattr(img, "shape") else (0, 0)
        # Heuristic: the counter preprocess upscales 3x -> tall/wide strip.
        if w >= h * 2:
            return next(counters, "#2 P 2 B 0 T 0")
        return next(card_seq, "")

    pipe = VisionPipeline(controller, grabber, CallableBackend(fake_ocr))
    pipe.tick()           # sync counter (burn 10 + 1 estimated hand)
    tick = pipe.tick()    # new hand, exact cards
    assert tick.new_hand and tick.winner == "P"
    assert tick.exact_cards is True
    snap = controller.snapshot()
    # 416 - 10 burn - 5 (synced est. hand) - 4 (exact K,9,5,2) = 397.
    assert snap.total_remaining == 397
    # The exact hand restores high composition confidence.
    assert snap.composition_confidence == "high"


def test_pipeline_falls_back_when_cards_disagree():
    controller = AppController(_card_config(read_cards=True))
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    grabber = StillImageGrabber(frame)

    counters = iter(["#1 P 1 B 0 T 0", "#2 P 1 B 1 T 0"])  # winner B
    card_seq = iter(["K", "9", "5", "2"])  # cards say player 9 > banker 7 -> P

    def fake_ocr(img):
        h, w = img.shape[:2]
        if w >= h * 2:
            return next(counters, "#2 P 1 B 1 T 0")
        return next(card_seq, "")

    pipe = VisionPipeline(controller, grabber, CallableBackend(fake_ocr))
    pipe.tick()
    tick = pipe.tick()
    assert tick.new_hand and tick.winner == "B"
    assert tick.exact_cards is False  # mismatch -> estimated path
    assert any("Card OCR winner" in w for w in tick.warnings)
    snap = controller.snapshot()
    assert snap.composition_confidence == "low"
