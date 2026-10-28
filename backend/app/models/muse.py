from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.entities import utc_now


class MuseExchange(Base):
    """Dispatch/continuation references; message bodies remain in the 30-day cache."""

    __tablename__ = "muse_exchanges"
    __table_args__ = (UniqueConstraint("grant_id", "request_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    connection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="starting", index=True)
    sent_message_id: Mapped[str | None] = mapped_column(String(256))
    source_session_id: Mapped[int | None] = mapped_column(Integer)
    source_turn_id: Mapped[int | None] = mapped_column(Integer)
    agenda_item_id: Mapped[int | None] = mapped_column(Integer)
    wake_scheduled: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class MuseInboxState(Base):
    """Grant-bound incoming cursor and conversation owner, without message bodies."""

    __tablename__ = "muse_inbox_states"

    grant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    agenda_item_id: Mapped[int | None] = mapped_column(Integer)
