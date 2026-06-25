"""Pattern / "mystic" model — varied, road-style betting leans.

This is the *entertainment* layer the user asked for: it reads streaks (dragons),
chops, "due" counters, and the shoe's personality, and produces a varied pick
(Player / Banker / Tie / a side bet) with reasons and a "vibe" score. It can
also fold in **empirical continuation rates from a growing library of real
shoes**, so the leans adapt to what this user's shoes actually do.

Honest note kept in code (and shown small in the UI): these patterns do NOT
change the true per-hand probabilities — baccarat hands are independent. The
composition model remains the source of real odds; this is pattern-flavoured
advice, by request.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_SIDE_TO_BET = {"P": "player", "B": "banker"}
_OPP = {"P": "B", "B": "P"}


@dataclass
class PatternState:
    sequence: List[str]          # full P/B/T order
    pb_sequence: List[str]       # P/B only (ties removed)
    streak_side: Optional[str]   # side of the current run
    streak_len: int              # length of the current run (P/B)
    is_dragon: bool              # streak_len >= 4
    chop_score: float            # 0-1, how alternating the recent shoe is
    hands_since: Dict[str, int]  # hands since last P / B / T / pair
    counts: Dict[str, int]       # totals incl. ties
    personality: str             # "Forming" | "Dragon" | "Choppy" | "Mixed"


@dataclass
class MysticAdvice:
    pick: str                    # bet name: player/banker/tie/either_pair/...
    pick_label: str
    vibe: float                  # 0-1 "confidence"
    personality: str
    reasons: List[str] = field(default_factory=list)
    votes: Dict[str, float] = field(default_factory=dict)
    confident: bool = False      # True only when a measured edge backs the pick
    verdict: str = ""            # human-readable confidence verdict


_BET_LABELS = {
    "player": "Player", "banker": "Banker", "tie": "Tie",
    "either_pair": "Either Pair", "player_pair": "Player Pair",
    "banker_pair": "Banker Pair",
}


def analyze_patterns(
    sequence: List[str], hands_since_pair: Optional[int] = None
) -> PatternState:
    """Compute streak / chop / due / personality signals from the outcome list."""
    pb = [s for s in sequence if s in ("P", "B")]

    # Current run length (over P/B only).
    streak_side: Optional[str] = pb[-1] if pb else None
    streak_len = 0
    for s in reversed(pb):
        if s == streak_side:
            streak_len += 1
        else:
            break

    # Chop score: recency-weighted fraction of alternating transitions.
    # More recent flips count more so a fresh chop registers quickly.
    window = pb[-9:]
    if len(window) <= 1:
        chop_score = 0.0
    else:
        n = len(window) - 1
        total_w = alt_w = 0.0
        for i in range(1, len(window)):
            w = i / n  # weight rises linearly toward the present
            total_w += w
            if window[i] != window[i - 1]:
                alt_w += w
        chop_score = alt_w / total_w if total_w else 0.0

    # "Due" counters: hands since the last P / B / T / pair.
    hands_since: Dict[str, int] = {}
    for token in ("P", "B", "T"):
        gap = 0
        for s in reversed(sequence):
            if s == token:
                break
            gap += 1
        hands_since[token] = gap
    if hands_since_pair is not None:
        hands_since["pair"] = hands_since_pair

    counts = {t: sequence.count(t) for t in ("P", "B", "T")}

    # Run-length stats for personality.
    runs: List[int] = []
    if pb:
        cur = 1
        for i in range(1, len(pb)):
            if pb[i] == pb[i - 1]:
                cur += 1
            else:
                runs.append(cur)
                cur = 1
        runs.append(cur)
    avg_run = sum(runs) / len(runs) if runs else 0.0

    if len(pb) < 8:
        personality = "Forming"
    elif streak_len >= 4 or avg_run >= 2.2:
        personality = "Dragon"
    elif chop_score >= 0.6:
        personality = "Choppy"
    else:
        personality = "Mixed"

    return PatternState(
        sequence=list(sequence),
        pb_sequence=pb,
        streak_side=streak_side,
        streak_len=streak_len,
        is_dragon=streak_len >= 4,
        chop_score=chop_score,
        hands_since=hands_since,
        counts=counts,
        personality=personality,
    )


def derived_road_signals(pb_sequence: List[str]) -> dict:
    """Big Eye Boy, Small Road, Cockroach road signals from P/B sequence.

    Each derived road compares the last completed run to an earlier run:
      Big Eye Boy — compare to 2 columns ago (offset 1)
      Small Road  — compare to 3 columns ago (offset 2)
      Cockroach   — compare to 4 columns ago (offset 3)

    Same length → current side continues (red).
    Different length → switch (blue).
    Returns None for each road that doesn't have enough history.
    """
    if not pb_sequence:
        return {"big_eye_boy": None, "small_road": None, "cockroach": None}

    # Build list of completed runs and track the current in-progress side.
    runs: List[tuple] = []
    cur_side = pb_sequence[0]
    cur_len = 1
    for s in pb_sequence[1:]:
        if s == cur_side:
            cur_len += 1
        else:
            runs.append((cur_side, cur_len))
            cur_side = s
            cur_len = 1
    # cur_side is the in-progress run (not yet a completed column).

    def _signal(offset: int) -> Optional[str]:
        """Compare last completed run length to run offset+1 positions ago."""
        if len(runs) < offset + 2:
            return None
        last_len = runs[-1][1]
        ref_len = runs[-(offset + 1)][1]
        if last_len == ref_len:
            return cur_side           # red — continue current side
        return "P" if cur_side == "B" else "B"  # blue — switch

    return {
        "big_eye_boy": _signal(1),
        "small_road":  _signal(2),
        "cockroach":   _signal(3),
    }


@dataclass
class MysticConfig:
    dragon_ride_max: int = 6      # ride a streak up to this length
    dragon_break_at: int = 7      # beyond this, call it "due to break"
    tie_due_after: int = 8        # hands since a tie before Tie feels "due"
    pair_due_after: int = 6
    chop_threshold: float = 0.55


class MysticAdvisor:
    """Blends pattern signals (and optional library empirics) into one lean."""

    def __init__(self, config: Optional[MysticConfig] = None) -> None:
        self.config = config or MysticConfig()

    def advise(
        self, state: PatternState, library: Optional["object"] = None
    ) -> MysticAdvice:
        c = self.config
        votes: Dict[str, float] = defaultdict(float)
        reasons: List[str] = []

        if len(state.pb_sequence) < 3:
            return MysticAdvice(
                pick="banker", pick_label="Banker", vibe=0.0,
                personality=state.personality,
                reasons=["Shoe still forming — no pattern yet"],
                votes={},
            )

        side, n = state.streak_side, state.streak_len
        last = state.pb_sequence[-1]

        # 1) Streak / dragon: ride it, then call it due to break.
        if side and n >= 2:
            if n <= c.dragon_ride_max:
                w = min(n, 5) * 1.0
                votes[_SIDE_TO_BET[side]] += w
                reasons.append(f"{n}-in-a-row {('Player' if side=='P' else 'Banker')} — ride the dragon")
            elif n >= c.dragon_break_at:
                votes[_SIDE_TO_BET[_OPP[side]]] += 1.6
                reasons.append(f"{n}-long streak — due to break, fade it")

        # 2) Chop: trendless alternation -> bet the opposite of the last.
        if state.chop_score >= c.chop_threshold and last:
            votes[_SIDE_TO_BET[_OPP[last]]] += state.chop_score * 2.2
            reasons.append(f"Choppy shoe ({state.chop_score*100:.0f}%) — play the alternation")

        # 3) "Due" — gambler's-fallacy flavour, by request.
        gap_t = state.hands_since.get("T", 0)
        if gap_t >= c.tie_due_after:
            votes["tie"] += min(2.5, gap_t / c.tie_due_after)
            reasons.append(f"{gap_t} hands since a Tie — Tie is 'due'")
        gap_pair = state.hands_since.get("pair")
        if gap_pair is not None and gap_pair >= c.pair_due_after:
            votes["either_pair"] += min(1.5, gap_pair / c.pair_due_after)
            reasons.append(f"{gap_pair} hands since a pair — Either Pair 'due'")
        for tok, bet in (("P", "player"), ("B", "banker")):
            gap = state.hands_since.get(tok, 0)
            if gap >= 5:
                votes[bet] += gap * 0.15
                reasons.append(f"{gap} hands since {('Player' if tok=='P' else 'Banker')} — overdue")

        # 4) Personality weighting.
        if state.personality == "Dragon" and side:
            votes[_SIDE_TO_BET[side]] += 1.0
            reasons.append("Dragon shoe — trends are holding")
        elif state.personality == "Choppy" and last:
            votes[_SIDE_TO_BET[_OPP[last]]] += 1.0

        # 5) Library empirics (if a shoe library is wired in).
        if library is not None and side:
            cont = getattr(library, "streak_continuation", lambda _l: None)(n)
            if cont is not None:
                if cont >= 0.5:
                    votes[_SIDE_TO_BET[side]] += (cont - 0.5) * 4
                    reasons.append(f"Library: {n}-streaks continued {cont*100:.0f}% of the time")
                else:
                    votes[_SIDE_TO_BET[_OPP[side]]] += (0.5 - cont) * 4
                    reasons.append(f"Library: {n}-streaks broke {(1-cont)*100:.0f}% of the time")

        if not votes:
            votes["banker"] += 0.5
            reasons.append("No strong signal — lean Banker (structural edge)")

        total = sum(votes.values())
        pick = max(votes.items(), key=lambda kv: kv[1])[0]
        vibe = votes[pick] / total if total else 0.0
        return MysticAdvice(
            pick=pick,
            pick_label=_BET_LABELS.get(pick, pick),
            vibe=min(1.0, vibe),
            personality=state.personality,
            reasons=reasons[:4],
            votes=dict(votes),
        )
