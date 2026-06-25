"""Growing library of real shoes (sqlite3, dependency-free).

Every completed shoe's outcome sequence is archived here. From the accumulated
shoes we compute **empirical** pattern stats — e.g. how often a streak that
reached length L actually continued — which the mystic advisor folds into its
leans. The more you play, the more the model is shaped by *your* real shoes.

These empirics tend toward the true (near-constant) probabilities as the sample
grows; that's expected. The point is a living, inspectable record of real hands.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Dict, List, Optional

DEFAULT_PATH = "baccarat_library.sqlite"


class ShoeLibrary:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS shoes ("
            "id INTEGER PRIMARY KEY, created_at REAL, sequence TEXT, hands INTEGER)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS predictions ("
            "id INTEGER PRIMARY KEY, created_at REAL, pick TEXT, winner TEXT, "
            "hit INTEGER, profit REAL)"
        )
        # Rich per-hand record (the "full vision" data): enough to grade EVERY
        # side bet for any stored shoe — winner, totals, pairs, suited, naturals.
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS hands ("
            "id INTEGER PRIMARY KEY, shoe_id INTEGER, seq INTEGER, winner TEXT, "
            "player_total INTEGER, banker_total INTEGER, p_pair INTEGER, "
            "b_pair INTEGER, p_suited INTEGER, b_suited INTEGER, is_natural INTEGER, "
            "margin INTEGER, cards TEXT)"
        )
        self._conn.commit()
        self._cache: Optional[Dict] = None
        self._vision_cache: Optional[List[dict]] = None

    # -- rich per-hand storage (full vision) ------------------------------- #
    def archive_shoe(self, records: List[dict], sequence: List[str]) -> int:
        """Archive a completed shoe with full per-hand detail (cards + side bets)."""
        shoe_id = self.archive(sequence)  # winner-sequence row (also gates stubs)
        if shoe_id == 0:
            return 0
        for i, r in enumerate(records):
            self._conn.execute(
                "INSERT INTO hands (shoe_id, seq, winner, player_total, banker_total, "
                "p_pair, b_pair, p_suited, b_suited, is_natural, margin, cards) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (shoe_id, i, r.get("winner"), int(r.get("player_total", 0)),
                 int(r.get("banker_total", 0)), int(bool(r.get("p_pair"))),
                 int(bool(r.get("b_pair"))), int(bool(r.get("p_suited"))),
                 int(bool(r.get("b_suited"))), int(bool(r.get("is_natural"))),
                 int(r.get("margin", 0)), ",".join(str(v) for v in r.get("cards", []))),
            )
        self._conn.commit()
        self._vision_cache = None
        return shoe_id

    def vision_stats(self) -> List[dict]:
        """Aggregate every side bet across all stored rich hands (full vision)."""
        if getattr(self, "_vision_cache", None) is not None:
            return self._vision_cache
        from ..engine.learning import grade

        side = ["super_6", "player_pair", "banker_pair", "either_pair",
                "suited_pair", "p_bonus", "b_bonus"]
        agg = {b: {"n": 0, "hits": 0, "profit": 0.0} for b in side}
        rows = self._conn.execute(
            "SELECT winner, banker_total, p_pair, b_pair, p_suited, b_suited, "
            "is_natural, margin FROM hands"
        )
        for w, bt, pp, bp, ps, bs, nat, mg in rows:
            o = {"winner": w, "banker_total": bt, "p_pair": bool(pp), "b_pair": bool(bp),
                 "p_suited": bool(ps), "b_suited": bool(bs), "is_natural": bool(nat),
                 "margin": mg, "exact": True}
            for b in side:
                g = grade(b, o)
                if g is not None:
                    a = agg[b]
                    a["n"] += 1
                    a["profit"] += g
                    a["hits"] += 1 if g > 0 else 0
        out = []
        for b in side:
            a = agg[b]
            out.append({"bet": b, "n": a["n"], "hits": a["hits"],
                        "hit_rate": (a["hits"] / a["n"]) if a["n"] else 0.0,
                        "profit": a["profit"],
                        "per100": (a["profit"] / a["n"] * 100) if a["n"] else 0.0})
        self._vision_cache = out
        return out

    def audit(self) -> Dict[str, int]:
        rich = {r[0] for r in self._conn.execute("SELECT DISTINCT shoe_id FROM hands")}
        shoes = list(self._conn.execute("SELECT id, hands FROM shoes"))
        return {
            "shoes": len(shoes),
            "rich_shoes": len(rich),
            "winner_only_shoes": sum(1 for s in shoes if s[0] not in rich),
            "rich_hands": list(self._conn.execute("SELECT count(*) FROM hands"))[0][0],
            "stub_shoes": sum(1 for s in shoes if (s[1] or 0) < 8),
        }

    def prune_stubs(self, min_hands: int = 8) -> int:
        """Drop short/partial shoes that aren't usable for the model."""
        ids = [r[0] for r in self._conn.execute(
            "SELECT id FROM shoes WHERE hands < ?", (min_hands,))]
        for sid in ids:
            self._conn.execute("DELETE FROM hands WHERE shoe_id=?", (sid,))
            self._conn.execute("DELETE FROM shoes WHERE id=?", (sid,))
        self._conn.commit()
        self._cache = None
        self._vision_cache = None
        return len(ids)

    # -- learner state + prediction ledger --------------------------------- #
    def save_learner(self, state: dict) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES ('learner', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(state),),
        )
        self._conn.commit()

    def load_learner(self) -> Optional[dict]:
        row = self._conn.execute("SELECT value FROM kv WHERE key='learner'").fetchone()
        return json.loads(row[0]) if row else None

    def record_prediction(self, pick: str, winner: str, hit: bool, profit: float) -> None:
        self._conn.execute(
            "INSERT INTO predictions (created_at, pick, winner, hit, profit) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), pick, winner, 1 if hit else 0, float(profit)),
        )
        self._conn.commit()

    # -- writing ----------------------------------------------------------- #
    def archive(self, sequence: List[str]) -> int:
        seq = "".join(s for s in sequence if s in ("P", "B", "T"))
        if len(seq) < 5:  # ignore stubs
            return 0
        cur = self._conn.execute(
            "INSERT INTO shoes (created_at, sequence, hands) VALUES (?, ?, ?)",
            (time.time(), seq, len(seq)),
        )
        self._conn.commit()
        self._cache = None  # invalidate empirics
        return cur.lastrowid

    # -- reading ----------------------------------------------------------- #
    def all_sequences(self) -> List[str]:
        return [row[0] for row in self._conn.execute("SELECT sequence FROM shoes")]

    def _empirics(self) -> Dict:
        if self._cache is not None:
            return self._cache
        reached: Dict[int, int] = {}
        continued: Dict[int, int] = {}
        shoes = 0
        total_hands = 0
        for seq in self.all_sequences():
            shoes += 1
            total_hands += len(seq)
            pb = [s for s in seq if s in ("P", "B")]
            # Walk runs; for each prefix length L of a run, it "reached" L; it
            # "continued" if the run is longer than L.
            i = 0
            while i < len(pb):
                j = i
                while j < len(pb) and pb[j] == pb[i]:
                    j += 1
                run_len = j - i
                for length in range(1, run_len + 1):
                    reached[length] = reached.get(length, 0) + 1
                    if length < run_len:
                        continued[length] = continued.get(length, 0) + 1
                i = j
        self._cache = {
            "shoes": shoes, "total_hands": total_hands,
            "reached": reached, "continued": continued,
        }
        return self._cache

    def streak_continuation(self, length: int) -> Optional[float]:
        """Empirical P(a streak of `length` continues), or None if too little data."""
        e = self._empirics()
        r = e["reached"].get(length, 0)
        if r < 12:  # need a minimum sample to trust it
            return None
        return e["continued"].get(length, 0) / r

    def stats(self) -> Dict[str, int]:
        e = self._empirics()
        return {"shoes": e["shoes"], "hands": e["total_hands"]}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
