"""Pattern / mystic model + shoe library tests."""

from baccarat_vision.engine.patterns import MysticAdvisor, analyze_patterns
from baccarat_vision.persistence.library import ShoeLibrary


def test_streak_detection():
    st = analyze_patterns(list("BBBBB"))
    assert st.streak_side == "B" and st.streak_len == 5 and st.is_dragon


def test_chop_detection():
    st = analyze_patterns(list("PBPBPBPB"))
    assert st.chop_score == 1.0
    assert st.personality == "Choppy"


def test_due_counters():
    st = analyze_patterns(list("BBPBPPBBPBPB"))  # no Tie at all
    assert st.hands_since["T"] == len("BBPBPPBBPBPB")
    # last hand is B -> 0 since banker, >0 since player
    assert st.hands_since["B"] == 0 and st.hands_since["P"] >= 1


def test_advisor_rides_short_dragon():
    a = MysticAdvisor().advise(analyze_patterns(list("BBBB")))
    assert a.pick == "banker"
    assert any("dragon" in r.lower() for r in a.reasons)


def test_advisor_fades_long_streak():
    a = MysticAdvisor().advise(analyze_patterns(list("BBBBBBBBB")))
    assert a.pick == "player"  # due to break -> fade


def test_advisor_plays_the_chop():
    a = MysticAdvisor().advise(analyze_patterns(list("PBPBPBPB")))
    # last is B, choppy -> bet Player (the alternation)
    assert a.pick == "player"


def test_advisor_varies_across_shoes():
    adv = MysticAdvisor()
    picks = {
        adv.advise(analyze_patterns(list("BBBB"))).pick,
        adv.advise(analyze_patterns(list("PBPBPBPB"))).pick,
        adv.advise(analyze_patterns(list("BBBBBBBBB"))).pick,
    }
    assert len(picks) >= 2  # not always the same answer


def test_library_archive_and_continuation(tmp_path):
    lib = ShoeLibrary(str(tmp_path / "lib.sqlite"))
    # Many shoes with strong 2-streaks that mostly continue.
    for _ in range(20):
        lib.archive(list("BBBPPPBBBPPP"))
    assert lib.stats()["shoes"] == 20
    cont1 = lib.streak_continuation(1)  # P(a run reaching 1 continues to 2)
    assert cont1 is not None and 0.0 <= cont1 <= 1.0


def test_library_ignores_stubs(tmp_path):
    lib = ShoeLibrary(str(tmp_path / "lib.sqlite"))
    assert lib.archive(list("BP")) == 0  # too short
    assert lib.stats()["shoes"] == 0
