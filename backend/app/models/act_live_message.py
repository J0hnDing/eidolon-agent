from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.entities import utc_now


class ActLiveMessage(Base):
    __tablename__ = "act_live_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn_id: Mapped[int] = mapped_column(ForeignKey("act_turns.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("act_live_messages.id", ondelete="CASCADE"), unique=True,
    )
    reply_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    reply_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        Index("uq_act_live_wait", "turn_id", unique=True, sqlite_where=sql_text("reply_status = 'awaiting'")),
    )
