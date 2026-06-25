"""Local JSON API over the verified engine — the bridge for the browser extension.

The Chrome extension reads the live game DOM (exact cards, winner, counter) and
POSTs each completed hand here.  The server runs it through the same
:class:`~baccarat_vision.controller.AppController` (exact baccarat solver, house
edges, bet-spread calculator, composition tracking) the desktop app uses, and
returns predictions. No OCR, no screen capture — the data is exact.

Dependency-free (stdlib ``http.server``) so it starts with::

    python -m baccarat_vision.server      # listens on http://127.0.0.1:8777

Endpoints (all accept an optional ``?slot=A`` / ``?slot=B`` query param so that
two tables can run simultaneously without stepping on each other):

  GET  /snapshot          -> current predictions + composition + bet spread
  GET  /health            -> ok / version / hands played
  POST /probe             -> {total, player_wins, banker_wins, ties}
                             Checks whether any slot already holds this shoe and
                             returns {slot, match, server_hands, gap} so the
                             extension can resume instead of resetting.
  POST /hand              -> {winner, player_total, banker_total, ...}
  POST /reset             -> {burn_cards?:int, hands?:int}
  POST /catch_up          -> {hands:int}  add N estimated hands without resetting
  POST /context           -> {currency, balance, denoms, ...}
  POST /bets              -> {bets:{bet_name: stake}}
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .controller import AppController, HandInput

HOST = "127.0.0.1"
PORT = 8777
VERSION = "0.5.0"

# ── Dual-slot shoe tracking ───────────────────────────────────────────────── #
# Two independent AppControllers let the user monitor two tables at once.
# All requests include ?slot=A (default) or ?slot=B.  The /probe endpoint
# assigns the right slot automatically on connect / refresh.
_SLOT_NAMES = ("A", "B")
_SLOTS: Dict[str, AppController] = {}
_SLOT_ACCESSED: Dict[str, float] = {}  # slot → last-used epoch for LRU eviction
_LIBRARY = None  # shared ShoeLibrary — both slots write to the same SQLite DB

# One global lock serialises all controller mutations (same as before).
_LOCK = threading.Lock()

# Sanitised, rotating debug captures live here (not appended to project files).
_DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "debug_samples"
_DEBUG_MAX = 50


def _snapshot_dict(controller: AppController) -> Dict[str, Any]:
    s = controller.snapshot()
    return {
        "shoe": {"hands_played": s.hands_played},
        "mystic": None if s.mystic is None else {
            "pick": s.mystic.pick,
            "pick_label": s.mystic.pick_label,
            "vibe": s.mystic.vibe,
            "personality": s.mystic.personality,
            "reasons": s.mystic.reasons,
            "confident": s.mystic.confident,
            "verdict": s.mystic.verdict,
        },
        "pattern": None if s.pattern is None else {
            "streak_side": s.pattern.streak_side,
            "streak_len": s.pattern.streak_len,
            "is_dragon": s.pattern.is_dragon,
            "chop_score": s.pattern.chop_score,
            "hands_since": s.pattern.hands_since,
            "personality": s.pattern.personality,
            "counts": s.pattern.counts,
        },
        "library": s.library_stats,
        "learning": None if s.learning is None else {
            "graded": s.learning.graded,
            "accuracy": s.learning.accuracy,
            "baseline_accuracy": s.learning.baseline_accuracy,
            "profit": s.learning.profit,
            "baseline_profit": s.learning.baseline_profit,
            "recent_accuracy": s.learning.recent_accuracy,
            "best_expert": s.learning.best_expert,
            "best_expert_hit": s.learning.best_expert_hit,
            "best_expert_profit": s.learning.best_expert_profit,
            "profit_per_hand": s.learning.profit_per_hand,
            "profit_lb": s.learning.profit_lb,
            "significant": s.learning.significant,
            "actionable": s.learning.actionable,
            "verdict": s.learning.verdict,
            "acts": s.learning.acts,
            "min_hands": s.learning.min_hands,
            "experts": s.learning.experts[:5],
            "bets": s.learning.bets,
        },
        "staking": None if s.staking is None else {
            "main_bet": s.staking.main_bet,
            "main_label": s.staking.main_label,
            "stake": s.staking.stake,
            "unit": s.staking.unit,
            "strategy": s.staking.strategy,
            "currency": s.staking.currency,
            "note": s.staking.note,
            "side_bets": s.staking.side_bets,
            "total": s.staking.total(),
            "spread_label": s.staking.spread_label,
            "spread_legs": s.staking.spread_legs,
            "spread_total": s.staking.spread_total,
            "spread_affordable": s.staking.spread_affordable,
        },
        "bankroll": s.bankroll,
        "vision": s.vision,
        "vote_summary": s.vote_summary,
        "template_match": s.template_match,
        "calibration": s.calibration,
        "dynamic_spread": None if s.dynamic_spread is None else {
            "main_bet": s.dynamic_spread.main_bet,
            "main_label": s.dynamic_spread.main_label,
            "total_stake": s.dynamic_spread.total_stake,
            "total_ev": s.dynamic_spread.total_ev,
            "unit": s.dynamic_spread.unit,
            "phase": s.dynamic_spread.phase,
            "signal": s.dynamic_spread.signal,
            "composition_signal": s.dynamic_spread.composition_signal,
            "learner_signal": s.dynamic_spread.learner_signal,
            "pattern_signal": s.dynamic_spread.pattern_signal,
            "multiplier": s.dynamic_spread.multiplier,
            "note": s.dynamic_spread.note,
            "currency": s.dynamic_spread.currency,
            "affordable": s.dynamic_spread.affordable,
            "legs": [
                {
                    "bet": l.bet, "label": l.label, "stake": l.stake,
                    "units": l.units, "ev": l.ev, "reason": l.reason,
                }
                for l in s.dynamic_spread.legs
            ],
            "pair_probs": s.dynamic_spread.pair_probs,
            "kelly_stake": s.dynamic_spread.kelly_stake,
            "kelly_fraction": s.dynamic_spread.kelly_fraction,
        },
    }


import re as _re

# Strip anything that could carry PII (chat text, player names, balances).
_PII_PATTERNS = [
    _re.compile(r'(data-locator="(?:author-name|chat[^"]*|balance[^"]*)")[^>]*>[^<]*', _re.I),
    _re.compile(r"\b(?:GC|SC)\s*[\d,]+", _re.I),
]


def _write_debug_sample(html: str) -> None:
    """Write one sanitised card/scoreboard sample to a rotating debug dir."""
    html = (html or "").strip()
    if not html:
        return
    for pat in _PII_PATTERNS:
        html = pat.sub(lambda m: m.group(1) + ">" if m.groups() else "•", html)
    try:
        _DEBUG_DIR.mkdir(exist_ok=True)
        existing = sorted(_DEBUG_DIR.glob("sample_*.html"))
        for old in existing[: max(0, len(existing) - _DEBUG_MAX + 1)]:
            old.unlink(missing_ok=True)
        (_DEBUG_DIR / f"sample_{int(time.time() * 1000)}.html").write_text(
            html + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _get_slot(slot: str) -> AppController:
    """Return (creating if needed) the AppController for ``slot``."""
    global _LIBRARY
    slot = slot.upper() if slot.upper() in _SLOT_NAMES else "A"
    if slot not in _SLOTS:
        if _LIBRARY is None:
            from .persistence.library import ShoeLibrary
            _LIBRARY = ShoeLibrary()
        _SLOTS[slot] = AppController(library=_LIBRARY)
    _SLOT_ACCESSED[slot] = time.time()
    return _SLOTS[slot]


def _probe(total: int, player_wins: int, banker_wins: int, ties: int) -> Dict[str, Any]:
    """Find the slot whose sequence best matches the given counter state.

    Matching criteria (all must hold):
      • |server_hands - total| ≤ 5
      • |server_P - player_wins| ≤ 3
      • |server_B - banker_wins| ≤ 3

    Returns the best-matching slot (or LRU slot if no match) with:
      slot, match, server_hands, gap (how many hands behind the server is).
    """
    best_slot: Optional[str] = None
    best_delta = 9999

    for slot in _SLOT_NAMES:
        if slot not in _SLOTS:
            continue
        seq = _SLOTS[slot].sequence
        s_total = len(seq)
        s_P = seq.count("P")
        s_B = seq.count("B")
        total_delta = abs(s_total - total)
        if total_delta <= 5 and abs(s_P - player_wins) <= 3 and abs(s_B - banker_wins) <= 3:
            if total_delta < best_delta:
                best_delta = total_delta
                best_slot = slot

    if best_slot is not None:
        seq = _SLOTS[best_slot].sequence
        s_total = len(seq)
        return {"slot": best_slot, "match": True,
                "server_hands": s_total, "gap": max(0, total - s_total)}

    # No match — assign the least recently used slot.
    lru = min(_SLOT_NAMES, key=lambda s: _SLOT_ACCESSED.get(s, 0.0))
    return {"slot": lru, "match": False, "server_hands": 0, "gap": total}


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, *args) -> None:  # quiet
        pass

    def _parse(self):
        """Return (clean_path, slot, query_dict)."""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        slot = (qs.get("slot", ["A"])[0] or "A").upper()
        if slot not in _SLOT_NAMES:
            slot = "A"
        return parsed.path.rstrip("/"), slot

    def _send(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self) -> None:
        self._send({})

    def do_GET(self) -> None:
        path, slot = self._parse()
        if path == "/health":
            with _LOCK:
                ctrl = _get_slot(slot)
                s = ctrl.snapshot()
            return self._send({
                "ok": True, "version": VERSION, "slot": slot,
                "hands": s.hands_played,
                "shoes": (s.library_stats or {}).get("shoes", 0),
                "predicting": s.prediction.predicting,
            })
        if path in ("/snapshot", ""):
            with _LOCK:
                self._send(_snapshot_dict(_get_slot(slot)))
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path, slot = self._parse()
        data = self._body()
        if path == "/debug-card":
            _write_debug_sample(str(data.get("html", "")))
            return self._send({"ok": True})
        if path == "/probe":
            # Probe is read-only — no lock needed, and must NOT create a slot.
            result = _probe(
                total=int(data.get("total", 0)),
                player_wins=int(data.get("player_wins", 0)),
                banker_wins=int(data.get("banker_wins", 0)),
                ties=int(data.get("ties", 0)),
            )
            return self._send(result)
        with _LOCK:
            self._do_post_locked(path, slot, data)

    def _do_post_locked(self, path: str, slot: str, data: Dict[str, Any]) -> None:
        ctrl = _get_slot(slot)
        try:
            if path == "/hand":
                ctrl.enter_hand(HandInput(
                    winner=data["winner"],
                    player_total=int(data.get("player_total", 0)),
                    banker_total=int(data.get("banker_total", 0)),
                    is_natural=bool(data.get("is_natural", False)),
                    p_pair=bool(data.get("p_pair", False)),
                    b_pair=bool(data.get("b_pair", False)),
                    p_suited_pair=bool(data.get("p_suited_pair", False)),
                    b_suited_pair=bool(data.get("b_suited_pair", False)),
                    card_values=data.get("card_values") or None,
                ))
            elif path == "/reset":
                burn = int(data.get("burn_cards", 10))
                hands = int(data.get("hands", 0))
                if hands > 0:
                    ctrl.catch_up(hands, burn)
                else:
                    ctrl.start_new_shoe(burn)
            elif path == "/catch_up":
                # Add N estimated hands to the current shoe without resetting.
                # Used when the server is slightly behind after a page refresh.
                hands = max(0, int(data.get("hands", 0)))
                for _ in range(hands):
                    ctrl.shoe.record_hand_estimated()
            elif path == "/context":
                ctrl.set_context(
                    currency=data.get("currency"),
                    balance=data.get("balance"),
                    denoms=data.get("denoms"),
                    min_bet=data.get("min_bet"),
                    max_bet=data.get("max_bet"),
                    strategy=data.get("strategy"),
                    responsiveness=data.get("responsiveness"),
                )
            elif path == "/bets":
                ctrl.clear_bets()
                for name, stake in (data.get("bets") or {}).items():
                    ctrl.set_bet(name, float(stake))
            else:
                return self._send({"error": "not found"}, 404)
        except (KeyError, ValueError, TypeError) as exc:
            return self._send({"error": str(exc)}, 400)
        self._send(_snapshot_dict(ctrl))


def make_server(controller: AppController | None = None, port: int = PORT) -> ThreadingHTTPServer:
    """Create the HTTP server.  If ``controller`` is supplied it becomes slot A
    (used by tests and the desktop app); otherwise both slots are created lazily
    from the shared ShoeLibrary on first request.
    """
    global _LIBRARY
    if controller is not None:
        _SLOTS["A"] = controller
        _SLOT_ACCESSED["A"] = time.time()
        if controller.library is not None:
            _LIBRARY = controller.library
    return ThreadingHTTPServer((HOST, port), _Handler)


def main() -> int:
    server = make_server()
    print(f"Baccarat Vision engine API on http://{HOST}:{PORT}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
