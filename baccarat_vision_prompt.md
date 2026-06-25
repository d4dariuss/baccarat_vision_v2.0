# Baccarat Live-Vision Analyzer — Claude Code Build Prompt

> **Target machine:** MacBook Pro M1 (Apple Silicon, macOS 14+)
> **Goal:** Build a configurable Python app that screen-captures a user-defined region of a live baccarat stream, reads the roads + result history via OCR/CV, tracks true shoe composition, computes honest next-hand probabilities with a confidence meter, and outputs a bet-spread payout calculator covering every side bet shown on screen.

---

## 0. Honest Disclaimers (bake these into the README and app footer)

- The **roads** (Big Road, Big Eye Boy, Small Road, Cockroach Pig) are *tracking tools*, not predictive models. Treat them as visualization only.
- The only mathematically defensible edge comes from **card composition tracking** as the shoe depletes (Thorp 1984 / Griffin's *Theory of Blackjack* baccarat appendix / Walker's published effects of card removal). The edge is small — typically <1% on Banker/Player and only emergent in late shoe.
- Tie, Super 6, and pair bets carry house edges of ~14%, ~15%, and ~10%+ respectively. The app should display these clearly so the user isn't misled by payout multipliers.
- The "confidence meter" reflects deviation from baseline probabilities given current shoe composition — **not** road patterns.

---

## 1. Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ (ARM64 native) | M1 wheels available for all deps |
| Screen capture | `mss` (cross-platform, fast) + `pyobjc-framework-Quartz` fallback | Reliable on macOS, supports region capture |
| Computer vision | `opencv-python` | Template matching for chips, roads, result indicators |
| OCR | `easyocr` (preferred for M1 GPU) or `pytesseract` | Read shoe counter (#24, P:13, B:10, T:1), bet amounts |
| Math/stats | `numpy`, `scipy` | Probability calculations, monte carlo |
| UI | `PySide6` (Qt6) | Native macOS feel, region selector overlay, dashboard |
| Config | `pydantic` + YAML | Type-safe, easy to edit |
| Persistence | SQLite via `sqlalchemy` | Log every hand, replay shoes |
| Packaging | `uv` for env mgmt + `py2app` for standalone build | M1-native, fast |

**Required macOS permission:** Screen Recording (System Settings → Privacy & Security → Screen Recording). App must detect missing permission on launch and guide the user.

---

## 2. Project Structure

```
baccarat_vision/
├── pyproject.toml
├── README.md
├── config/
│   ├── default.yaml          # Default settings
│   └── casinos/
│       └── stake_us.yaml     # Per-casino region maps + payout tables
├── src/
│   ├── capture/
│   │   ├── region_selector.py    # Draggable overlay to pick capture rect
│   │   └── screen_grabber.py     # mss loop, throttled
│   ├── vision/
│   │   ├── road_reader.py        # Parse the 4 roads from pixels
│   │   ├── counter_reader.py     # OCR the #24 / P:13 / B:10 / T:1 strip
│   │   ├── result_detector.py    # Detect when a new hand result lands
│   │   └── card_reader.py        # (Stretch) OCR dealt card values if visible
│   ├── engine/
│   │   ├── shoe_state.py         # Tracks cards remaining per rank
│   │   ├── probability.py        # Card-removal effect math
│   │   ├── road_tracker.py       # Pure pattern bookkeeping (informational)
│   │   └── predictor.py          # Combines composition + confidence scoring
│   ├── betting/
│   │   ├── payout_table.py       # All side-bet payouts (configurable)
│   │   └── bet_spread_calc.py    # Given {bet: amount} → outcome matrix
│   ├── ui/
│   │   ├── main_window.py        # Dashboard
│   │   ├── prediction_panel.py   # P/B/T % + confidence bar
│   │   ├── bet_panel.py          # Bet spread input + outcome calc
│   │   └── shoe_panel.py         # Cards remaining, road mirror, history
│   └── persistence/
│       └── db.py
└── tests/
    ├── test_probability.py
    ├── test_bet_spread.py
    └── fixtures/                 # Saved PNGs of known game states
```

---

## 3. Configuration Schema (`config/default.yaml`)

```yaml
capture:
  region:                       # User-selected via overlay, persisted here
    x: 0
    y: 0
    width: 1920
    height: 1080
  fps: 2                        # Polling rate — 2/sec is plenty
  display_id: 0                 # Multi-monitor support

regions:                        # All relative to capture.region
  shoe_counter:    {x: 22,   y: 970,  w: 380, h: 30}    # "#24 P 13 B 10 T 1"
  big_road:        {x: 22,   y: 1005, w: 620, h: 130}
  big_eye_boy:     {x: 22,   y: 1140, w: 310, h: 70}
  small_road:      {x: 22,   y: 1215, w: 310, h: 65}
  cockroach_pig:   {x: 335,  y: 1140, w: 310, h: 65}
  table_result:    {x: 800,  y: 700,  w: 400, h: 200}   # Center of table
  bet_panel:       {x: 740,  y: 985,  w: 845, h: 215}

decks_per_shoe: 8               # Standard baccarat
penetration_pct: 75             # When dealer reshuffles
banker_rule: "ez_baccarat"      # Confirmed: this table reduces Banker payout to 0.5:1 when Banker wins with 6 (no 5% commission)

payouts:                        # Net payout multipliers (winnings on $1 bet). Confirmed from "Live Baccarat 1 NC" rules screen.
  player:                 1.0
  banker:                 1.0
  banker_six:             0.5   # Banker wins with total of 6 → main Banker bet pays half
  tie:                    8.0
  super_6:                15.0  # Separate side bet: Banker wins with total of 6
  player_pair:            11.0
  banker_pair:            11.0
  either_pair:            5.0
  suited_pair_one_hand:   25.0  # Exactly one of P/B has a suited pair
  suited_pair_both_hands: 200.0 # Both hands have suited pairs
  p_bonus:                      # Dragon-style ladder
    natural_win:   1.0
    natural_tie:   "push"
    win_by_9:      30.0
    win_by_8:      10.0
    win_by_7:      6.0
    win_by_6:      4.0
    win_by_5:      2.0
    win_by_4:      1.0
    win_by_1_to_3: "lose"       # Implicit: not in payout list → loss
    loss:          "lose"
  b_bonus:                      # Same ladder as p_bonus
    natural_win:   1.0
    natural_tie:   "push"
    win_by_9:      30.0
    win_by_8:      10.0
    win_by_7:      6.0
    win_by_6:      4.0
    win_by_5:      2.0
    win_by_4:      1.0

max_bets:                       # Table limits (SC)
  player:        5000
  banker:        5000
  super_6:       5000
  tie:           1000
  player_pair:   500
  banker_pair:   500
  either_pair:   1000
  suited_pair:   50
  p_bonus:       200
  b_bonus:       200
min_bet: 1                      # All bets share min of 1 SC

ui:
  always_on_top: true
  opacity: 0.95
  theme: dark

prediction:
  min_hands_before_predicting: 10
  composition_weight: 1.0       # Pure card-counting math
  road_weight: 0.0              # Default OFF — keep honest. User can raise if they want pattern bias.
  confidence_floor: 0.0
  confidence_ceiling: 1.0
```

---

## 4. Core Math (encode these exactly)

### 4.1 Baseline probabilities (8-deck shoe, full)
- **Banker wins:** 0.458597
- **Player wins:** 0.446247
- **Tie:** 0.095156

Source: Thorp, *The Mathematics of Gambling*; reproduced in Wizard of Odds 8-deck analysis. Implement as a default constant.

### 4.2 House edges (display in UI next to each bet)

Computed for the confirmed `Live Baccarat 1 NC` payout structure, 8-deck shoe at start:

| Bet | House Edge | Notes |
|---|---|---|
| Banker | **~1.46%** | The 0.5:1 reduction on Banker-with-6 replaces the usual 5% commission |
| Player | **~1.24%** | Unchanged from standard baccarat |
| Tie 8:1 | **~14.36%** | Avoid except as a hedge |
| Super 6 (15:1) | **~13.66%** | P(Banker wins with 6) ≈ 5.39% × 15 − 94.61% ≈ −13.66% |
| P Pair / B Pair (11:1) | **~10.36%** | P(pair) ≈ 7.47% |
| Either Pair (5:1) | **~14.20%** | P(at least one pair) ≈ 14.30% |
| Suited Pair (25:1 / 200:1) | **~8.5%** | Combined edge across both tiers |
| P Bonus / B Bonus | **~9–10%** | Compute exactly via simulation; varies with shoe composition |

**Sources:** Banker/Player/Tie baseline from Thorp (1984); pair probabilities from Wizard of Odds 8-deck baccarat appendix; EZ Baccarat banker edge from Eliot Jacobson, *Advanced Advantage Play* (2015). All edges should be **recomputed at runtime** in `engine/probability.py` rather than hard-coded — the tests in §9 should pin the computed values to within 0.01% of the values above.

The UI must show these edges in tooltip form next to every bet input so the user can't unconsciously chase the high-multiplier bets without seeing their cost.

### 4.3 Card composition tracking
Maintain a vector `remaining[rank] = count` for ranks 0-9 (where 10/J/Q/K all count as 0, A=1).

After each completed hand where card values are visible (or inferable from the displayed total), decrement `remaining`.

If individual cards **aren't** visible (the screenshot shows only "PLAYER 3"-style totals during deal), the app should:
- Decrement an estimated 4–6 cards per hand from a uniform prior
- Mark composition confidence as "low" until card values become observable
- Provide a **manual card entry mode** for users who want true counting

### 4.4 Probability recalculation
When `remaining` changes, recompute P(B), P(P), P(T) via:

1. **Fast approximation (default):** Use Thorp's published "effects of removal" — each card removed shifts P(Banker) and P(Player) by a known delta (table in Griffin, 1999). Implement as a lookup.
2. **Exact (stretch):** Monte Carlo simulation — deal N=50,000 hands from current `remaining` using the official baccarat drawing rules, count outcomes. Cache results, only re-run when composition shifts materially.

### 4.5 Confidence meter
```python
confidence = abs(P_current - P_baseline) / max_observed_deviation
# Clipped to [0, 1], displayed as a 0–100% bar
# Color: gray <30%, yellow 30–60%, green >60%
```
A high confidence does **not** mean "high chance of winning" — it means "current shoe deviates meaningfully from baseline." Make this distinction explicit in the UI tooltip.

---

## 5. Bet Spread Calculator (the feature you specifically asked for)

**Input:** dict of `{bet_name: stake_in_dollars}`
**Output:** every distinct game outcome with its net profit/loss, sortable.

### 5.1 Outcome dimensions

Every baccarat hand can be fully described by this tuple:

```python
@dataclass(frozen=True)
class HandOutcome:
    winner: Literal["P", "B", "T"]
    player_total: int            # 0–9
    banker_total: int            # 0–9
    is_natural: bool             # Either side dealt 8 or 9 on first 2 cards
    p_pair: bool
    b_pair: bool
    p_suited_pair: bool          # Implies p_pair
    b_suited_pair: bool          # Implies b_pair

    @property
    def margin(self) -> int:
        return abs(self.player_total - self.banker_total)
```

Not all combinations are reachable (e.g. margin 0 only with `winner="T"`). The engine enumerates only reachable outcomes.

### 5.2 Per-bet payout logic (encode each as a pure function)

```python
def payout_banker(stake: float, o: HandOutcome) -> float:
    if o.winner == "T": return 0.0                         # push
    if o.winner == "P": return -stake
    # Banker wins
    if o.banker_total == 6: return stake * 0.5             # EZ carve-out
    return stake * 1.0

def payout_super_6(stake: float, o: HandOutcome) -> float:
    return stake * 15.0 if (o.winner == "B" and o.banker_total == 6) else -stake

def payout_tie(stake: float, o: HandOutcome) -> float:
    return stake * 8.0 if o.winner == "T" else -stake

def payout_player_pair(stake: float, o: HandOutcome) -> float:
    return stake * 11.0 if o.p_pair else -stake

def payout_either_pair(stake: float, o: HandOutcome) -> float:
    return stake * 5.0 if (o.p_pair or o.b_pair) else -stake

def payout_suited_pair(stake: float, o: HandOutcome) -> float:
    if o.p_suited_pair and o.b_suited_pair: return stake * 200.0
    if o.p_suited_pair or  o.b_suited_pair: return stake * 25.0
    return -stake

def payout_p_bonus(stake: float, o: HandOutcome) -> float:
    if o.winner == "T":
        return 0.0 if o.is_natural else -stake             # natural tie pushes, regular tie loses
    if o.winner != "P": return -stake
    if o.is_natural: return stake * 1.0
    return {9: 30.0, 8: 10.0, 7: 6.0, 6: 4.0, 5: 2.0, 4: 1.0}.get(o.margin, -1.0) * (stake if o.margin >= 4 else 1)
    # Wins by 1–3 lose. Implement cleanly with a guard instead of the trick above in real code.
```

(`payout_player`, `payout_banker_pair`, `payout_b_bonus` mirror the patterns above.)

### 5.3 Worked examples (all three must appear as test cases)

**Example A — your original case:** `{player: $5, tie: $1}`

| Outcome | Player bet | Tie bet | Net |
|---|---|---|---|
| Player wins (any margin) | +$5 | −$1 | **+$4** |
| Banker wins (any margin, incl. with 6) | −$5 | −$1 | −$6 |
| Tie | $0 push | +$8 | **+$8** |

**Example B — Banker carve-out matters:** `{banker: $10, super_6: $1}`

| Outcome | Banker bet | Super 6 bet | Net |
|---|---|---|---|
| Banker wins not-with-6 | +$10 | −$1 | +$9 |
| Banker wins with 6 | +$5 | +$15 | **+$20** |
| Player wins | −$10 | −$1 | −$11 |
| Tie | $0 push | −$1 | −$1 |

**Example C — Bonus ladder:** `{p_bonus: $2}`, Player wins 9 vs 0, non-natural

→ Margin = 9, not natural → 30:1 → **net +$60**

### 5.4 Calculator output

For a given bet spread, the calculator returns:

- **Outcome table** — one row per distinct net result, with the probability mass that produces it (computed from current shoe composition, §4).
- **Total at risk** = Σ stakes
- **Expected value** = Σ (P_outcome × net_outcome)
- **Best case** / **Worst case** = max/min net
- **Volatility (σ)** = probability-weighted standard deviation of net
- **Visual** — bar chart of net vs probability, with EV line overlaid

Rows are grouped intelligently so the user sees ~10–15 meaningful outcome buckets rather than the full ~200+ tuple space. Grouping logic: identical net result → single row; bonus-bet margin distinctions only shown when a bonus bet is active.

---

## 6. Computer Vision Tasks

### 6.1 Shoe counter OCR
The strip `#24  P 13  B 10  T 1` is high-contrast text. Use EasyOCR with a regex post-filter:
```regex
#(\d+)\s+P\s*(\d+)\s+B\s*(\d+)\s+T\s*(\d+)
```
Sanity check: `P + B + T` should equal `hand_number` ± small margin.

### 6.2 Big Road parsing
The Big Road is a grid where each cell is either empty, a blue P circle, or a red B circle (sometimes with a green diagonal slash for tie + a small dot for natural/pair).

Approach:
1. Compute grid cell size from the configured region.
2. For each cell, classify center pixel HSV → `{empty, P, B}`.
3. Detect tie slashes (green diagonal across cell).
4. Reconstruct the sequence of hands.

Cross-validate against the OCR counter — if they disagree, log and surface a warning.

### 6.3 Result detection (trigger for "new hand")
Two strategies, use both:
- **Counter change:** When OCR strip increments, a new hand happened.
- **Visual cue:** The center-table result (e.g., "PLAYER 3") changes — detect via SSIM diff against prior frame.

When a new hand is detected:
1. Pull the new road state.
2. Update `shoe_state`.
3. Recompute predictions.
4. Log to SQLite.

### 6.4 (Stretch goal) Card value reading
If the user wants true card counting, add a step that crops the card areas during the deal animation and OCRs/CV-classifies rank. This is non-trivial because of motion blur — defer to v2.

---

## 7. UI / Dashboard Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Baccarat Vision  ●LIVE    Shoe #24 — Hand 24/~70           │
├──────────────────────────────┬──────────────────────────────┤
│  PREDICTION                  │  BET SPREAD                  │
│                              │                              │
│  Banker  47.2% ████████░░    │  [+] Player        $ 5.00    │
│  Player  44.1% ███████░░░    │  [+] Tie           $ 1.00    │
│  Tie      8.7% █░░░░░░░░░    │  [+] Banker Pair   $ 0.00    │
│                              │  ...                         │
│  Confidence: 34%  ▓▓▓▓░░░░   │                              │
│  "Shoe shifted slightly      │  Total at risk:  $ 6.00      │
│   toward Banker"             │  EV:             −$ 0.07     │
│                              │  Best case:      +$ 8.00     │
│                              │  Worst case:     −$ 6.00     │
├──────────────────────────────┴──────────────────────────────┤
│  OUTCOME MATRIX                                             │
│  Outcome              Probability   Net      Highlight      │
│  Tie                  8.7%          +$8.00   ⭐             │
│  Player win           44.1%         +$4.00                  │
│  Banker win (Super 6) 5.2%          −$6.00                  │
│  ...                                                        │
├─────────────────────────────────────────────────────────────┤
│  SHOE COMPOSITION                                           │
│  0/T/J/Q/K  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░ 124/128 remaining            │
│  Aces       ▓▓▓▓▓▓▓▓▓▓▓░░░░░  28/32                        │
│  ...                                                        │
└─────────────────────────────────────────────────────────────┘
```

- **Always-on-top window** so it overlays the casino stream.
- **Configurable opacity** (so user can see through it).
- **Region selector mode:** F2 brings up a translucent overlay where the user click-drags to define the capture rectangle and each sub-region.

---

## 8. Setup & Run

```bash
# Install uv if missing
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
cd baccarat_vision
uv venv --python 3.11
uv pip install -e ".[dev]"

# Grant Screen Recording permission when macOS prompts
# Then:
uv run python -m baccarat_vision.app

# First-run wizard:
# 1. Pick capture region by dragging
# 2. Calibrate sub-regions (or load a saved casino profile)
# 3. Confirm payout table for your casino
# 4. Start
```

---

## 9. Testing Requirements

- **`tests/test_bet_spread.py`** — all three worked examples in §5.3 must be encoded as test cases and pass to four decimal places. Additionally:
  - `{banker: $10}` with Banker-6 outcome → +$5.00 (carve-out)
  - `{banker: $10}` with Banker-not-6 outcome → +$10.00
  - `{p_bonus: $1}` for every margin tier (4 through 9, plus natural win, natural tie, regular loss, win-by-1, win-by-2, win-by-3)
  - `{suited_pair: $1}` for the three states (neither / one hand / both hands)
- **`tests/test_probability.py`** — baseline 8-deck P(B)=0.458597, P(P)=0.446247, P(T)=0.095156 must match to 4dp. Computed house edges must match §4.2 to within 0.01%.
- **`tests/test_vision.py`** — both provided screenshots in `tests/fixtures/`:
  - Live table screenshot → parser extracts `hand=24, P=13, B=10, T=1`
  - Payout rules screenshot → if parsed at startup, loads into the exact config of §3
- **Snapshot tests** for road parsing using saved PNGs of known game states.

---

## 10. Build Order (suggested)

1. Config + payout table + bet spread calculator (pure logic, no vision yet — fully testable).
2. Probability engine (composition tracking + baseline).
3. UI skeleton with manual hand entry (so the math works even without CV).
4. Screen capture + region selector.
5. OCR for shoe counter.
6. CV for Big Road.
7. Confidence meter + EV display.
8. Persistence + replay mode.
9. (Stretch) Card-value OCR for true counting.

---

## 11. Non-Goals (don't build these)

- ❌ Any automated betting / clicking inside the casino — strictly a read-only analysis tool.
- ❌ Claims of guaranteed profit. The UI must surface the house edge for every bet.
- ❌ Road-pattern "predictions" enabled by default. The road tracker is informational; `road_weight: 0.0` in default config.

---

## 12. Deliverables

1. Working Python app runnable via `uv run python -m baccarat_vision.app` on M1 macOS 14+.
2. Complete test suite (`pytest`) with the cases listed in §9.
3. README with permission setup, region calibration walkthrough, and a screenshot of the dashboard.
4. One example casino profile YAML (`config/casinos/example.yaml`) matching the reference screenshot's layout.
