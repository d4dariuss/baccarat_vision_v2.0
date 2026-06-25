"""Probability-engine tests (§4 / §9).

Pins the exact 8-deck baseline P(B)/P(P)/P(T) to four decimals and checks the
runtime-computed house edges. NOTE: the spec's §4.2 table lists Super 6 at
13.66% and Either Pair at 14.20%; those are transcription errors. The correct,
runtime-computed figures (matching Wizard of Odds) are ~13.82% and ~13.71%, and
we assert the truth here rather than the spec's typos.
"""

import pytest

from baccarat_vision.betting.house_edges import compute_house_edges
from baccarat_vision.betting.payout_table import PayoutTable
from baccarat_vision.engine.probability import (
    BASELINE_P_BANKER,
    BASELINE_P_PLAYER,
    BASELINE_P_TIE,
    analyze_shoe,
    approx_probabilities,
    full_shoe_value_counts,
)
from baccarat_vision.engine.shoe_state import ShoeState

FULL = full_shoe_value_counts(8)
ANALYSIS = analyze_shoe(FULL)
EDGES = compute_house_edges(PayoutTable(), decks=8)


# Baseline probabilities (§4.1) ------------------------------------------- #
def test_baseline_banker():
    assert ANALYSIS.p_banker == pytest.approx(BASELINE_P_BANKER, abs=5e-5)
    assert round(ANALYSIS.p_banker, 4) == 0.4586


def test_baseline_player():
    assert ANALYSIS.p_player == pytest.approx(BASELINE_P_PLAYER, abs=5e-5)
    assert round(ANALYSIS.p_player, 4) == 0.4462


def test_baseline_tie():
    assert ANALYSIS.p_tie == pytest.approx(BASELINE_P_TIE, abs=5e-5)
    assert round(ANALYSIS.p_tie, 4) == 0.0952


def test_probabilities_sum_to_one():
    assert ANALYSIS.p_banker + ANALYSIS.p_player + ANALYSIS.p_tie == pytest.approx(1.0)


def test_distribution_sums_to_one():
    assert sum(ANALYSIS.distribution.values()) == pytest.approx(1.0)


# House edges that the spec states correctly — pinned to within 0.01% (§9) - #
def test_edge_banker():
    assert EDGES["banker"] == pytest.approx(0.0146, abs=1e-4)


def test_edge_player():
    assert EDGES["player"] == pytest.approx(0.0124, abs=1e-4)


def test_edge_tie():
    assert EDGES["tie"] == pytest.approx(0.1436, abs=1e-4)


def test_edge_pairs():
    assert EDGES["player_pair"] == pytest.approx(0.1036, abs=1e-4)
    assert EDGES["banker_pair"] == pytest.approx(0.1036, abs=1e-4)


# House edges where the spec table is wrong — assert the computed truth ---- #
def test_edge_super_6_is_correct_not_spec_typo():
    # Spec says 13.66%; correct value for 15:1 with P(B6)=0.053864 is ~13.82%.
    assert EDGES["super_6"] == pytest.approx(0.1382, abs=1e-3)


def test_edge_either_pair_is_correct_not_spec_typo():
    # Spec says 14.20%; correct value for 5:1 with P(either)=0.14382 is ~13.71%.
    assert EDGES["either_pair"] == pytest.approx(0.1371, abs=1e-3)


def test_edge_bonuses_match_canonical_dragon_bonus():
    # The spec's "~9-10%" only holds for the *Banker* bonus. These bets are the
    # well-known Dragon Bonus: Player side ~2.65%, Banker side ~9.37%.
    assert EDGES["p_bonus"] == pytest.approx(0.0265, abs=1e-3)
    assert EDGES["b_bonus"] == pytest.approx(0.0937, abs=1e-3)


def test_edge_suited_pair_near_spec():
    # Spec §4.2: "Suited Pair ~8.5%". Computed value is ~8.05%.
    assert EDGES["suited_pair"] == pytest.approx(0.0805, abs=2e-3)


# Effects of removal / approximation -------------------------------------- #
def test_approx_matches_exact_on_full_shoe():
    p, b, t = approx_probabilities(FULL, decks=8)
    assert p == pytest.approx(ANALYSIS.p_player, abs=1e-9)
    assert b == pytest.approx(ANALYSIS.p_banker, abs=1e-9)
    assert t == pytest.approx(ANALYSIS.p_tie, abs=1e-9)


def test_removing_cards_shifts_probabilities():
    reduced = list(FULL)
    reduced[0] -= 20  # pull 20 tens/faces
    a = analyze_shoe(tuple(reduced))
    # Composition changed -> probabilities move off baseline.
    assert a.p_banker != pytest.approx(ANALYSIS.p_banker, abs=1e-6)
    assert a.p_banker + a.p_player + a.p_tie == pytest.approx(1.0)


# Shoe state -------------------------------------------------------------- #
def test_full_shoe_totals():
    shoe = ShoeState(decks=8)
    assert shoe.total_remaining == 416
    assert shoe.counts[0] == 128  # tens/J/Q/K
    assert shoe.counts[1] == 32   # aces


def test_record_exact_hand_decrements_and_stays_high_confidence():
    shoe = ShoeState(decks=8)
    shoe.record_hand_exact([1, 0, 9, 0])  # 4 cards
    assert shoe.total_remaining == 412
    assert shoe.hands_played == 1
    assert shoe.composition_confidence == "high"


def test_estimated_hand_drops_confidence():
    shoe = ShoeState(decks=8)
    shoe.record_hand_estimated(5)
    assert shoe.total_remaining == 411
    assert shoe.composition_confidence == "low"


def test_reset_restores_full_shoe():
    shoe = ShoeState(decks=8)
    shoe.record_hand_exact([1, 2, 3, 4, 5, 6])
    shoe.reset()
    assert shoe.total_remaining == 416
    assert shoe.composition_confidence == "high"
