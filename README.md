# Baccarat Live-Vision Analyzer

A read-only desktop tool that tracks true shoe composition, computes **honest**
next-hand probabilities, and prices any bet spread across every side bet on the
table. Built for Apple Silicon macOS.

> **Build status:** §10 build order **complete (steps 1–9)** — config + payout
> table + bet-spread calculator, probability/composition engine, the PySide6
> dashboard, screen capture + drag region selector, shoe-counter OCR, Big Road
> CV, the live capture→engine pipeline with the confidence/EV display, SQLite
> persistence + replay, and card-value OCR for true counting (the stretch goal,
> opt-in and validated against the detected winner). 84 tests pass.

---

## Honest disclaimers (read these)

- The **roads** (Big Road, Big Eye Boy, Small Road, Cockroach Pig) are
  *tracking/visualization tools*, not predictive models. `road_weight` is `0.0`
  by default and nothing in the road tracker feeds the predictor.
- The only mathematically defensible edge comes from **card-composition
  tracking** as the shoe depletes. It is small (typically <1% on Banker/Player)
  and only emerges late in the shoe.
- **Tie, Super 6, and pair/bonus bets carry large house edges** (≈8–15%). The UI
  shows the computed edge in a tooltip on every bet so multipliers can't mislead.
- The **confidence meter** measures how far the current shoe deviates from
  baseline — **not** your chance of winning. High = "unusual shoe", nothing more.
- This tool does **not** bet, click, or automate anything inside a casino. It is
  strictly read-only analysis. Not financial advice.

---

## Setup & run

The engine + test suite run on any **Python 3.11+**. For the GUI use **Python
3.11–3.13** — PySide6 ships official wheels for those; on 3.14 the Qt platform
plugin may fail to initialise (PySide6 doesn't support 3.14 yet).

```bash
# With uv (recommended on M1):
uv venv --python 3.13
uv pip install -e ".[dev]"

# Or with plain venv + pip:
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run the dashboard (manual hand entry works with no permissions/OCR):
python -m baccarat_vision.app
```

Core deps (`pydantic`, `pyyaml`, `numpy`, `scipy`, `PySide6`, `opencv-python`,
`mss`, `SQLAlchemy`) cover everything except OCR. The OCR engines are optional
extras since they're heavy (torch):

```bash
pip install -e ".[ocr]"    # easyocr (preferred on M1) + pytesseract fallback
pip install -e ".[macos]"  # pyobjc — Screen Recording preflight + Quartz fallback
```

Without an OCR engine the app still runs: live mode falls back to a
`NullBackend` (no counter reading) and manual hand entry is always available.

**macOS Screen Recording permission** (needed for live capture): System
Settings → Privacy & Security → Screen Recording → enable this app. `app.py`
performs a non-fatal preflight check (via Quartz, if installed) and guides you
if it's missing. Manual hand entry needs no permission.

---

## Using the dashboard (step 3)

The window has four areas (§7): a **Manual Hand Entry** form, a **Prediction**
panel (P/B/T + confidence), a **Bet Spread** panel (stake inputs + live outcome
matrix), and a **Shoe Composition** panel (cards-remaining bars + a Big Road
mirror).

To drive the math by hand:
1. Pick the **Winner** (P/B/T) and the two totals; tick Natural / pairs if shown.
2. Optionally type the dealt **card values** (e.g. `3,4,2,3`, where `0` = any
   ten/face, `1` = Ace) for *exact* composition tracking. Leave it blank and the
   engine removes an estimated ~5 cards from a uniform prior and marks
   composition confidence "low".
3. Click **Enter hand**. Predictions, the outcome matrix, and the shoe update.
4. Enter stakes in the Bet Spread panel to see EV / best / worst / volatility and
   the grouped outcome matrix. **Reshuffle** restores a full shoe.

### Live capture (steps 4–7)

1. **Calibrate the capture region.** Press **F2** (or *Select Region*), drag a
   rectangle over the casino stream, press **Enter**. The geometry is saved to
   `config/default.yaml`. Sub-region offsets (counter / roads / table) are read
   from the config (`regions:`) — load a casino profile (e.g.
   `config/casinos/example.yaml`) that matches your stream's layout.
2. Grant Screen Recording permission and click **● Go Live**. A timer polls at
   `capture.fps` (default 2/s). The shoe counter (`#N P.. B.. T..`) is the
   **single source of truth**: each tick OCRs it and only acts on a reading that
   is self-consistent (`P+B+T == N`). It **reconciles by delta** — adding exactly
   the hands the counts say have happened since the last clean read — so a missed
   frame or a fast two-hand gap is *caught up*, not lost. Bad/unreadable frames
   are skipped silently (no false fires, no tie errors).

On **Go Live** it syncs to the casino's current shoe depth (removing the
10-card opening burn + an estimate for hands already played), so "cards left"
is realistic immediately. A **counter reset** is detected as a new shoe:
reshuffle + re-apply the 10-card burn (`vision.burn_cards`).

By default composition is tracked at the **hand** level (≈5 cards/hand) — good
for the countdown and shoe progress. For exact per-card counting, enable card
reading (below).

### True counting via card OCR (step 9, opt-in)

For genuine card-composition counting, enable card reading:

1. Add card sub-regions to `config/default.yaml` under `regions:` — one per
   card slot, named `card_player_1/2/3` and `card_banker_1/2/3` (crop tightly
   around each card's rank corner).
2. Set `vision.read_cards: true` (and optionally `vision.ocr_backend: easyocr`).
3. Install an OCR engine: `pip install -e ".[ocr]"`.

On each new hand the pipeline OCRs the card ranks, maps them to values
(A=1, 2-9, 10/J/Q/K=0), and **only trusts the read if the resulting winner
matches** the counter/road-derived winner — otherwise it warns and falls back to
estimation. Trusted reads use exact composition and keep confidence "high"; the
status bar shows `exact cards` vs `estimated`. This guard exists because card
OCR during the deal animation is unreliable (motion blur, §6.4).

`card_reader.read_cards()` / `parse_card_rank()` are pure and unit-tested, so you
can validate your region crops offline before going live.

### Replay a logged shoe

Every hand is logged to SQLite (`baccarat_vision.db`) when a `Database` is
attached to the controller. `Database.replay(shoe_id, controller)` re-feeds a
shoe deterministically through a fresh controller — handy for reviewing a
session or regression-testing the engine.

The live pipeline, manual entry, and replay all drive the **same**
`AppController`; the engine and UI don't change between them.

---

## Architecture

```
config/                YAML config + example casino profile (§3)
src/baccarat_vision/
  settings.py          pydantic config models + load/save
  controller.py        UI-agnostic app core (drives engine from hand inputs)
  pipeline.py          live capture→OCR→CV→engine vision pipeline
  app.py               entry point + Screen Recording preflight
  engine/
    probability.py     exact baccarat solver, baseline, effects of removal, edges
    shoe_state.py      per-value composition tracking (exact + estimated)
    predictor.py       composition probabilities + confidence meter
    road_tracker.py    Big Road bookkeeping (informational only)
  betting/
    payout_table.py    per-bet pure payout functions (configurable)
    bet_spread_calc.py HandOutcome, calculator, outcome distribution
    house_edges.py     runtime house-edge computation
  capture/
    screen_grabber.py  mss live grabber + still-image grabber (replay/tests)
    region_selector.py drag-to-select overlay (F2)
  vision/
    ocr_backend.py     pluggable OCR (easyocr / pytesseract / null / callable)
    counter_reader.py  shoe-counter OCR + regex parse + sanity check
    road_reader.py     Big Road HSV cell classification (+ synthetic renderer)
    result_detector.py new-hand detection (counter delta + visual SSIM-ish)
    card_reader.py     card-value OCR for true counting (step 9, validated)
  persistence/
    db.py              SQLAlchemy models, hand logging, replay
  ui/                  PySide6 dashboard panels + live controls
tests/                 pytest suite (84 tests, steps 1–9)
```

### Probability engine

`engine/probability.py` computes outcome probabilities **exactly** by
exhaustively enumerating every reachable deal under the official drawing rules,
weighted by without-replacement draw probabilities. No Monte-Carlo error. The
full-shoe result reproduces the published baseline to six decimals:

| Outcome | Computed | Target (Thorp) |
|---|---|---|
| Banker | 0.458597 | 0.458597 |
| Player | 0.446247 | 0.446247 |
| Tie    | 0.095156 | 0.095156 |

House edges are computed at runtime (never hard-coded) by integrating the
payout functions against this distribution.

---

## Spec corrections (§4.2 / §9)

While implementing the runtime edge computation, three values in the spec's
§4.2 table turned out to be transcription errors. We report the **computed
truth** (which matches the Wizard of Odds appendix and the canonical Dragon
Bonus figures) rather than reproduce the typos. Tests assert the correct values.

| Bet | Spec §4.2 | Computed (correct) | Note |
|---|---|---|---|
| Banker | 1.46% | **1.458%** | ✓ matches |
| Player | 1.24% | **1.235%** | ✓ matches |
| Tie 8:1 | 14.36% | **14.360%** | ✓ matches |
| Pair 11:1 | 10.36% | **10.361%** | ✓ matches |
| **Super 6 15:1** | 13.66% | **13.82%** | spec typo (its own arithmetic gives −13.76%) |
| **Either Pair 5:1** | 14.20% | **13.71%** | spec typo; P(either pair)=14.38% |
| **P Bonus** | "9–10%" | **2.65%** | this is the Dragon Bonus *Player* side (2.65%) |
| **B Bonus** | "9–10%" | **9.37%** | the "9–10%" only applies to the Banker side |
| Suited Pair | ~8.5% | **8.05%** | within stated approximation |

---

## Testing

```bash
.venv/bin/python -m pytest -q
```

84 tests cover (§9): all three §5.3 worked examples, the Banker carve-out, the
full P-Bonus margin ladder, the three suited-pair states, the exact 8-deck
baseline to 4dp, the runtime house edges, shoe-state tracking, the controller /
manual-entry integration, **counter OCR parsing**, **Big Road CV** (synthetic
render → read round-trip, standing in for fixture PNGs), **new-hand detection**,
the **capture geometry + end-to-end pipeline**, **persistence + replay**, and
**card-value OCR** (rank parsing + the validated exact-vs-estimated pipeline path).

The §9 vision tests reference two casino screenshots in `tests/fixtures/`; none
were provided with the spec, so the Big Road test generates its own ground-truth
images via `road_reader.render_big_road`. Drop the real PNGs into
`tests/fixtures/` and the same `read_big_road` / `parse_counter` functions apply.

A dashboard screenshot will be added from a windowed run on Python 3.13 (the GUI
doesn't initialise under the 3.14 interpreter in this dev sandbox). All
non-GUI logic is covered by the passing suite above.
