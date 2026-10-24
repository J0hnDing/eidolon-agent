from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint
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
    action_json: Mapped[dict | None] = mapped_column(JSON)
    provenance_json: Mapped[dict | None] = mapped_column(JSON)
    follow_up_json: Mapped[dict | None] = mapped_column(JSON)
    execution_state_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    execution_status: Mapped[str | None] = mapped_column(String(32))
    act_session_id: Mapped[int | None] = mapped_column(Integer)
    act_turn_id: Mapped[int | None] = mapped_column(Integer)
    nonce_hash: Mapped[str | None] = mapped_column(String(64))
    telegram_message_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    telegram_connection_id: Mapped[int | None] = mapped_column(Integer)
    telegram_outcome_fingerprint: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AssistantFollowUp(Base):
    __tablename__ = "assistant_follow_ups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    timing_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_turn_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AssistantState(Base):
    __tablename__ = "assistant_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_time: Mapped[str] = mapped_column(String(5), default="09:00", nullable=False)
    reflection_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reflection_time: Mapped[str] = mapped_column(String(5), default="22:00", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Toronto", nullable=False)
    primary_session_id: Mapped[int | None] = mapped_column(Integer)
    thread_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_daily_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reflection_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reflection_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agenda_schedules_migrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    planning_contract_migrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    initialization_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class AssistantSourceState(Base):
    __tablename__ = "assistant_source_states"

    source: Mapped[str] = mapped_column(String(256), primary_key=True)
    scope: Mapped[str] = mapped_column(Text, default="", nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(512))


class AssistantChange(Base):
    __tablename__ = "assistant_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSON)
    after_json: Mapped[dict | None] = mapped_column(JSON)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    review_turn_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class AssistantAgendaItem(Base):
    __tablename__ = "assistant_agenda"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="task", nullable=False)
    question_category: Mapped[str] = mapped_column(String(16), default="common", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    recurrence_json: Mapped[dict | None] = mapped_column(JSON)
    completion_requirement: Mapped[str | None] = mapped_column(String(32))
    recurrence_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schedule_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_session_id: Mapped[int | None] = mapped_column(Integer)
    last_turn_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AssistantAgendaOccurrence(Base):
    __tablename__ = "assistant_agenda_occurrences"
    __table_args__ = (UniqueConstraint("agenda_item_id", "schedule_version", "scheduled_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agenda_item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    schedule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    turn_id: Mapped[int | None] = mapped_column(Integer, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
