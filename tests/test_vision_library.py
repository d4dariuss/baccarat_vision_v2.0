"""Rich per-shoe storage + full-vision side-bet aggregation + audit/prune."""

from baccarat_vision.controller import AppController, HandInput
from baccarat_vision.persistence.library import ShoeLibrary


def test_archive_shoe_stores_rich_hands_and_vision(tmp_path):
    lib = ShoeLibrary(str(tmp_path / "lib.sqlite"))
    records = [
        {"winner": "B", "player_total": 4, "banker_total": 6, "p_pair": True,
         "b_pair": True, "p_suited": False, "b_suited": False, "is_natural": False,
         "margin": 2, "cards": [2, 2, 3, 3]},  # Banker on 6 -> Super 6; both pairs
        {"winner": "P", "player_total": 9, "banker_total": 0, "p_pair": False,
         "b_pair": False, "p_suited": True, "b_suited": False, "is_natural": False,
         "margin": 9, "cards": [4, 5, 0, 0]},  # P win by 9; suited P pair? (p_suited)
    ] * 6
    seq = ["B", "P"] * 6
    sid = lib.archive_shoe(records, seq)
    assert sid > 0
    vs = {r["bet"]: r for r in lib.vision_stats()}
    # Super 6 hit on every banker-6 hand we stored.
    assert vs["super_6"]["hits"] > 0
    assert vs["either_pair"]["hits"] > 0
    assert vs["p_bonus"]["hits"] > 0  # the win-by-9s
    assert vs["super_6"]["n"] == len(records)  # graded on every rich hand


def test_audit_and_prune(tmp_path):
    lib = ShoeLibrary(str(tmp_path / "lib.sqlite"))
    lib.archive(list("BPBPBPBPBPBP"))   # 12 hands (kept)
    lib.archive(list("BPBPB"))          # 5 hands (stub <8 -> pruned)
    a = lib.audit()
    assert a["shoes"] == 2 and a["stub_shoes"] == 1
    removed = lib.prune_stubs(8)
    assert removed == 1
    assert lib.audit()["shoes"] == 1


def test_controller_archives_full_shoe(tmp_path):
    lib = ShoeLibrary(str(tmp_path / "lib.sqlite"))
    ctrl = AppController(library=lib)
    for w in list("BPBPBPBPBP"):
        ctrl.enter_hand(HandInput(w, 4, 6, p_pair=(w == "B"), card_values=[2, 2, 3, 3]))
    ctrl.start_new_shoe(10)  # triggers archive of the completed shoe
    assert lib.audit()["rich_shoes"] == 1
    assert lib.audit()["rich_hands"] == 10
    # Side bets now recoverable from the stored shoe.
    assert any(r["bet"] == "super_6" and r["n"] == 10 for r in lib.vision_stats())
