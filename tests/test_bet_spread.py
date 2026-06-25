"""Bet-spread calculator tests (§5.3 / §9).

All three worked examples from the spec are encoded here, plus the Banker
carve-out, the full P-Bonus ladder, and the three suited-pair states. Net
payouts are checked to four decimal places.
"""

import pytest

from baccarat_vision.betting.bet_spread_calc import BetSpreadCalculator, HandOutcome
from baccarat_vision.betting.payout_table import PayoutTable

TABLE = PayoutTable()
CALC = BetSpreadCalculator(TABLE)


def net(spread, outcome):
    return CALC.net(spread, outcome)


# Reusable outcomes -------------------------------------------------------- #
PLAYER_WIN = HandOutcome("P", player_total=7, banker_total=5, is_natural=False)
BANKER_WIN = HandOutcome("B", player_total=2, banker_total=7, is_natural=False)
BANKER_WIN_6 = HandOutcome("B", player_total=4, banker_total=6, is_natural=False)
TIE = HandOutcome("T", player_total=5, banker_total=5, is_natural=False)


# Example A — {player: $5, tie: $1} --------------------------------------- #
def test_example_a_player_win():
    assert net({"player": 5, "tie": 1}, PLAYER_WIN) == pytest.approx(4.0, abs=1e-4)


def test_example_a_banker_win():
    assert net({"player": 5, "tie": 1}, BANKER_WIN) == pytest.approx(-6.0, abs=1e-4)


def test_example_a_banker_win_six_still_minus_six():
    assert net({"player": 5, "tie": 1}, BANKER_WIN_6) == pytest.approx(-6.0, abs=1e-4)


def test_example_a_tie():
    assert net({"player": 5, "tie": 1}, TIE) == pytest.approx(8.0, abs=1e-4)


# Example B — {banker: $10, super_6: $1} ---------------------------------- #
def test_example_b_banker_not_six():
    assert net({"banker": 10, "super_6": 1}, BANKER_WIN) == pytest.approx(9.0, abs=1e-4)


def test_example_b_banker_six():
    assert net({"banker": 10, "super_6": 1}, BANKER_WIN_6) == pytest.approx(20.0, abs=1e-4)


def test_example_b_player_win():
    assert net({"banker": 10, "super_6": 1}, PLAYER_WIN) == pytest.approx(-11.0, abs=1e-4)


def test_example_b_tie():
    assert net({"banker": 10, "super_6": 1}, TIE) == pytest.approx(-1.0, abs=1e-4)


# Example C — {p_bonus: $2}, player wins 9 vs 0 non-natural --------------- #
def test_example_c_bonus_win_by_nine():
    o = HandOutcome("P", player_total=9, banker_total=0, is_natural=False)
    assert net({"p_bonus": 2}, o) == pytest.approx(60.0, abs=1e-4)


# Banker carve-out (§9) --------------------------------------------------- #
def test_banker_six_pays_half():
    assert net({"banker": 10}, BANKER_WIN_6) == pytest.approx(5.0, abs=1e-4)


def test_banker_not_six_pays_full():
    assert net({"banker": 10}, BANKER_WIN) == pytest.approx(10.0, abs=1e-4)


# Full P-Bonus ladder (§9) ------------------------------------------------ #
@pytest.mark.parametrize(
    "p_total,b_total,expected",
    [
        (9, 0, 30.0),  # win by 9
        (8, 0, 10.0),  # win by 8 (non-natural)
        (7, 0, 6.0),   # win by 7
        (6, 0, 4.0),   # win by 6
        (5, 0, 2.0),   # win by 5
        (4, 0, 1.0),   # win by 4
        (3, 0, -1.0),  # win by 3 -> lose
        (2, 0, -1.0),  # win by 2 -> lose
        (1, 0, -1.0),  # win by 1 -> lose
    ],
)
def test_p_bonus_margin_ladder(p_total, b_total, expected):
    o = HandOutcome("P", player_total=p_total, banker_total=b_total, is_natural=False)
    assert net({"p_bonus": 1}, o) == pytest.approx(expected, abs=1e-4)


def test_p_bonus_natural_win():
    o = HandOutcome("P", player_total=9, banker_total=7, is_natural=True)
    assert net({"p_bonus": 1}, o) == pytest.approx(1.0, abs=1e-4)


def test_p_bonus_natural_tie_pushes():
    o = HandOutcome("T", player_total=8, banker_total=8, is_natural=True)
    assert net({"p_bonus": 1}, o) == pytest.approx(0.0, abs=1e-4)


def test_p_bonus_regular_tie_loses():
    o = HandOutcome("T", player_total=5, banker_total=5, is_natural=False)
    assert net({"p_bonus": 1}, o) == pytest.approx(-1.0, abs=1e-4)


def test_p_bonus_banker_win_loses():
    assert net({"p_bonus": 1}, BANKER_WIN) == pytest.approx(-1.0, abs=1e-4)


# Suited pair — three states (§9) ----------------------------------------- #
def test_suited_pair_neither():
    o = HandOutcome("B", player_total=6, banker_total=7, is_natural=False)
    assert net({"suited_pair": 1}, o) == pytest.approx(-1.0, abs=1e-4)


def test_suited_pair_one_hand():
    o = HandOutcome(
        "B", player_total=6, banker_total=7, is_natural=False,
        p_pair=True, p_suited_pair=True,
    )
    assert net({"suited_pair": 1}, o) == pytest.approx(25.0, abs=1e-4)


def test_suited_pair_both_hands():
    o = HandOutcome(
        "B", player_total=6, banker_total=7, is_natural=False,
        p_pair=True, p_suited_pair=True, b_pair=True, b_suited_pair=True,
    )
    assert net({"suited_pair": 1}, o) == pytest.approx(200.0, abs=1e-4)


# Calculator summary integration ------------------------------------------ #
def test_evaluate_outcome_matrix_summary():
    from baccarat_vision.betting.bet_spread_calc import distribution_from_analysis
    from baccarat_vision.engine.probability import analyze_shoe, full_shoe_value_counts

    analysis = analyze_shoe(full_shoe_value_counts(8))
    dist = distribution_from_analysis(analysis, decks=8)
    result = CALC.evaluate({"player": 5, "tie": 1}, dist)

    # Probabilities of all grouped rows sum to 1.
    assert sum(r.probability for r in result.rows) == pytest.approx(1.0, abs=1e-9)
    assert result.total_at_risk == pytest.approx(6.0)
    # Best case is the tie (+$8), worst is a banker win (-$6).
    assert result.best_case == pytest.approx(8.0, abs=1e-4)
    assert result.worst_case == pytest.approx(-6.0, abs=1e-4)
    # House edges make this a negative-EV spread.
    assert result.expected_value < 0
