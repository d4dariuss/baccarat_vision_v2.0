"""Big Road bookkeeping -- *informational only* (§0, §11).

The roads are a visualization tool, not a predictive model. This tracker
reconstructs the Big Road column/row layout from the sequence of hand winners
so the UI can mirror what's on screen. It deliberately exposes **no** predictive
output; ``road_weight`` stays 0 in the default config and nothing here feeds the
:class:`~baccarat_vision.engine.predictor.Predictor`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RoadEntry:
    """One Big Road cell: the winning side plus tie/pair annotations."""

    side: str  # "P" or "B" (ties annotate the prior entry)
    ties: int = 0  # ties stacked on this entry before the next P/B
    p_pair: bool = False
    b_pair: bool = False


@dataclass
class BigRoad:
    """Column-major Big Road. Each column is a streak of one side."""

    columns: List[List[RoadEntry]] = field(default_factory=list)

    def record(
        self,
        winner: str,
        p_pair: bool = False,
        b_pair: bool = False,
    ) -> None:
        if winner == "T":
            # Ties annotate the most recent non-tie entry.
            last = self._last_entry()
            if last is not None:
                last.ties += 1
            return
        entry = RoadEntry(side=winner, p_pair=p_pair, b_pair=b_pair)
        if self.columns and self.columns[-1][-1].side == winner:
            self.columns[-1].append(entry)  # extend the streak
        else:
            self.columns.append([entry])  # new column on a change

    def _last_entry(self) -> Optional[RoadEntry]:
        if self.columns and self.columns[-1]:
            return self.columns[-1][-1]
        return None

    @property
    def hand_count(self) -> int:
        return sum(e.ties + 1 for col in self.columns for e in col)

    def as_grid(self, max_rows: int = 6) -> List[List[Optional[str]]]:
        """Render to a column-major grid of 'P'/'B'/None for display."""
        grid: List[List[Optional[str]]] = []
        for col in self.columns:
            cells: List[Optional[str]] = [e.side for e in col][:max_rows]
            while len(cells) < max_rows:
                cells.append(None)
            grid.append(cells)
        return grid

    def reset(self) -> None:
        self.columns = []
