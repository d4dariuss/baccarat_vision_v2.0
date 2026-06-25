"""Capture geometry + end-to-end vision pipeline tests (§6 / §10 steps 4-7)."""

import numpy as np
import pytest

from baccarat_vision.capture.screen_grabber import (
    Rect,
    StillImageGrabber,
    rect_from_subregion,
)
from baccarat_vision.controller import AppController
from baccarat_vision.pipeline import VisionPipeline
from baccarat_vision.settings import (
    AppConfig,
    CaptureConfig,
    CaptureRegion,
    SubRegion,
)
from baccarat_vision.vision.ocr_backend import CallableBackend


def test_rect_crop():
    img = np.arange(100, dtype=np.uint8).reshape(10, 10)
    rect = Rect(2, 3, 4, 5)
    crop = rect.crop(img)
    assert crop.shape == (5, 4)
    assert crop[0, 0] == img[3, 2]


def test_still_image_grabber_respects_origin():
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    frame[10:20, 30:40] = 255
    grabber = StillImageGrabber(frame)
    out = grabber.grab(Rect(30, 10, 10, 10))
    assert (out == 255).all()


def test_rect_from_subregion():
    sub = SubRegion(x=5, y=6, w=7, h=8)
    assert rect_from_subregion(sub) == Rect(5, 6, 7, 8)


def test_logical_to_physical_retina_scaling():
    from baccarat_vision.capture.region_selector import logical_to_physical

    # 2x Retina, primary screen at origin.
    assert logical_to_physical(100, 50, 200, 100, dpr=2.0) == Rect(200, 100, 400, 200)
    # 1x display is a no-op.
    assert logical_to_physical(10, 20, 30, 40, dpr=1.0) == Rect(10, 20, 30, 40)
    # Secondary-monitor origin offset is applied before scaling.
    assert logical_to_physical(10, 20, 30, 40, dpr=2.0, origin_x=5, origin_y=6) == Rect(
        30, 52, 60, 80
    )


def _pipeline_config():
    return AppConfig(
        capture=CaptureConfig(region=CaptureRegion(x=0, y=0, width=200, height=100)),
        regions={
            "shoe_counter": SubRegion(x=0, y=0, w=120, h=20),
            "table_result": SubRegion(x=0, y=40, w=80, h=40),
        },
        payouts={},
    )


def test_pipeline_advances_shoe_on_counter_increment():
    config = _pipeline_config()
    controller = AppController(config)
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    grabber = StillImageGrabber(frame)

    # Scripted OCR: each call returns the next counter string.
    scripted = iter(
        [
            "#1 P 1 B 0 T 0",
            "#2 P 1 B 1 T 0",  # banker wins -> winner B
            "#3 P 2 B 1 T 0",  # player wins -> winner P
        ]
    )
    last = {"text": ""}

    def fake_ocr(_img):
        try:
            last["text"] = next(scripted)
        except StopIteration:
            pass
        return last["text"]

    pipe = VisionPipeline(controller, grabber, CallableBackend(fake_ocr))

    t1 = pipe.tick()  # first clean read -> sync to shoe, no new hand fired
    assert t1.new_hand is False and t1.synced is True
    t2 = pipe.tick()  # #2, banker increment
    assert t2.new_hand and t2.winner == "B"
    t3 = pipe.tick()  # #3, player increment
    assert t3.new_hand and t3.winner == "P"

    snap = controller.snapshot()
    # 1 hand synced on join + 2 reconciled = 3 hands played.
    assert snap.hands_played == 3
    # Road mirror reflects the detected (post-join) winners.
    assert snap.road_grid[0][0] == "B"
    assert snap.road_grid[1][0] == "P"


def test_pipeline_reconciles_missed_hands_in_one_gap():
    # If OCR misses a frame, a multi-hand jump is caught up, not lost.
    config = _pipeline_config()
    controller = AppController(config)
    grabber = StillImageGrabber(np.zeros((100, 200, 3), dtype=np.uint8))
    scripted = iter(["#5 P 3 B 2 T 0", "#9 P 5 B 3 T 1"])  # jump of 4 hands
    last = {"text": "#5 P 3 B 2 T 0"}

    def fake_ocr(_img):
        try:
            last["text"] = next(scripted)
        except StopIteration:
            pass
        return last["text"]

    pipe = VisionPipeline(controller, grabber, CallableBackend(fake_ocr))
    pipe.tick()           # sync at hand 5
    base = controller.snapshot().hands_played
    t = pipe.tick()       # jumps to hand 9 -> +2P +1B +1T = 4 hands
    assert t.hands_added == 4
    assert controller.snapshot().hands_played == base + 4


def test_pipeline_counter_reset_rebaselines_without_reshuffle():
    # A counter reset (new shoe at the casino) must NOT auto-reshuffle — the user
    # controls new shoes manually. It just rebaselines so no bogus hands are added.
    config = _pipeline_config()
    controller = AppController(config)
    grabber = StillImageGrabber(np.zeros((100, 200, 3), dtype=np.uint8))
    scripted = iter(["#40 P 20 B 18 T 2", "#1 P 1 B 0 T 0"])  # counter resets
    last = {"text": "#40 P 20 B 18 T 2"}

    def fake_ocr(_img):
        try:
            last["text"] = next(scripted)
        except StopIteration:
            pass
        return last["text"]

    pipe = VisionPipeline(controller, grabber, CallableBackend(fake_ocr))
    pipe.tick()           # sync deep into the old shoe (40 hands)
    synced_hands = controller.snapshot().hands_played
    t = pipe.tick()       # counter reset
    assert t.new_hand is False  # no hands added from a negative delta
    # Shoe composition is unchanged (no auto-reshuffle); user clicks New Shoe.
    assert controller.snapshot().hands_played == synced_hands


def test_controller_start_new_shoe_burns():
    controller = AppController()
    controller.start_new_shoe(burn_cards=10)
    snap = controller.snapshot()
    assert snap.total_remaining == 416 - 10
    assert snap.hands_played == 0


def test_pipeline_null_backend_no_crash():
    config = _pipeline_config()
    controller = AppController(config)
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    pipe = VisionPipeline(controller, StillImageGrabber(frame))  # NullBackend
    tick = pipe.tick()
    assert tick.new_hand is False
    assert controller.snapshot().hands_played == 0
