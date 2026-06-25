"""SQLite persistence + replay (§10 step 8).

Logs every completed hand (with the probability snapshot at the time) so shoes
can be reviewed or **replayed** deterministically through a fresh controller.
Uses SQLAlchemy 2.0 ORM over SQLite; an in-memory database is used in tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

from ..controller import HandInput


class Base(DeclarativeBase):
    pass


class Shoe(Base):
    __tablename__ = "shoes"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
    decks: Mapped[int] = mapped_column(default=8)
    penetration_pct: Mapped[float] = mapped_column(default=75.0)
    note: Mapped[str] = mapped_column(String(256), default="")

    hands: Mapped[List["Hand"]] = relationship(
        back_populates="shoe", cascade="all, delete-orphan", order_by="Hand.seq"
    )


class Hand(Base):
    __tablename__ = "hands"

    id: Mapped[int] = mapped_column(primary_key=True)
    shoe_id: Mapped[int] = mapped_column(ForeignKey("shoes.id"))
    seq: Mapped[int] = mapped_column()  # 1-based order within the shoe
    winner: Mapped[str] = mapped_column(String(1))
    player_total: Mapped[int] = mapped_column()
    banker_total: Mapped[int] = mapped_column()
    is_natural: Mapped[bool] = mapped_column(default=False)
    p_pair: Mapped[bool] = mapped_column(default=False)
    b_pair: Mapped[bool] = mapped_column(default=False)
    card_values: Mapped[str] = mapped_column(String(64), default="")  # "3,4,2"
    # Probability snapshot at the moment the hand was logged (nullable).
    p_player: Mapped[Optional[float]] = mapped_column(default=None)
    p_banker: Mapped[Optional[float]] = mapped_column(default=None)
    p_tie: Mapped[Optional[float]] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    shoe: Mapped["Shoe"] = relationship(back_populates="hands")

    def to_hand_input(self) -> HandInput:
        cards = (
            [int(x) for x in self.card_values.split(",") if x]
            if self.card_values
            else None
        )
        return HandInput(
            winner=self.winner,
            player_total=self.player_total,
            banker_total=self.banker_total,
            is_natural=self.is_natural,
            p_pair=self.p_pair,
            b_pair=self.b_pair,
            card_values=cards,
        )


class Database:
    """Thin façade over a SQLAlchemy engine for the app's logging needs."""

    def __init__(self, url: str = "sqlite:///baccarat_vision.db") -> None:
        self.engine = create_engine(url)
        Base.metadata.create_all(self.engine)

    def start_shoe(self, decks: int = 8, penetration_pct: float = 75.0) -> int:
        with Session(self.engine) as s:
            shoe = Shoe(decks=decks, penetration_pct=penetration_pct)
            s.add(shoe)
            s.commit()
            return shoe.id

    def log_hand(
        self,
        shoe_id: int,
        hand: HandInput,
        prediction: Optional[object] = None,
    ) -> int:
        with Session(self.engine) as s:
            seq = (
                s.query(Hand).filter_by(shoe_id=shoe_id).count() + 1
            )
            record = Hand(
                shoe_id=shoe_id,
                seq=seq,
                winner=hand.winner,
                player_total=hand.player_total,
                banker_total=hand.banker_total,
                is_natural=hand.is_natural,
                p_pair=hand.p_pair,
                b_pair=hand.b_pair,
                card_values=",".join(str(v) for v in (hand.card_values or [])),
                p_player=getattr(prediction, "p_player", None),
                p_banker=getattr(prediction, "p_banker", None),
                p_tie=getattr(prediction, "p_tie", None),
            )
            s.add(record)
            s.commit()
            return record.id

    def get_hands(self, shoe_id: int) -> List[Hand]:
        with Session(self.engine) as s:
            stmt = select(Hand).where(Hand.shoe_id == shoe_id).order_by(Hand.seq)
            return list(s.scalars(stmt))

    def replay(self, shoe_id: int, controller) -> None:
        """Feed every logged hand of ``shoe_id`` through ``controller`` in order."""
        controller.reshuffle()
        for hand in self.get_hands(shoe_id):
            controller.enter_hand(hand.to_hand_input())
