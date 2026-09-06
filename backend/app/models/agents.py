from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.entities import utc_now


class AgentPolicy(Base):
    __tablename__ = "agent_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class AgentCredential(Base):
    __tablename__ = "agent_credentials"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentProposal(Base):
    __tablename__ = "agent_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    actions: Mapped[str] = mapped_column(Text, nullable=False)
    references_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    replaces_proposal_id: Mapped[int | None] = mapped_column(Integer)
    material_change: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    execution_status: Mapped[str | None] = mapped_column(String(32))
    act_session_id: Mapped[int | None] = mapped_column(Integer)
    act_turn_id: Mapped[int | None] = mapped_column(Integer)
    nonce_hash: Mapped[str | None] = mapped_column(String(64))
    telegram_message_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    telegram_connection_id: Mapped[int | None] = mapped_column(Integer)
    telegram_outcome_fingerprint: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AssistantAssessmentState(Base):
    __tablename__ = "assistant_assessment_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    anchor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(32))
