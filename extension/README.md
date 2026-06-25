# Baccarat Vision — Chrome extension (DOM reading)

Reads the live baccarat game **directly from the page DOM** — exact counter,
cards (with suits), scores, and result — instead of screen OCR. It feeds each
completed hand to the local engine server (the verified Python: exact baccarat
solver, composition tracking, house edges, bet-spread calculator) and shows the
predictions in an on-page overlay.

This is **read-only analysis** — it never bets or clicks. Check the casino's
Terms of Service before using; automated DOM scraping may violate them and could
risk your account even though this tool only reads.

## How it fits together

```
game iframe (client.petros04.com)
  └─ content.js   reads DOM (counter, cards, result)  ──┐
                                                        │ chrome.runtime message
  background.js   relays to localhost (dodges mixed-content block)
                                                        │ fetch
  http://127.0.0.1:8777   baccarat_vision.server  ⇄  AppController (the engine)
```

## Run it

1. **Start the engine server** (from the project root, any 3.11+ venv):
   ```bash
   .venv/bin/python -m baccarat_vision.server
   ```
   It listens on `http://127.0.0.1:8777`. No GUI, no screen capture.

2. **Load the extension:** Chrome → `chrome://extensions` → enable *Developer
   mode* → *Load unpacked* → select this `extension/` folder.

3. Open the SpinQuest baccarat table. An overlay appears top-right showing the
   status, the predicted next hand + confidence, and a **live redisplay of the
   current hand** (cards with suits + winner) so you can verify the reads.

## How it reads the game (decoded)

* **Counter** — `scoreBoardInfo__totalItem` divs: `#hand`, then P(blue)/B(red)/
  T(green) badges each followed by a count text node.
* **Cards** — each `baccaratCardsStack__card` holds a child with
  `data-locator="{suit}-{rank}"` (e.g. `♣-10`, `♠-9`, `♣-A`). We read the exact
  rank+suit, so counting is exact (with pairs + naturals), no OCR.
* **Scores** — `baccaratCardsStack__score`. Player = leftmost stack, Banker =
  rightmost.

Detection is counter-driven (reconcile by P/B/T delta, like the desktop app),
with exact card values layered in per hand. If a frame can't decode a card it
falls back to estimated for that hand and posts a sample to the server, so it
never stalls. Debug: in the game-frame console, `__bv.readHand()` /
`__bv.readCounter()`.
