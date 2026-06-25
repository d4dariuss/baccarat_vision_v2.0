"""Live vision pipeline (§6 / §10 steps 4-7).

Ties the capture grabber + OCR + road CV + new-hand detection into the
:class:`AppController`. One :meth:`VisionPipeline.tick` grabs the capture
region, reads the shoe counter, detects whether a new hand landed, and — when it
has — derives the winning side from the **counter delta** (which of P/B/T grew),
advances the shoe (estimated composition, since card values aren't read yet),
and cross-validates against the Big Road.

Card values aren't observable from totals alone, so composition is tracked with
the estimated/low-confidence path (§4.3); card-value OCR is the stretch step 9.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .capture.screen_grabber import Grabber, Rect
from .controller import AppController, HandInput
from .vision.card_reader import CardReadResult, read_cards
from .vision.counter_reader import CounterReading, read_counter
from .vision.ocr_backend import NullBackend, OcrBackend
from .vision.road_reader import RoadReadResult, read_big_road


@dataclass
class PipelineTick:
    new_hand: bool = False
    winner: Optional[str] = None
    hands_added: int = 0
    counter: Optional[CounterReading] = None
    road: Optional[RoadReadResult] = None
    cards: Optional[CardReadResult] = None
    exact_cards: bool = False  # True when card OCR was trusted for this hand
    new_shoe: bool = False
    synced: bool = False
    detection_source: str = "none"
    warnings: List[str] = field(default_factory=list)


class VisionPipeline:
    def __init__(
        self,
        controller: AppController,
        grabber: Grabber,
        ocr_backend: Optional[OcrBackend] = None,
    ) -> None:
        self.controller = controller
        self.grabber = grabber
        self.ocr = ocr_backend or NullBackend()
        self.regions = controller.config.regions
        self.burn_cards = controller.config.vision.burn_cards
        # Cumulative (P, B, T) win counts from the last clean counter read. None
        # until we get the first consistent reading (then we sync the shoe).
        self._counts: Optional[tuple[int, int, int]] = None
        self._last_hand = 0
        # Step 9: card-value OCR for true counting (opt-in).
        self.read_cards_enabled = controller.config.vision.read_cards
        self._player_card_names = sorted(
            n for n in self.regions if n.startswith("card_player")
        )
        self._banker_card_names = sorted(
            n for n in self.regions if n.startswith("card_banker")
        )

    # -- framing ----------------------------------------------------------- #
    def capture_frame(self) -> np.ndarray:
        cap = self.controller.config.capture.region
        return self.grabber.grab(Rect(cap.x, cap.y, cap.width, cap.height))

    def _scale(self, frame: np.ndarray) -> tuple[float, float]:
        """Scale from configured (calibration) space to the actual frame size.

        Regions are stored in the resolution they were measured at
        (``capture.region.width/height``). If the live grab comes back at a
        different size (e.g. mss returns logical vs physical pixels, or the
        window resized), we rescale every region by the observed ratio instead
        of assuming a fixed display resolution.
        """
        cap = self.controller.config.capture.region
        fh, fw = frame.shape[:2]
        sx = fw / cap.width if cap.width else 1.0
        sy = fh / cap.height if cap.height else 1.0
        return sx, sy

    def _crop_sub(self, frame: np.ndarray, sub) -> np.ndarray:
        sx, sy = self._scale(frame)
        rect = Rect(
            x=round(sub.x * sx), y=round(sub.y * sy),
            w=round(sub.w * sx), h=round(sub.h * sy),
        )
        return rect.crop(frame)

    def _subimage(self, frame: np.ndarray, name: str) -> Optional[np.ndarray]:
        sub = self.regions.get(name)
        if sub is None:
            return None
        return self._crop_sub(frame, sub)

    # -- main step --------------------------------------------------------- #
    def tick(self) -> PipelineTick:
        """One capture→read→reconcile step.

        The shoe counter (``#N P.. B.. T..``) is the single source of truth. A
        reading is only acted on when it is self-consistent (P+B+T == N); we add
        whatever hands the count says have happened since the last clean read,
        so a missed frame or a two-hand gap is reconciled rather than lost.
        Inconsistent / unreadable frames are skipped silently.
        """
        frame = self.capture_frame()
        tick = PipelineTick()

        counter_img = self._subimage(frame, "shoe_counter")
        if counter_img is not None and not isinstance(self.ocr, NullBackend):
            tick.counter = read_counter(counter_img, self.ocr)

        road_img = self._subimage(frame, "big_road")
        if road_img is not None:
            tick.road = read_big_road(road_img)

        c = tick.counter
        if c is None or not c.consistent:
            return tick  # wait for a clean read; don't guess from a bad frame

        counts = (c.player_wins, c.banker_wins, c.ties)

        # First clean read: sync the shoe to the casino's current depth.
        if self._counts is None:
            self.controller.catch_up(sum(counts), self.burn_cards)
            self._counts = counts
            self._last_hand = c.hand_number
            tick.synced = True
            tick.detection_source = "counter"
            return tick

        # Counter went backwards (a new shoe started at the casino, or an OCR
        # hiccup). We do NOT auto-reshuffle — the user controls new shoes via the
        # New Shoe button. Just rebaseline so a negative delta adds no bogus hands.
        if sum(counts) < sum(self._counts) or c.hand_number < self._last_hand:
            self._counts = counts
            self._last_hand = c.hand_number
            return tick

        # Reconcile: add exactly the hands the counter says have occurred.
        self._advance(self._counts, counts, tick, frame)
        self._counts = counts
        self._last_hand = c.hand_number
        return tick

    def _advance(
        self,
        old: tuple[int, int, int],
        new: tuple[int, int, int],
        tick: PipelineTick,
        frame: np.ndarray,
    ) -> None:
        d_p = max(0, new[0] - old[0])
        d_b = max(0, new[1] - old[1])
        d_t = max(0, new[2] - old[2])
        total = d_p + d_b + d_t
        if total == 0:
            return
        tick.new_hand = True
        tick.hands_added = total
        tick.detection_source = "counter"
        sequence = ["P"] * d_p + ["B"] * d_b + ["T"] * d_t

        if total == 1:
            # Single clean hand: try exact card values (validated vs the winner).
            winner = sequence[0]
            cards = self._try_read_cards(tick, frame, winner)
            if cards is not None and tick.cards is not None:
                self.controller.enter_hand(
                    HandInput(
                        winner=winner,
                        player_total=tick.cards.player_total,
                        banker_total=tick.cards.banker_total,
                        card_values=cards,
                    )
                )
                tick.exact_cards = True
            else:
                self.controller.enter_hand(HandInput(winner, 0, 0))
            tick.winner = winner
            return

        # Multiple hands in one gap: order is unknown, add them estimated.
        for winner in sequence:
            self.controller.enter_hand(HandInput(winner, 0, 0))
        tick.winner = f"{d_p}P/{d_b}B/{d_t}T"

    def _try_read_cards(
        self, tick: PipelineTick, frame: np.ndarray, winner: Optional[str]
    ) -> Optional[List[int]]:
        if not self.read_cards_enabled or isinstance(self.ocr, NullBackend):
            return None
        if not self._player_card_names or not self._banker_card_names:
            return None
        player_imgs = [self._crop_sub(frame, self.regions[n]) for n in self._player_card_names]
        banker_imgs = [self._crop_sub(frame, self.regions[n]) for n in self._banker_card_names]
        result = read_cards(player_imgs, banker_imgs, self.ocr)
        tick.cards = result
        if result is None:
            tick.warnings.append("Card OCR unreadable; using estimated composition")
            return None
        # Validate against the known winner before trusting for exact counting.
        if winner is not None and result.winner != winner:
            tick.warnings.append(
                f"Card OCR winner {result.winner} != detected {winner}; "
                "using estimated composition"
            )
            return None
        return result.all_values

    def reset(self) -> None:
        self._counts = None
        self._last_hand = 0
