"""Application controller — the UI-agnostic core of the dashboard.

Holds all engine + betting state and exposes a small API the UI (or a test, or
later the vision loop) drives. Crucially this means the math works end-to-end
via **manual hand entry** with no computer vision present (§10 step 3): feed it
hand outcomes and it updates the shoe, roads, predictions and bet-spread matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .betting.bet_spread_calc import (
    BetSpreadCalculator,
    SpreadResult,
    distribution_from_analysis,
)
from .betting.house_edges import compute_house_edges
from .engine.learning import OnlineLearner, Scoreboard, BET_LABELS as _LEARN_LABELS, hit as _hit, profit as _profit
from .engine.patterns import MysticAdvice, PatternState, analyze_patterns
from .engine.staking import StakeSuggestion, suggest_stake

# The gameline is the near-even-money main bet; Tie is a high-edge long shot,
# so it lives with the side bets.
_GAMELINE = ("player", "banker")
_SIDE_BETS = ("tie", "super_6", "player_pair", "banker_pair", "either_pair",
              "suited_pair", "p_bonus", "b_bonus")
_BET_LABELS_ALL = {
    "player": "Player", "banker": "Banker", "tie": "Tie", "super_6": "Super 6",
    "player_pair": "P Pair", "banker_pair": "B Pair", "either_pair": "Either Pair",
    "suited_pair": "Suited Pair", "p_bonus": "P Bonus", "b_bonus": "B Bonus",
}
from .engine.dynamic_spread import DynamicSpread, compute_dynamic_spread
from .engine.predictor import Prediction, Predictor
from .engine.probability import analyze_shoe
from .engine.road_tracker import BigRoad
from .engine.shoe_state import ShoeState
from .settings import AppConfig, load_config


@dataclass
class HandInput:
    """One manually-entered (or later CV-detected) hand result."""

    winner: str  # "P", "B", "T"
    player_total: int
    banker_total: int
    is_natural: bool = False
    p_pair: bool = False
    b_pair: bool = False
    p_suited_pair: bool = False
    b_suited_pair: bool = False
    # If the actual card values are known, exact composition tracking is used;
    # otherwise an estimated 4-6 cards are removed from a uniform prior.
    card_values: Optional[List[int]] = None


@dataclass
class DashboardState:
    """Everything the dashboard needs to render one frame."""

    prediction: Prediction
    spread: SpreadResult
    house_edges: Dict[str, float]
    shoe_counts: List[int]
    shoe_initial: List[int]
    total_remaining: int
    cards_dealt: int
    penetration: float
    hands_played: int
    composition_confidence: str
    road_grid: List[List[Optional[str]]]
    needs_reshuffle: bool
    mystic: Optional[MysticAdvice] = None
    pattern: Optional[PatternState] = None
    library_stats: Dict[str, int] = field(default_factory=dict)
    learning: Optional[Scoreboard] = None
    staking: Optional[StakeSuggestion] = None
    dynamic_spread: Optional[DynamicSpread] = None
    bankroll: Dict[str, object] = field(default_factory=dict)
    vision: List[dict] = field(default_factory=list)  # side-bet history (full vision)


class AppController:
    def __init__(
        self,
        config: Optional[AppConfig] = None,
        db: Optional[object] = None,
        library: Optional[object] = None,
    ) -> None:
        self.config = config or load_config()
        self.shoe = ShoeState(
            decks=self.config.decks_per_shoe,
            penetration_pct=self.config.penetration_pct,
        )
        self.predictor = Predictor(self.config.prediction_config())
        self.road = BigRoad()
        self.payout_table = self.config.payout_table()
        self.calc = BetSpreadCalculator(self.payout_table)
        self.bets: Dict[str, float] = {}
        # House edges are ~constant over the shoe; compute once for display.
        self._house_edges = compute_house_edges(
            self.payout_table, decks=self.config.decks_per_shoe
        )
        self._initial_counts = list(self.shoe.remaining)
        # Optional persistence (step 8). A shoe row is created lazily.
        self.db = db
        self._shoe_id: Optional[int] = None
        # Pattern model + self-learning ensemble + growing shoe library.
        self.library = library
        self.learner = OnlineLearner()
        if self.library is not None:
            try:
                saved = self.library.load_learner()
                if saved:
                    self.learner = OnlineLearner.from_dict(saved)
            except Exception:
                pass
        self.sequence: List[str] = []      # P/B/T outcomes this shoe
        self._shoe_hands: List[Dict] = []  # full per-hand records for the library
        self._hands_since_pair = 99
        self._pending: Optional[Dict[str, Optional[str]]] = None  # bets for next hand
        self._pending_regime: Optional[str] = None  # regime the pending bet was made under
        # Bankroll / staking state (balance + currency + denoms come from the DOM).
        self.bankroll = {
            "currency": "", "balance": 0.0, "denoms": [], "min_bet": 0.0,
            "max_bet": 0.0, "strategy": "confidence", "consec_losses": 0,
            "shoe_start_balance": None, "suggested_pnl": 0.0,
        }
        self._last_stake: Optional[StakeSuggestion] = None  # to grade for PnL

    def set_context(self, *, currency=None, balance=None, denoms=None,
                    min_bet=None, max_bet=None, strategy=None,
                    responsiveness=None) -> None:
        """Update live betting context read from the page (balance, chips, …)."""
        bk = self.bankroll
        if responsiveness is not None:
            # How fast the ensemble re-weights toward a new regime (Fixed-Share).
            self.learner.set_share(float(responsiveness))
        if currency is not None:
            bk["currency"] = currency
        if balance is not None:
            bk["balance"] = float(balance)
            if bk["shoe_start_balance"] is None:
                bk["shoe_start_balance"] = float(balance)
        if denoms is not None:
            bk["denoms"] = [float(d) for d in denoms if d]
        if min_bet is not None:
            bk["min_bet"] = float(min_bet)
        if max_bet is not None:
            bk["max_bet"] = float(max_bet)
        if strategy is not None:
            bk["strategy"] = strategy

    # -- hand entry -------------------------------------------------------- #
    def enter_hand(self, hand: HandInput) -> None:
        if hand.card_values:
            self.shoe.record_hand_exact(list(hand.card_values))
        else:
            self.shoe.record_hand_estimated()
        # Grade the prediction made for THIS hand, then learn from the result.
        if self._pending is not None:
            pick, _ = self.learner.pick(self._pending, regime=self._pending_regime)
            won = _hit(pick, hand.winner, hand.p_pair, hand.b_pair)
            units = _profit(pick, hand.winner, hand.banker_total, hand.p_pair, hand.b_pair)
            self.learner.update(
                self._pending, hand.winner, hand.banker_total, hand.p_pair, hand.b_pair,
                p_suited=hand.p_suited_pair, b_suited=hand.b_suited_pair,
                is_natural=hand.is_natural,
                margin=abs(hand.player_total - hand.banker_total),
                exact=bool(hand.card_values), regime=self._pending_regime,
            )
            if self.library is not None:
                try:
                    self.library.save_learner(self.learner.to_dict())
                    self.library.record_prediction(pick, hand.winner, won, units)
                except Exception:
                    pass

        # Grade the staking suggestion (running suggested-bankroll + Martingale).
        if self._last_stake is not None:
            g = _profit(self._last_stake.main_bet, hand.winner, hand.banker_total,
                        hand.p_pair, hand.b_pair) * self._last_stake.stake
            self.bankroll["suggested_pnl"] += g
            self.bankroll["consec_losses"] = (
                self.bankroll["consec_losses"] + 1 if g < 0 else 0
            )

        self.road.record(hand.winner, p_pair=hand.p_pair, b_pair=hand.b_pair)
        self.sequence.append(hand.winner)
        # Full per-hand record so every side bet is recoverable per shoe.
        self._shoe_hands.append({
            "winner": hand.winner, "player_total": hand.player_total,
            "banker_total": hand.banker_total, "p_pair": hand.p_pair,
            "b_pair": hand.b_pair, "p_suited": hand.p_suited_pair,
            "b_suited": hand.b_suited_pair, "is_natural": hand.is_natural,
            "margin": abs(hand.player_total - hand.banker_total),
            "cards": list(hand.card_values or []),
        })
        self._hands_since_pair = 0 if (hand.p_pair or hand.b_pair) else self._hands_since_pair + 1
        self._log_hand(hand)

        # Form the prediction for the NEXT hand from the updated weights, and
        # remember the regime it was made under (to grade the right vector).
        state = analyze_patterns(self.sequence, hands_since_pair=self._hands_since_pair)
        self._pending = self.learner.expert_bets(state, self.library)
        self._pending_regime = state.personality

    def _log_hand(self, hand: HandInput) -> None:
        if self.db is None:
            return
        if self._shoe_id is None:
            self._shoe_id = self.db.start_shoe(
                decks=self.config.decks_per_shoe,
                penetration_pct=self.config.penetration_pct,
            )
        self.db.log_hand(self._shoe_id, hand, prediction=self.predictor.predict(self.shoe))

    # -- bets -------------------------------------------------------------- #
    def set_bet(self, name: str, amount: float) -> None:
        if amount:
            self.bets[name] = amount
        else:
            self.bets.pop(name, None)

    def clear_bets(self) -> None:
        self.bets.clear()

    # -- shoe control ------------------------------------------------------ #
    def reshuffle(self) -> None:
        # Archive the completed shoe (with full per-hand detail) before clearing.
        if self.library is not None and len(self.sequence) >= 5:
            try:
                self.library.archive_shoe(self._shoe_hands, self.sequence)
            except Exception:
                pass
        # A new shoe is a fresh regime: relax the expert weights toward uniform
        # so this shoe re-finds its pattern instead of inheriting a frozen
        # leader (the long-run learned stats are kept).
        self.learner.soften_weights(0.5)
        self.sequence = []
        self._shoe_hands = []
        self._hands_since_pair = 99
        self._pending = None
        self._pending_regime = None
        self._last_stake = None
        self.bankroll["consec_losses"] = 0
        self.bankroll["shoe_start_balance"] = self.bankroll["balance"] or None
        self.shoe.reset()
        self.predictor.reset()
        self.road.reset()
        self._shoe_id = None  # next hand opens a fresh shoe row in the DB

    def start_new_shoe(self, burn_cards: int = 10) -> None:
        """Begin a fresh shoe and remove the dealer's opening burn (§ SpinQuest=10)."""
        self.reshuffle()
        self.shoe.burn(burn_cards)

    def catch_up(self, hands: int, burn_cards: int = 10) -> None:
        """Sync to a shoe already ``hands`` deep (when going live mid-shoe).

        Resets to a fresh shoe, removes the opening burn, then removes an
        estimated number of cards for the hands already played. Winners are
        unknown, so the road is left empty for the pre-join hands.
        """
        self.start_new_shoe(burn_cards)
        for _ in range(max(0, hands)):
            self.shoe.record_hand_estimated()

    # -- rendering --------------------------------------------------------- #
    def snapshot(self) -> DashboardState:
        prediction = self.predictor.predict(self.shoe)
        # A nearly-spent shoe can't be enumerated; skip the bet-spread matrix.
        if self.shoe.total_remaining < 20:
            spread = self.calc.evaluate(self.bets, [])
        else:
            analysis = analyze_shoe(self.shoe.remaining)
            dist = distribution_from_analysis(analysis, decks=self.config.decks_per_shoe)
            spread = self.calc.evaluate(self.bets, dist)

        pattern = analyze_patterns(self.sequence, hands_since_pair=self._hands_since_pair)
        bets = self._pending if self._pending is not None else self.learner.expert_bets(pattern, self.library)
        _, votes = self.learner.pick(bets, regime=pattern.personality)
        learning = self.learner.scoreboard()

        # Always recommend a gameline bet (Player/Banker/Tie) — the best of those.
        gl_votes = {b: votes.get(b, 0.0) for b in _GAMELINE}
        main_bet = max(gl_votes, key=gl_votes.get) if any(gl_votes.values()) else "banker"
        gl_total = sum(gl_votes.values()) or 1.0
        main_vibe = gl_votes.get(main_bet, 0.0) / gl_total
        main_confident = learning.actionable and main_vibe >= 0.4
        mystic = MysticAdvice(
            pick=main_bet,
            pick_label=_BET_LABELS_ALL.get(main_bet, main_bet),
            vibe=main_vibe,
            personality=pattern.personality,
            reasons=self.learner.reasons_for(main_bet, bets, regime=pattern.personality),
            votes=votes,
            confident=main_confident,
            verdict=learning.verdict,
        )

        # Side bets that are "due" (a pattern expert wants one) or show a learned edge.
        side_suggestions, seen = [], set()
        for b in bets.values():
            if b in _SIDE_BETS and b not in seen:
                seen.add(b)
                side_suggestions.append((b, _BET_LABELS_ALL.get(b, b), "pattern / due"))
        for r in learning.bets:
            if r["bet"] in _SIDE_BETS and r["significant"] and r["bet"] not in seen:
                seen.add(r["bet"])
                side_suggestions.append((r["bet"], _BET_LABELS_ALL.get(r["bet"], r["bet"]), "edge detected"))

        bk = self.bankroll
        staking = suggest_stake(
            main_bet=main_bet, main_label=_BET_LABELS_ALL.get(main_bet, main_bet),
            confident=main_confident, vibe=main_vibe, balance=bk["balance"],
            denoms=bk["denoms"], min_bet=bk["min_bet"], max_bet=bk["max_bet"],
            strategy=bk["strategy"], consec_losses=bk["consec_losses"],
            currency=bk["currency"], side_suggestions=side_suggestions,
        )
        self._last_stake = staking

        # Dynamic probability-driven spread (new engine).
        dyn_spread: Optional[DynamicSpread] = None
        if self.shoe.total_remaining >= 20:
            try:
                dyn_spread = compute_dynamic_spread(
                    analysis=analysis,
                    prediction=prediction,
                    learning=learning,
                    mystic=mystic,
                    penetration=self.shoe.penetration,
                    balance=bk["balance"],
                    denoms=bk["denoms"],
                    min_bet=bk["min_bet"],
                    max_bet=bk["max_bet"],
                    currency=bk["currency"],
                )
            except Exception:
                pass

        bankroll_summary = {
            "currency": bk["currency"], "balance": bk["balance"],
            "shoe_start": bk["shoe_start_balance"], "suggested_pnl": bk["suggested_pnl"],
            "strategy": bk["strategy"], "consec_losses": bk["consec_losses"],
        }
        library_stats, vision = {}, []
        if self.library is not None:
            try:
                library_stats = self.library.stats()
                vision = self.library.vision_stats()
            except Exception:
                library_stats, vision = {}, []

        return DashboardState(
            prediction=prediction,
            spread=spread,
            house_edges=self._house_edges,
            shoe_counts=list(self.shoe.remaining),
            shoe_initial=list(self._initial_counts),
            total_remaining=self.shoe.total_remaining,
            cards_dealt=self.shoe.cards_dealt,
            penetration=self.shoe.penetration,
            hands_played=self.shoe.hands_played,
            composition_confidence=self.shoe.composition_confidence,
            road_grid=self.road.as_grid(),
            needs_reshuffle=self.shoe.needs_reshuffle,
            mystic=mystic,
            pattern=pattern,
            library_stats=library_stats,
            learning=learning,
            staking=staking,
            dynamic_spread=dyn_spread,
            bankroll=bankroll_summary,
            vision=vision,
        )
