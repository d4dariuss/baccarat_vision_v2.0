"""Online-learning ensemble + ledger tests."""

from baccarat_vision.controller import AppController, HandInput
from baccarat_vision.engine.learning import OnlineLearner, profit
from baccarat_vision.engine.patterns import analyze_patterns
from baccarat_vision.persistence.library import ShoeLibrary


def test_profit_payouts():
    assert profit("banker", "B", 7, False, False) == 1.0
    assert profit("banker", "B", 6, False, False) == 0.5   # EZ carve-out
    assert profit("banker", "T", 0, False, False) == 0.0   # push
    assert profit("player", "P", 0, False, False) == 1.0
    assert profit("tie", "T", 0, False, False) == 8.0
    assert profit("tie", "B", 0, False, False) == -1.0
    assert profit("either_pair", "B", 0, True, False) == 5.0


def test_grade_every_bet_on_the_sheet():
    from baccarat_vision.engine.learning import grade
    # Banker wins on 6 -> half on Banker, 15:1 on Super 6.
    o = {"winner": "B", "banker_total": 6, "exact": True}
    assert grade("banker", o) == 0.5
    assert grade("super_6", o) == 15.0
    # P natural win -> P Bonus pays flat 1:1 (natural overrides the ladder).
    assert grade("p_bonus", {"winner": "P", "banker_total": 0, "exact": True,
                             "is_natural": True, "margin": 2}) == 1.0
    # P win by 9 (non-natural) -> 30:1.
    assert grade("p_bonus", {"winner": "P", "banker_total": 0, "exact": True,
                             "margin": 9}) == 30.0
    # Both suited pairs -> 200:1; one -> 25:1.
    assert grade("suited_pair", {"winner": "B", "banker_total": 5, "exact": True,
                                 "p_suited": True, "b_suited": True}) == 200.0
    assert grade("suited_pair", {"winner": "B", "banker_total": 5, "exact": True,
                                 "p_suited": True, "b_suited": False}) == 25.0
    # Side bets are ungradable on a winner-only (non-exact) hand; P/B/T still grade.
    assert grade("super_6", {"winner": "B", "banker_total": 6, "exact": False}) is None
    assert grade("player", {"winner": "P", "banker_total": 0, "exact": False}) == 1.0


def test_learner_tracks_all_bets_on_exact_hand():
    from baccarat_vision.engine.learning import ALL_BETS
    lr = OnlineLearner()
    lr.update({}, "B", 6, True, False, p_suited=True, is_natural=False, margin=4, exact=True)
    tracked = {r["bet"] for r in lr.scoreboard().bets if r["n"] > 0}
    assert set(ALL_BETS) <= tracked


def test_winning_expert_gains_weight():
    lr = OnlineLearner()
    bets = {"flat-banker": "banker", "flat-player": "player"}
    for _ in range(20):  # Banker wins every time
        lr.update(bets, "B", 7, False, False)
    assert lr.weights["flat-banker"] > lr.weights["flat-player"]
    assert lr.stats["flat-banker"]["wins"] == 20


def test_ensemble_pick_follows_weights():
    lr = OnlineLearner()
    bets = {"flat-banker": "banker", "flat-player": "player"}
    for _ in range(30):
        lr.update(bets, "B", 7, False, False)
    pick, votes = lr.pick(bets)
    assert pick == "banker"


def test_scoreboard_tracks_accuracy_and_baseline():
    lr = OnlineLearner()
    bets = {"flat-banker": "banker", "flat-player": "player"}
    for _ in range(10):
        lr.update(bets, "B", 7, False, False)
    sb = lr.scoreboard()
    assert sb.graded == 10
    assert sb.baseline_accuracy == 1.0  # banker won every hand
    assert sb.best_expert == "flat-banker"


def test_learner_persistence_roundtrip():
    lr = OnlineLearner()
    bets = {"flat-banker": "banker"}
    for _ in range(5):
        lr.update(bets, "B", 7, False, False)
    restored = OnlineLearner.from_dict(lr.to_dict())
    assert restored.weights["flat-banker"] == lr.weights["flat-banker"]
    assert restored.stats["flat-banker"]["n"] == lr.stats["flat-banker"]["n"]
    assert restored.scoreboard().graded == lr.scoreboard().graded


def test_confidence_gate_discriminates_edge_from_noise():
    bets = {"flat-banker": "banker", "flat-player": "player"}
    # Below the minimum sample -> never confident.
    lr = OnlineLearner()
    for _ in range(10):
        lr.update(bets, "B", 7, False, False)
    assert lr.confident() is False

    # A real, sustained edge with enough hands -> confident + significant.
    for _ in range(60):
        lr.update(bets, "B", 7, False, False)
    assert lr.confident() is True
    sb = lr.scoreboard()
    assert sb.actionable is True and "edge" in sb.verdict.lower()

    # Pure coin-flip noise -> profit lower bound <= 0, not significant.
    import random
    random.seed(7)
    noise = OnlineLearner()
    for _ in range(200):
        w = "B" if random.random() < 0.5 else "P"
        noise.update(bets, w, 7, False, False)
    assert noise.scoreboard().significant is False


def test_fixed_share_tracks_a_regime_switch_faster():
    """After a dragon breaks into a chop, the responsive learner should follow
    the new regime at least as well as a frozen (share=0) one."""
    from baccarat_vision.engine.patterns import analyze_patterns

    def follow_chop_accuracy(share):
        lr = OnlineLearner(share=share)
        hist = []
        for w in list("BBBBBBBBBBBB"):          # dragon
            hist.append(w)
            lr.update(lr.expert_bets(analyze_patterns(hist)), w, 7, False, False)
        hits = 0
        for w in list("PBPBPBPBPB"):            # then a clean chop
            st = analyze_patterns(hist)
            pick, _ = lr.pick(lr.expert_bets(st))  # predict BEFORE seeing w
            if (pick == "player" and w == "P") or (pick == "banker" and w == "B"):
                hits += 1
            hist.append(w)
            lr.update(lr.expert_bets(st), w, 7, False, False)
        return hits

    assert follow_chop_accuracy(0.12) >= follow_chop_accuracy(0.0)


def test_regime_vectors_specialise_differently():
    """Each regime should learn its own weights: streak-following experts win
    in a dragon, alternation-following experts win in a chop."""
    from baccarat_vision.engine.patterns import analyze_patterns

    lr = OnlineLearner(share=0.08)
    hist = []
    # Alternate long dragon blocks and chop blocks several times.
    for _ in range(4):
        for w in list("BBBBBBBB"):
            hist.append(w)
            st = analyze_patterns(hist)
            lr.update(lr.expert_bets(st), w, 7, False, False, regime=st.personality)
        for w in list("PBPBPBPB"):
            hist.append(w)
            st = analyze_patterns(hist)
            lr.update(lr.expert_bets(st), w, 7, False, False, regime=st.personality)

    dragon, choppy = lr.regime_weights["Dragon"], lr.regime_weights["Choppy"]
    # The two regimes ended up with genuinely different weight vectors.
    assert dragon != choppy
    # Riding the banker streak is favoured more in a dragon than in a chop;
    # opposing the last result is favoured more in a chop than in a dragon.
    assert dragon["flat-banker"] > choppy["flat-banker"]
    assert choppy["oppose-last"] > dragon["oppose-last"]


def test_regime_weights_persist_roundtrip():
    from baccarat_vision.engine.patterns import analyze_patterns

    lr = OnlineLearner(share=0.08)
    hist = []
    for w in list("BBBBBBBBBB"):
        hist.append(w)
        st = analyze_patterns(hist)
        lr.update(lr.expert_bets(st), w, 7, False, False, regime=st.personality)
    restored = OnlineLearner.from_dict(lr.to_dict())
    assert restored.regime_weights["Dragon"] == lr.regime_weights["Dragon"]


def test_soften_weights_relaxes_toward_uniform():
    lr = OnlineLearner(share=0.0)
    bets = {"flat-banker": "banker", "flat-player": "player"}
    for _ in range(30):                          # build a strong flat-banker lead
        lr.update(bets, "B", 7, False, False)
    spread_before = lr.weights["flat-banker"] - lr.weights["flat-player"]
    lr.soften_weights(0.5)
    spread_after = lr.weights["flat-banker"] - lr.weights["flat-player"]
    assert 0 < spread_after < spread_before      # relaxed toward uniform, not erased
    assert lr.stats["flat-banker"]["wins"] == 30  # long-run stats kept


def test_controller_learns_and_persists(tmp_path):
    lib = ShoeLibrary(str(tmp_path / "lib.sqlite"))
    ctrl = AppController(library=lib)
    # Feed a streaky shoe; learner should grade each hand after the first.
    for w in list("BBBBPBBBPB"):
        ctrl.enter_hand(HandInput(w, 7, 5))
    snap = ctrl.snapshot()
    assert snap.learning is not None
    assert snap.learning.graded >= 8        # graded all but the first
    assert snap.mystic is not None and snap.mystic.pick

    # A fresh controller on the same library reloads the learner state.
    ctrl2 = AppController(library=lib)
    assert ctrl2.learner.scoreboard().graded == snap.learning.graded
