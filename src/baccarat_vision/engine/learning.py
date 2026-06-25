"""Online learning over pattern "experts" with full-information feedback.

Baccarat reveals the whole result every hand, so we can grade **every** strategy
each hand (not just the one we picked). Each expert maps the current pattern
state to a bet; after the hand resolves we score each expert's realised profit
and nudge its weight via multiplicative weights (``w *= exp(η·profit)``). The
ensemble pick is the highest-weighted vote. Useful experts grow, useless ones
decay — learned from this user's real shoes, and persisted so it compounds.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .patterns import PatternState

_BET = {"P": "player", "B": "banker"}
_OPP = {"P": "B", "B": "P"}
SCALE = 8.0  # largest single-hand profit (a Tie), used to keep η stable

BET_LABELS = {
    "player": "Player", "banker": "Banker", "tie": "Tie", "either_pair": "Either Pair",
}


def _last(s: PatternState) -> Optional[str]:
    return s.pb_sequence[-1] if s.pb_sequence else None


# -- experts: PatternState (+ optional library) -> bet name or None ---------- #
def _ride(s, lib=None):
    return _BET[s.streak_side] if s.streak_side and 2 <= s.streak_len <= 6 else None


def _fade(s, lib=None):
    return _BET[_OPP[s.streak_side]] if s.streak_side and s.streak_len >= 7 else None


def _chop(s, lib=None):
    l = _last(s)
    return _BET[_OPP[l]] if l and s.chop_score >= 0.55 else None


def _follow_last(s, lib=None):
    l = _last(s)
    return _BET[l] if l else None


def _oppose_last(s, lib=None):
    l = _last(s)
    return _BET[_OPP[l]] if l else None


def _tie_due(s, lib=None):
    return "tie" if s.hands_since.get("T", 0) >= 8 else None


def _pair_due(s, lib=None):
    g = s.hands_since.get("pair")
    return "either_pair" if (g is not None and g >= 6) else None


def _markov(s, lib=None):
    pb = s.pb_sequence
    if len(pb) < 4:
        return None
    key = tuple(pb[-2:])
    nxt: Dict[str, int] = defaultdict(int)
    for i in range(len(pb) - 2):
        if tuple(pb[i:i + 2]) == key:
            nxt[pb[i + 2]] += 1
    total = sum(nxt.values())
    if not nxt or total < 2:
        return None
    winner = max(nxt, key=nxt.get)
    return _BET[winner] if nxt[winner] / total > 0.6 else None


def _markov3(s, lib=None):
    """Trigram Markov: predict from the last 3 outcomes."""
    pb = s.pb_sequence
    if len(pb) < 6:
        return None
    key = tuple(pb[-3:])
    nxt: Dict[str, int] = defaultdict(int)
    for i in range(len(pb) - 3):
        if tuple(pb[i:i + 3]) == key:
            nxt[pb[i + 3]] += 1
    total = sum(nxt.values())
    if not nxt or total < 2:
        return None
    winner = max(nxt, key=nxt.get)
    return _BET[winner] if nxt[winner] / total > 0.6 else None


def _zipper(s, lib=None):
    """Detect PPBB / BBPP doubling pattern; predict the start of the next pair."""
    pb = s.pb_sequence
    if len(pb) < 4:
        return None
    w = pb[-4:]
    if w[0] == w[1] and w[2] == w[3] and w[0] != w[2]:
        return _BET[_OPP[w[2]]]
    return None


def _early_fade(s, lib=None):
    """Fade a streak at exactly 5-6: empirically where break probability rises."""
    if s.streak_side and 5 <= s.streak_len <= 6:
        return _BET[_OPP[s.streak_side]]
    return None


def _flat_banker(s, lib=None):
    return "banker"


def _flat_player(s, lib=None):
    return "player"


EXPERTS: Dict[str, Callable] = {
    "ride-dragon": _ride,
    "fade-streak": _fade,
    "early-fade": _early_fade,
    "play-chop": _chop,
    "follow-last": _follow_last,
    "oppose-last": _oppose_last,
    "tie-due": _tie_due,
    "pair-due": _pair_due,
    "markov": _markov,
    "markov3": _markov3,
    "zipper": _zipper,
    "flat-banker": _flat_banker,
    "flat-player": _flat_player,
}

# Shoe personalities (must match patterns.analyze_patterns). Each gets its own
# weight vector so re-entering a regime (dragon→chop→dragon) reuses what worked
# there last time — near-zero relearn lag.
REGIMES = ("Forming", "Dragon", "Choppy", "Mixed")


def hit(bet: str, winner: str, p_pair: bool, b_pair: bool) -> bool:
    if bet == "player":
        return winner == "P"
    if bet == "banker":
        return winner == "B"
    if bet == "tie":
        return winner == "T"
    if bet == "either_pair":
        return bool(p_pair or b_pair)
    return False


# Every bet on the table (matches config/default.yaml + the game's payout sheet).
ALL_BETS = [
    "player", "banker", "super_6", "tie", "player_pair", "banker_pair",
    "either_pair", "suited_pair", "p_bonus", "b_bonus",
]
_BONUS_LADDER = {9: 30.0, 8: 10.0, 7: 6.0, 6: 4.0, 5: 2.0, 4: 1.0}


def grade(bet: str, o: dict) -> Optional[float]:
    """Net units for a 1-unit bet given an outcome dict, or None if ungradable.

    Side bets that need card detail (Super 6, pairs, suited, bonuses) return
    None on winner-only hands (``exact`` False) so they don't pollute the stats.
    """
    w = o["winner"]
    bt = o.get("banker_total", 0)
    exact = o.get("exact", False)
    if bet == "player":
        return 0.0 if w == "T" else (1.0 if w == "P" else -1.0)
    if bet == "banker":
        if w == "T":
            return 0.0
        if w == "P":
            return -1.0
        return 0.5 if bt == 6 else 1.0
    if bet == "tie":
        return 8.0 if w == "T" else -1.0
    if not exact:
        return None  # the rest need read cards
    pp, bp = o.get("p_pair", False), o.get("b_pair", False)
    ps, bs = o.get("p_suited", False), o.get("b_suited", False)
    nat, margin = o.get("is_natural", False), o.get("margin", 0)
    if bet == "super_6":
        return 15.0 if (w == "B" and bt == 6) else -1.0
    if bet == "either_pair":
        return 5.0 if (pp or bp) else -1.0
    if bet == "player_pair":
        return 11.0 if pp else -1.0
    if bet == "banker_pair":
        return 11.0 if bp else -1.0
    if bet == "suited_pair":
        if ps and bs:
            return 200.0
        if ps or bs:
            return 25.0
        return -1.0
    if bet in ("p_bonus", "b_bonus"):
        side = "P" if bet == "p_bonus" else "B"
        if w == "T":
            return 0.0 if nat else -1.0
        if w != side:
            return -1.0
        if nat:
            return 1.0
        return _BONUS_LADDER.get(margin, -1.0)
    return None


def profit(bet: str, winner: str, banker_total: int, p_pair: bool, b_pair: bool) -> float:
    """Back-compat helper for P/B/T/Either-Pair grading (exact context)."""
    return grade(bet, {
        "winner": winner, "banker_total": banker_total, "exact": True,
        "p_pair": p_pair, "b_pair": b_pair,
    }) or 0.0


@dataclass
class Scoreboard:
    graded: int
    accuracy: float
    baseline_accuracy: float
    profit: float
    baseline_profit: float
    recent_accuracy: float
    best_expert: str
    best_expert_hit: float
    best_expert_profit: float
    profit_per_hand: float       # ensemble mean profit/hand
    profit_lb: float             # 95% lower bound on profit/hand
    significant: bool            # strict: 95% lower bound > 0 (proven edge)
    actionable: bool             # looser: net-positive over the min sample (BET gate)
    verdict: str                 # human-readable confidence verdict
    acts: int = 0                # hands the ensemble has acted on
    min_hands: int = 0           # hands required before BET can unlock
    experts: List[dict] = field(default_factory=list)
    bets: List[dict] = field(default_factory=list)  # every bet's realised record


def _acc(s: dict) -> float:
    return s["wins"] / s["n"] if s["n"] else 0.0


def _mean_lb(profit: float, acts: int, profit2: float, k: int = 20,
             z: float = 1.96) -> tuple:
    """Shrunk mean profit/hand and its lower bound (shrink toward 0)."""
    if acts == 0:
        return 0.0, 0.0
    mean = profit / acts
    var = max(1e-9, profit2 / acts - mean * mean)
    se = (var / acts) ** 0.5
    shrunk = profit / (acts + k)          # k pseudo-hands of zero profit
    return shrunk, mean - z * se           # frequentist lower bound

# Family-wise z for the "proven edge" claim: we test ~10 fixed experts, so a
# Bonferroni-style bound (per-test α≈0.05/10) needs ~2.8σ, not 1.96σ. This is
# what keeps the strict significance gate from being fooled by a lucky run.
_SIG_Z = 2.81


class OnlineLearner:
    MIN_HANDS = 12      # engage within a ~45-hand shoe; still filters early noise
    RECENCY = 12        # window (hands) the BET gate weights toward the current regime

    def __init__(self, eta: float = 0.5, share: float = 0.08) -> None:
        self.eta = eta
        # Fixed-Share: each update mixes this fraction of weight back toward
        # uniform, so no expert can freeze the vote — a newly-hot expert (e.g.
        # play-chop when a dragon breaks into a chop) climbs back in ~1/share
        # hands. Higher = snaps to a new regime faster; lower = steadier.
        self.share = share
        self.weights = {n: 1.0 for n in EXPERTS}          # regime-agnostic vector
        self.regime_weights = {r: {n: 1.0 for n in EXPERTS} for r in REGIMES}
        self.stats = {n: self._fresh() for n in EXPERTS}
        self.ensemble = self._fresh()
        self.base_banker = self._fresh()
        self.base_player = self._fresh()
        self.bet_stats = {b: self._fresh() for b in ALL_BETS}  # every bet on the sheet
        self._recent = deque(maxlen=60)
        self._recent_profit = deque(maxlen=self.RECENCY)  # ensemble P/L, current regime

    @staticmethod
    def _fresh() -> dict:
        return {"n": 0, "wins": 0, "profit": 0.0, "acts": 0, "profit2": 0.0}

    def expert_bets(self, state: PatternState, library=None) -> Dict[str, Optional[str]]:
        return {n: fn(state, library) for n, fn in EXPERTS.items()}

    def _wv(self, regime: Optional[str]) -> Dict[str, float]:
        """Weight vector for a regime (its own if known, else the global one)."""
        return self.regime_weights.get(regime, self.weights) if regime else self.weights

    def pick(self, bets: Dict[str, Optional[str]], regime: Optional[str] = None):
        wv = self._wv(regime)
        votes: Dict[str, float] = defaultdict(float)
        for n, b in bets.items():
            if b:
                votes[b] += wv[n]
        if not votes:
            return "banker", {}
        return max(votes, key=votes.get), dict(votes)

    def _reweight(self, wv: Dict[str, float], graded: Dict[str, float]) -> None:
        """Multiplicative-weights update + renormalise + Fixed-Share on one vector."""
        for n, p in graded.items():
            wv[n] *= math.exp(self.eta * p / SCALE)
        k = len(wv)
        tot = sum(wv.values())
        if tot > 0:
            for n in wv:
                w = wv[n] / tot * k
                wv[n] = (1.0 - self.share) * w + self.share * 1.0

    def _accrue(self, st: dict, p: float) -> None:
        st["acts"] += 1
        st["profit"] += p
        st["profit2"] += p * p
        if p != 0:  # a push carries no win/loss info for the hit-rate
            st["n"] += 1
            st["wins"] += 1 if p > 0 else 0

    def update(self, bets, winner: str, banker_total: int, p_pair: bool, b_pair: bool,
               p_suited: bool = False, b_suited: bool = False, is_natural: bool = False,
               margin: int = 0, exact: bool = False, regime: Optional[str] = None) -> None:
        o = {
            "winner": winner, "banker_total": banker_total, "p_pair": p_pair, "b_pair": b_pair,
            "p_suited": p_suited, "b_suited": b_suited, "is_natural": is_natural,
            "margin": margin, "exact": exact,
        }
        graded: Dict[str, float] = {}
        for n, b in bets.items():
            if not b:
                continue
            p = grade(b, o)
            if p is None:
                continue
            graded[n] = p
            self._accrue(self.stats[n], p)  # stats are regime-agnostic
        # Reweight the global vector AND this hand's regime vector (Fixed-Share
        # inside each), so each regime specialises while the global stays sane.
        self._reweight(self.weights, graded)
        if regime in self.regime_weights:
            self._reweight(self.regime_weights[regime], graded)

        pick, _ = self.pick(bets, regime)
        pp = grade(pick, o)
        if pp is not None:
            self._accrue(self.ensemble, pp)
            self._recent_profit.append(pp)
            if pp != 0:
                self._recent.append(1 if pp > 0 else 0)
        self._accrue(self.base_banker, grade("banker", o))
        self._accrue(self.base_player, grade("player", o))
        # Full-information tracker: grade EVERY bet on the sheet this hand.
        for bet in ALL_BETS:
            g = grade(bet, o)
            if g is not None:
                self._accrue(self.bet_stats[bet], g)

    def reasons_for(self, pick: str, bets, regime: Optional[str] = None) -> List[str]:
        wv = self._wv(regime)
        names = sorted(
            [n for n, b in bets.items() if b == pick],
            key=lambda n: wv[n], reverse=True,
        )
        out = []
        for n in names[:3]:
            st = self.stats[n]
            if st["n"]:
                out.append(f"{n} ({_acc(st) * 100:.0f}% · {st['profit']:+.1f}u)")
            else:
                out.append(n)
        return out

    def set_share(self, share: float) -> None:
        """Tune responsiveness: clamp the Fixed-Share rate to a sane [0, 0.4]."""
        self.share = max(0.0, min(0.4, share))

    def soften_weights(self, toward_uniform: float = 0.5) -> None:
        """Blend the expert weights toward uniform (call at a new shoe).

        A shoe is a fresh regime; carrying a frozen leader into it biases the
        first dozen hands. This relaxes the regime-agnostic *weights* so each
        shoe re-finds its regime, while the long-run *stats* and the per-regime
        weight vectors (the whole point — reuse across shoes) are kept.
        """
        a = max(0.0, min(1.0, toward_uniform))
        for n in self.weights:
            self.weights[n] = (1.0 - a) * self.weights[n] + a * 1.0
        self._recent_profit.clear()  # form is regime-specific; start the shoe clean

    def _best_fixed_edge_lb(self) -> float:
        """Best fixed-expert profit/hand lower bound, family-wise penalised.

        Each expert is a fixed rule, so its realised profit is ~iid and the CI
        is valid (unlike the adaptive ensemble). The max over experts is the
        honest "is any stable rule really winning?" signal.
        """
        best = -9.0
        for n in EXPERTS:
            s = self.stats[n]
            if s["acts"] < self.MIN_HANDS:
                continue
            _, lb = _mean_lb(s["profit"], s["acts"], s["profit2"], z=_SIG_Z)
            best = max(best, lb)
        return best

    def _recent_pph(self) -> float:
        """Ensemble profit/hand weighted toward the current regime.

        Uses the recent window once it has enough hands, else the shrunk
        lifetime mean — so the BET gate reacts to the live regime, not the
        whole-shoe average.
        """
        if len(self._recent_profit) >= 6:
            return sum(self._recent_profit) / len(self._recent_profit)
        e = self.ensemble
        pph, _ = _mean_lb(e["profit"], e["acts"], e["profit2"])
        return pph

    def confident(self) -> bool:
        """BET gate: net-positive over the recent window (looser, regime-aware)."""
        return self.ensemble["acts"] >= self.MIN_HANDS and self._recent_pph() > 0

    def scoreboard(self) -> Scoreboard:
        ranked = sorted(EXPERTS, key=lambda n: self.stats[n]["profit"], reverse=True)
        best = ranked[0]
        e = self.ensemble
        pph = self._recent_pph()                                    # regime-aware
        enough = e["acts"] >= self.MIN_HANDS
        # The strict "proven edge" claim is measured on the FIXED experts (whose
        # realised profit is ~iid, so the CI is valid) with a family-wise
        # penalty — NOT on the adaptive ensemble's own profit, which inflates
        # when it chases hot streaks. In real (edgeless) baccarat this ~never
        # fires; that's the point.
        pph_lb = self._best_fixed_edge_lb()
        significant = enough and pph_lb > 0      # strict: a fixed rule has a real edge
        actionable = enough and pph > 0          # looser: ensemble net-positive now
        if not enough:
            verdict = f"warming up ({e['acts']}/{self.MIN_HANDS} hands)"
        elif significant:
            verdict = f"proven edge (+{pph_lb*100:.1f}u/100 lower bound)"
        elif actionable:
            verdict = f"riding the current run (+{pph*100:.1f}u/100 recent)"
        else:
            verdict = "no edge in the current run — optional only"
        experts = []
        for n in ranked:
            s = self.stats[n]
            _, lb = _mean_lb(s["profit"], s["acts"], s["profit2"])
            experts.append({
                "name": n, "hit": _acc(s), "profit": s["profit"],
                "weight": self.weights[n], "n": s["n"], "edge_lb": lb,
            })
        return Scoreboard(
            graded=e["n"],
            accuracy=_acc(e),
            baseline_accuracy=_acc(self.base_banker),
            profit=e["profit"],
            baseline_profit=self.base_banker["profit"],
            recent_accuracy=(sum(self._recent) / len(self._recent)) if self._recent else 0.0,
            best_expert=best,
            best_expert_hit=_acc(self.stats[best]),
            best_expert_profit=self.stats[best]["profit"],
            profit_per_hand=pph,
            profit_lb=pph_lb,
            significant=significant,
            actionable=actionable,
            verdict=verdict,
            acts=e["acts"],
            min_hands=self.MIN_HANDS,
            experts=experts,
            bets=[self._bet_record(b) for b in ALL_BETS],
        )

    def _bet_record(self, bet: str) -> dict:
        s = self.bet_stats[bet]
        pph, lb = _mean_lb(s["profit"], s["acts"], s["profit2"])
        return {
            "bet": bet, "n": s["acts"], "hit": _acc(s), "profit": s["profit"],
            "per100": pph * 100, "per100_lb": lb * 100,
            "significant": s["acts"] >= self.MIN_HANDS and lb > 0,
        }

    # -- persistence ------------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "weights": self.weights, "regime_weights": self.regime_weights,
            "stats": self.stats, "ensemble": self.ensemble,
            "base_banker": self.base_banker, "base_player": self.base_player,
            "bet_stats": self.bet_stats, "recent": list(self._recent),
            "recent_profit": list(self._recent_profit),
        }

    @staticmethod
    def _load_counter(v: dict) -> dict:
        return {
            "n": int(v.get("n", 0)), "wins": int(v.get("wins", 0)),
            "profit": float(v.get("profit", 0.0)), "acts": int(v.get("acts", v.get("n", 0))),
            "profit2": float(v.get("profit2", abs(float(v.get("profit", 0.0))))),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OnlineLearner":
        self = cls()
        self.weights.update({k: float(v) for k, v in d.get("weights", {}).items() if k in self.weights})
        for r, wv in d.get("regime_weights", {}).items():
            if r in self.regime_weights:
                self.regime_weights[r].update(
                    {k: float(v) for k, v in wv.items() if k in self.regime_weights[r]}
                )
        for k, v in d.get("stats", {}).items():
            if k in self.stats:
                self.stats[k] = cls._load_counter(v)
        for key in ("ensemble", "base_banker", "base_player"):
            if key in d:
                setattr(self, key, cls._load_counter(d[key]))
        for k, v in d.get("bet_stats", {}).items():
            if k in self.bet_stats:
                self.bet_stats[k] = cls._load_counter(v)
        self._recent = deque(d.get("recent", []), maxlen=60)
        self._recent_profit = deque(d.get("recent_profit", []), maxlen=self.RECENCY)
        return self
