# Claude Code Prompt: Baccarat Vision Accuracy and Practicality Upgrade

You are Claude Code working inside this repository:

`/Users/dariusniermann/Desktop/Projects/App Development/baccarat_vision`

Your job is to improve Baccarat Vision so it is more accurate, practical, smarter, and better at using all live data already available in the SpinQuest/Iconic21 webpage and the existing Python engine. Keep the tool read-only. Do not add code that clicks, places bets, bypasses access controls, or automates casino actions.

## First Read These Files

Read these before editing:

- `README.md`
- `extension/README.md`
- `extension/manifest.json`
- `extension/background.js`
- `extension/content.js`
- `extension/overlay.css`
- `src/baccarat_vision/server.py`
- `src/baccarat_vision/controller.py`
- `src/baccarat_vision/engine/shoe_state.py`
- `src/baccarat_vision/engine/probability.py`
- `src/baccarat_vision/engine/predictor.py`
- `src/baccarat_vision/engine/learning.py`
- `src/baccarat_vision/engine/patterns.py`
- `src/baccarat_vision/engine/staking.py`
- `src/baccarat_vision/betting/bet_spread_calc.py`
- `src/baccarat_vision/persistence/library.py`
- `config/default.yaml`
- `SpinQuest _ The #1 Social Casino, Fun & Free to Play.html`
- `SpinQuest _ The #1 Social Casino, Fun & Free to Play_files/saved_resource.html`
- `card_samples.html`
- `tests/test_controller.py`
- `tests/test_learning.py`
- `tests/test_vision_library.py`
- `tests/test_real_screenshot.py`
- `tests/test_card_reader.py`

Use `rg` to locate symbols and selectors. Do not assume the current comments are fully accurate; verify against the code and saved webpage source.

## Current Architecture

The project has two paths into one engine:

```text
Chrome extension path
  extension/content.js
    reads SpinQuest/Iconic21 DOM in all frames
    detects counter deltas, current cards, balance/chips
    posts /reset, /hand, /context through chrome.runtime
  extension/background.js
    relays requests to http://127.0.0.1:8777
  src/baccarat_vision/server.py
    stdlib HTTP API around AppController
  src/baccarat_vision/controller.py
    engine orchestration, learning, staking, persistence

Desktop/OCR path
  src/baccarat_vision/pipeline.py
    captures screen regions, OCRs counter/cards, reconciles counter deltas
  src/baccarat_vision/controller.py
    same AppController as extension path
```

Important engine modules:

```text
src/baccarat_vision/engine/
  probability.py   exact value-level baccarat enumeration
  shoe_state.py    per-value 0-9 composition state
  predictor.py     composition probabilities and confidence
  learning.py      online expert ensemble and all-bet grading
  patterns.py      road/pattern analysis and entertainment-style advice
  staking.py       bankroll-aware stake suggestion

src/baccarat_vision/betting/
  bet_spread_calc.py  full outcome matrix for active spreads
  payout_table.py     payout functions
  house_edges.py      runtime house-edge calculation

src/baccarat_vision/persistence/
  library.py       SQLite shoe archive, learner state, rich-hand side-bet stats
  db.py            SQLAlchemy persistence used by desktop/replay path
```

Existing tests already cover the exact solver, controller integration, learning, staking, rich shoe archive, screenshots, and optional EasyOCR fixtures.

## Webpage Source Observations

The saved wrapper page is `SpinQuest _ The #1 Social Casino, Fun & Free to Play.html`, saved from:

`https://spinquest.com/casino/games/iconic21_launch_asia_ncmbal_01`

It is mainly the SpinQuest shell plus analytics and a game iframe. The actual game frame is represented by:

`SpinQuest _ The #1 Social Casino, Fun & Free to Play_files/saved_resource.html`

That game frame is from `https://client.petros04.com/...` and includes the Iconic21 app bundles:

- `runtime.4992d418.js`
- `vendor.74affe59.js`
- `main.93f64bbf.js`
- `no-commission-baccarat-desktop.4d6b10d6.min.css`

The extension's `all_frames: true` is necessary because the baccarat DOM lives in the game iframe, not just the top SpinQuest wrapper.

Useful source selectors and data points found in `saved_resource.html`, `card_samples.html`, and the CSS:

- Table identity and limits:
  - `[data-locator="table-limits-title"]` -> `Live Baccarat 1 NC`
  - `[data-locator="table-limits-amount"]` -> `SC 1 - SC 5,000`
  - The expanded limits table includes per-bet min/max/reward data for Player, Banker, Banker wins on 6, Super 6, Tie, P/B Pair, Either Pair, Suited Pair, and P/B Bonus.
- Balance and result footer:
  - `[data-locator="balance"]`
  - `[data-locator="balance-amount"]` -> example `SC 65`
  - `[data-locator="last-win"]`
  - `[data-locator="last-win-amount"]`
  - `[data-locator="round-info-date"]`
  - `[data-locator="round-info-round-id"]`
- Video/phase:
  - `[data-locator="video-player"]`
  - visible round timer and result banner exist in screenshot fixtures.
- Scoreboard:
  - CSS prefix `scoreBoardInfo`
  - counter items use `scoreBoardInfo__totalItem-*`
  - `card_samples.html` includes examples like `#7`, P count `2`, B count `5`, T count `0`.
- Cards:
  - card stacks use `baccaratCardsStack__cards-*`
  - scores use `baccaratCardsStack__score-*`
  - card wrappers/cards use `baccaratCardsStack__card-*`
  - inner card face uses `data-locator` with exact suit and rank, examples:
    - `data-locator="\u2663-10"` for club 10
    - `data-locator="\u2665-8"` for heart 8
    - `data-locator="\u2665-J"` for heart jack
  - This means the browser extension can know exact rank and suit, not just baccarat value.
- History/roads:
  - CSS includes `baccarat__history-*`
  - screenshots show Big Road, derived roads, P/B/T totals, and road cells.
- Chips and live stake context:
  - screenshots show chip denominations `1`, `5`, `25`, `100`, `500`, `2.5K`
  - current visible bets and percentages are in the table surface.

## Problems to Fix

Prioritize these issues.

1. The extension is a 462-line monolith. It mixes DOM selectors, parsing, polling, state reconciliation, API calls, and overlay rendering. Extract a testable DOM reader module and a small state/reconciliation layer.

2. `extension/content.js` sets `DEBUG = true`. Make debug logging configurable and default it off.

3. The overlay CSS sets `pointer-events: none` on `#bv-panel`, but the overlay contains a clickable `<details>` section. Either make only passive areas pointerless or provide a small interactive hit area for expanding/collapsing details.

4. API calls have no timeout, no health status, no backoff, and no clear offline recovery. Add timeout handling and a `/health` endpoint or equivalent.

5. `ThreadingHTTPServer` mutates one global controller with no lock. Add a controller lock or switch to a safer request model so concurrent `/hand`, `/context`, and `/snapshot` calls cannot interleave state changes.

6. `/debug-card` appends raw HTML to `card_samples.html` in the project root. Replace this with a sanitized, rotating debug capture under a dedicated path such as `debug_samples/`, and strip unrelated DOM/PII.

7. `ShoeState.composition_confidence` flips back to `"high"` after any exact hand, even when the shoe contains unknown burn cards and estimated catch-up hands. Replace the binary flag with explicit metrics:
   - exact hands
   - estimated hands
   - unknown burn cards
   - estimated cards removed
   - exact cards removed
   - an `exactness_ratio` or confidence grade that cannot become fully high after a partial exact read.

8. The browser can read exact rank and suit, but the engine currently tracks only baccarat values 0-9. Add a richer card model so all available data is preserved:
   - `rank`
   - `suit`
   - baccarat `value`
   - side (`player` or `banker`)
   - position/order within hand
   - source (`dom`, `ocr`, `estimated`)

9. Pair/suited-pair probabilities in `distribution_from_analysis()` are independent approximations. Because the DOM exposes rank/suit, improve side-bet probability calculations from live remaining rank/suit composition. Keep the exact value-level solver for P/B/T, but make pair, suited pair, and bonus side-bet probability estimates more faithful.

10. Multi-hand gaps currently turn count deltas into ordered arrays like all Player wins, then Banker wins, then Ties. That loses chronology. Use the road/history DOM where possible to reconstruct the actual order of missed hands. If order cannot be recovered, mark those hands explicitly as order-unknown and exclude them from sequence-pattern learning.

11. Current recommendations are driven heavily by pattern experts and "due" ideas. Keep entertainment/pattern readouts separate from mathematically defensible recommendations. Main bet advice should be tied to EV, composition exactness, sample quality, and confidence intervals. Side bets should not be recommended merely because they are "due".

12. The extension reads balance with `[class*="balance__value"]`, but the saved game source has stable `data-locator="balance-amount"`. Prefer stable `data-locator` selectors where they exist, then class-prefix fallbacks.

13. The extension does not parse the full table limits/payout table even though it exists in the DOM. Use table source data to update `/context` with:
   - currency
   - table min/max
   - bet-specific min/max
   - payout schedule if visible
   - game variant (`Live Baccarat 1 NC`)

14. Persistence needs stronger provenance. Store per-hand metadata:
   - round id
   - round timestamp
   - table name
   - table limits
   - source frame URL/domain
   - selector version
   - exactness metrics
   - raw normalized DOM snapshot hash, not raw full HTML

15. The server response includes a lot of useful data, but the overlay does not make quality clear enough. Add concise quality indicators:
   - engine connected/offline
   - exact DOM cards vs estimated
   - selector health
   - current round id
   - composition exactness percentage
   - whether the current suggestion is EV-backed or observe-only

## Implementation Plan

Make the smallest coherent set of code changes that materially improves accuracy and practicality. Do not rewrite the whole app.

### Phase 1: Structured DOM Snapshot

Create a structured DOM snapshot layer for the extension.

Suggested shape:

```js
{
  frame: {
    href,
    isGameFrame,
    selectorVersion
  },
  table: {
    name,
    currency,
    minBet,
    maxBet,
    betLimits,
    payouts
  },
  round: {
    id,
    timestamp,
    phase,
    timer,
    lastWin
  },
  counter: {
    hand,
    P,
    B,
    T,
    consistent
  },
  cards: {
    player: [{ rank, suit, value, index }],
    banker: [{ rank, suit, value, index }],
    totals: { player, banker },
    winner,
    isNatural,
    pPair,
    bPair,
    pSuitedPair,
    bSuitedPair
  },
  bankroll: {
    currency,
    balance,
    chipDenoms
  },
  roads: {
    visibleTail,
    sequenceTail
  },
  health: {
    selectorsFound,
    warnings
  }
}
```

Expose it at `window.__bv.snapshot()` for debugging. Keep `window.__bv.readCounter()` and `window.__bv.readHand()` as compatibility helpers.

Prefer `data-locator` selectors first:

- `table-limits-title`
- `table-limits-amount`
- `balance-amount`
- `last-win-amount`
- `round-info-date`
- `round-info-round-id`
- inner card `data-locator` values containing suit/rank

Use class-prefix fallbacks only where stable locators do not exist.

### Phase 2: Rich Hand API Contract

Extend `/hand` to accept rich cards while preserving backwards compatibility.

Current body:

```json
{
  "winner": "P",
  "player_total": 9,
  "banker_total": 1,
  "card_values": [2, 7, 1, 0]
}
```

Target body:

```json
{
  "winner": "P",
  "player_total": 9,
  "banker_total": 1,
  "is_natural": true,
  "p_pair": false,
  "b_pair": false,
  "p_suited_pair": false,
  "b_suited_pair": false,
  "cards": {
    "player": [
      {"rank": "2", "suit": "h", "value": 2, "index": 0},
      {"rank": "7", "suit": "d", "value": 7, "index": 1}
    ],
    "banker": [
      {"rank": "A", "suit": "d", "value": 1, "index": 0},
      {"rank": "K", "suit": "s", "value": 0, "index": 1}
    ]
  },
  "counter": {"hand": 1, "P": 1, "B": 0, "T": 0},
  "round": {"id": "1098388985", "timestamp": "13 Jun 2026 19:44:39"},
  "source": {"kind": "dom", "selector_version": 1}
}
```

Add Python dataclasses or typed dictionaries in `controller.py` for rich card inputs. Continue accepting `card_values` from the desktop/OCR path.

### Phase 3: Exactness-Aware Shoe State

Update `ShoeState` to track exactness instead of a binary confidence string. Add tests for:

- burn cards make confidence partial/low
- catch-up hands remain estimated even after later exact cards
- exact DOM cards improve exactness but do not erase unknown history
- reset returns to a clean full shoe, then burn lowers exactness again

Keep existing `composition_confidence` in snapshots for UI compatibility, but derive it from the new metrics.

### Phase 4: Rank/Suit Composition for Side Bets

Use rank/suit data where available.

Minimum useful improvement:

- Continue value-level counts for exact P/B/T enumeration.
- Add optional rank/suit remaining counts when rich DOM cards are present.
- Compute current pair and suited-pair probabilities from remaining rank/suit counts instead of full-shoe constants.
- Pass those probabilities into `distribution_from_analysis()` or a replacement that can accept dynamic side-bet state probabilities.

Better improvement:

- Compute conditional side-bet probabilities based on current rank/suit composition and first-two-card structure.
- Keep performance acceptable. Cache by rank/suit composition key.

Do not break the existing exact value solver. It is a strength of the app.

### Phase 5: Better Reconciliation and History

Counter deltas are the source of truth for how many P/B/T results occurred, but they do not preserve order when more than one hand is missed. Use the history/road DOM to recover order:

- Parse a recent visible road/history tail when possible.
- Compare old tail and new tail to append exact missed sequence.
- If the tail cannot resolve order, record outcomes as order-unknown.
- Feed order-known hands into sequence learning.
- Exclude order-unknown hands from pattern experts but still use them for composition estimation and aggregate counts.

Add tests for:

- single-hand exact DOM path
- multi-hand gap with recoverable history order
- multi-hand gap without order recovery
- counter reset/new shoe archive
- implausible jumps are ignored or rebaselined without polluting data

### Phase 6: Recommendation Quality

Rework displayed recommendations so they are practical and honest:

- Separate "composition EV" from "pattern notes".
- Do not label side bets as recommended solely because a pattern says they are due.
- Gate any actionable recommendation with:
  - adequate exactness
  - enough hands or sufficient shoe penetration
  - positive expected value after payout rules
  - confidence interval or conservative lower bound where learning data is involved
- Display "observe" when no real edge exists.
- Keep bankroll/staking conservative. Avoid Martingale escalation as a default.

The app can still show road/pattern color, but it should not imply that pattern color changes baccarat odds.

### Phase 7: Server and Extension Robustness

Add:

- `/health` endpoint with version and state summary.
- API timeout in the background relay.
- Offline/backoff state in the content script.
- Controller mutation lock in `server.py`.
- Better error responses with stable JSON shape.
- A path-configured debug sample writer that sanitizes and rotates files.

### Phase 8: Persistence and Audit Tools

Extend `ShoeLibrary` so it can answer:

- How many shoes are rich vs estimated?
- How many hands have exact DOM cards?
- How many hands were order-unknown?
- Profit/EV by bet and by source quality.
- Selector/version drift over time.

Add a CLI or library helper that prints a short audit report. Keep it dependency-free if possible.

## Test Requirements

Run the existing suite:

```bash
.venv/bin/python -m pytest -q
```

If `.venv` is not suitable, inspect `pyproject.toml` and use the available Python environment with `pythonpath = ["src"]`.

Add focused tests for the new behavior. Suggested files:

- `tests/test_dom_contract.py` or a Python-side fixture test around saved HTML/card samples
- `tests/test_rich_hand_input.py`
- `tests/test_shoe_exactness.py`
- `tests/test_dynamic_side_bet_probabilities.py`
- `tests/test_server_api.py`

If adding JavaScript tests, keep tooling minimal and document how to run them. If no JS test runner is introduced, make the content-script parser functions simple enough to fixture-test through saved normalized snapshots.

## Acceptance Criteria

The work is done when:

- Existing Python tests pass.
- New tests cover rich DOM hands, exactness metrics, and table/balance parsing.
- The extension can expose a structured `window.__bv.snapshot()` from the game frame.
- `/hand` can accept rich card objects and still accepts legacy `card_values`.
- The engine preserves rank/suit data for persistence and side-bet analytics.
- Composition confidence no longer falsely returns to high after unknown burn/catch-up cards.
- Side-bet suggestions are not based only on due/pattern signals.
- Overlay shows connection and data-quality state clearly.
- Debug captures are sanitized and no longer append arbitrary HTML to `card_samples.html` by default.
- The implementation remains read-only.

## Development Constraints

- Keep changes scoped and consistent with existing project style.
- Prefer existing modules over adding broad new abstractions.
- Do not add heavy dependencies without a strong reason.
- Preserve the exact baccarat solver and current public APIs where practical.
- Keep the Chrome extension Manifest V3-compatible.
- Never add automated betting, clicking, or account-interaction behavior.
- Update `extension/README.md` and `README.md` only where behavior or commands change.

## UI Layout Requirements

Every visual element in the overlay and any new UI must follow these rules without exception. Apply these to all existing and new overlay HTML/CSS.

### No horizontal overflow
- The overlay panel must never cause horizontal scrolling or clip content off the left or right edge of the viewport.
- All containers must use `box-sizing: border-box`.
- Use `max-width: 100%` on the panel and all child elements.
- Never use fixed pixel widths that could exceed the viewport width. Use `min()`, percentages, or `max-width` constraints instead.
- Use `overflow-x: hidden` on the panel as a hard guard.
- No absolutely positioned element may extend beyond the viewport edges. Constrain with `left`/`right` bounds and `max-width`.

### Vertical-only scrolling
- If content overflows, it must scroll vertically only (`overflow-y: auto; overflow-x: hidden`).
- Never introduce horizontal scrollbars.

### Text fitting and wrapping
- All text must wrap rather than overflow. Apply `word-break: break-word; overflow-wrap: anywhere` to all text containers.
- Never use `white-space: nowrap` unless the text is guaranteed to fit (e.g. a single short label with a max-width guard).
- Long values (round IDs, URLs, selector strings) must truncate with `text-overflow: ellipsis` inside a constrained container, or wrap.
- Font sizes must be relative (`rem`/`em`) or use `clamp()` so they scale down on narrow viewports. Do not use fixed `px` font sizes above `14px` for body content.

### Icon alignment and sizing
- All icons must be vertically centred with their adjacent text using `display: flex; align-items: center; gap: <value>`.
- Icons must have an explicit, bounded size (`width` + `height` or `font-size` for icon fonts). Never let an icon inherit an unconstrained size.
- Use `flex-shrink: 0` on icons so they do not compress when text is long.
- Icon and label must stay on the same line unless the combined width genuinely cannot fit, in which case the label wraps below and the icon remains top-aligned (`align-items: flex-start`).

### Autoresizing elements
- Buttons, badges, and pill labels must size to their content with padding, not fixed widths.
- Tables or grid layouts must use `table-layout: auto` or CSS grid `minmax` columns so cells shrink/grow with content.
- Any scrollable list must have a `max-height` so it does not push other content off screen.

### Testing these rules
After each UI change, verify:
1. Shrink the viewport to 320 px wide — no horizontal scrollbar appears, no content is clipped.
2. All icon+label pairs are vertically centred.
3. Long strings (50+ character round IDs, full URLs) wrap or truncate cleanly.
4. The panel remains fully within the viewport at all supported sizes.

## Useful Review Notes

The current code already has important strengths:

- Exact value-level baccarat enumeration in `probability.py`.
- One shared `AppController` for manual, desktop vision, replay, and extension paths.
- Persistence for shoes and rich per-hand side-bet grading.
- Real SpinQuest screenshot fixtures under `tests/fixtures/`.
- DOM cards expose exact rank/suit via `data-locator`, which is better than OCR.

The highest-impact upgrade is not a new predictive trick. It is preserving and exploiting the exact data the page already gives you: rank, suit, table limits, balance, round id, history order, source quality, and exactness. Build around that.
