"""Real-data vision test against an actual SpinQuest screenshot (§9 fixtures).

Skips automatically when EasyOCR isn't installed (it's a heavy optional dep), so
the core suite stays light. When OCR *is* present this exercises the full
capture→OCR→parse path on a real frame using the calibrated default config.
"""

from pathlib import Path

import cv2
import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "spinquest_full.png"

pytest.importorskip("easyocr", reason="EasyOCR not installed (optional [ocr] extra)")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="screenshot fixture missing")


def test_pipeline_reads_spinquest_counter():
    from baccarat_vision.controller import AppController
    from baccarat_vision.capture.screen_grabber import StillImageGrabber
    from baccarat_vision.pipeline import VisionPipeline
    from baccarat_vision.settings import (
        AppConfig,
        CaptureConfig,
        CaptureRegion,
        SubRegion,
    )
    from baccarat_vision.vision.ocr_backend import get_ocr_backend

    frame = cv2.imread(str(FIXTURE))
    assert frame is not None and frame.shape[:2] == (2234, 3456)

    # Pin the config to the layout this fixture was captured at (full screen +
    # the verified counter box), independent of the user-editable default.yaml.
    config = AppConfig(
        capture=CaptureConfig(region=CaptureRegion(x=0, y=0, width=3456, height=2234)),
        regions={"shoe_counter": SubRegion(x=372, y=1574, w=396, h=52)},
        payouts={},
    )
    ctrl = AppController(config)
    pipe = VisionPipeline(ctrl, StillImageGrabber(frame, origin=(0, 0)), get_ocr_backend("easyocr"))
    tick = pipe.tick()

    assert tick.counter is not None, "counter region failed to OCR"
    c = tick.counter
    assert (c.hand_number, c.player_wins, c.banker_wins, c.ties) == (26, 12, 11, 3)
    assert c.consistent


CARDS_FIXTURE = FIXTURE.parent / "spinquest_cards.png"


@pytest.mark.skipif(not CARDS_FIXTURE.exists(), reason="cards fixture missing")
def test_card_ocr_reads_bottom_panel():
    """EasyOCR reads the flat cards in the bottom result panel (true counting)."""
    from baccarat_vision.vision.card_reader import read_cards
    from baccarat_vision.vision.ocr_backend import get_ocr_backend

    img = cv2.imread(str(CARDS_FIXTURE))
    assert img is not None and img.shape[:2] == (2234, 3456)
    # Per-side card bands measured from the bottom result panel (PLAYER 2,7 = 9
    # vs BANKER A,K = 1).
    player = img[1780:1950, 1470:1700]
    banker = img[1780:1950, 1930:2170]
    result = read_cards([player], [banker], get_ocr_backend("easyocr"))
    assert result is not None
    assert sorted(result.player_values) == [2, 7]
    assert sorted(result.banker_values) == [0, 1]  # A=1, K=0
    assert result.player_total == 9 and result.banker_total == 1
    assert result.winner == "P"
