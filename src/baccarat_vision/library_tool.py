"""Audit, prune, and inspect the shoe library.

    python -m baccarat_vision.library_tool audit            # report what's stored
    python -m baccarat_vision.library_tool prune [min_hands] # drop stub shoes (<8)
    python -m baccarat_vision.library_tool vision            # side-bet history

Run from the directory holding ``baccarat_library.sqlite`` (the project root,
where you start the server).
"""

from __future__ import annotations

import sys

from .persistence.library import ShoeLibrary


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "audit"
    lib = ShoeLibrary()
    a = lib.audit()
    print(
        f"Library: {a['shoes']} shoes  ·  {a['rich_shoes']} with full card data, "
        f"{a['winner_only_shoes']} winner-only  ·  {a['rich_hands']} rich hands  ·  "
        f"{a['stub_shoes']} stub shoes (<8 hands)."
    )
    if a["winner_only_shoes"]:
        print(
            "  Note: winner-only shoes predate full-card logging — usable for the "
            "pattern/markov model, but their side bets can't be reconstructed."
        )

    if cmd == "prune":
        n = int(argv[1]) if len(argv) > 1 else 8
        removed = lib.prune_stubs(n)
        print(f"Pruned {removed} stub shoe(s) with <{n} hands. {lib.audit()['shoes']} kept.")
    elif cmd == "vision":
        print("Side-bet history (from stored card data):")
        for r in lib.vision_stats():
            print(f"  {r['bet']:12} n={r['n']:5d}  hit={r['hit_rate'] * 100:5.1f}%  "
                  f"per100={r['per100']:+8.1f}u")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
