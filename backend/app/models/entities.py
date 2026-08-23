from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class CodexRoutingSettings(Base):
    __tablename__ = "codex_routing_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class MemoryFact(Base):
    __tablename__ = "memory_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="normal", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_editable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    runtime: Mapped[str] = mapped_column(String(32), default="function", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed", nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    manifest_path: Mapped[str] = mapped_column(String(512), nullable=False)
    instructions_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    function_requirements_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    integration_requirements_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    installed_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    active_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    versions: Mapped[list["SkillVersion"]] = relationship(back_populates="skill")
    runs: Mapped[list["SkillRun"]] = relationship(back_populates="skill", foreign_keys="SkillRun.skill_id")
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(back_populates="skill")
    generation_requests: Mapped[list["SkillGenerationRequest"]] = relationship(back_populates="proposed_skill")
    schedules: Mapped[list["SkillSchedule"]] = relationship(back_populates="skill")
    web_app_instances: Mapped[list["WebAppInstance"]] = relationship(
        back_populates="skill",
        cascade="all, delete-orphan",
    )
    function_access_approvals_as_caller: Mapped[list["FunctionAccessApproval"]] = relationship(
        foreign_keys="FunctionAccessApproval.caller_skill_id",
        back_populates="caller_skill",
        cascade="all, delete-orphan",
    )
    function_access_approvals_as_target: Mapped[list["FunctionAccessApproval"]] = relationship(
        foreign_keys="FunctionAccessApproval.target_skill_id",
        back_populates="target_skill",
        cascade="all, delete-orphan",
    )
    integration_authorizations: Mapped[list["IntegrationAuthorization"]] = relationship(
        back_populates="skill",
        cascade="all, delete-orphan",
    )
    integration_audit_records: Mapped[list["IntegrationAuditRecord"]] = relationship(
        back_populates="skill",
        cascade="all, delete-orphan",
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False, index=True)
    folder_path: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    code_snapshot_path: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(32), default="system", nullable=False)
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("skill_versions.id"), nullable=True, index=True)
    permission_fingerprint: Mapped[str] = mapped_column(String(128), default="", nullable=False, index=True)
    test_status: Mapped[str] = mapped_column(String(32), default="not_run", nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), default="not_run", nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)

    skill: Mapped["Skill"] = relationship(back_populates="versions")


class SkillRun(Base):
    __tablename__ = "skill_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    codex_invocations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version_id: Mapped[int | None] = mapped_column(ForeignKey("skill_versions.id"), nullable=True, index=True)
    invocation_source: Mapped[str] = mapped_column(String(32), default="internal", nullable=False, index=True)
    caller_skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True, index=True)
    caller_version_id: Mapped[int | None] = mapped_column(ForeignKey("skill_versions.id"), nullable=True, index=True)
    source_schedule_id: Mapped[int | None] = mapped_column(ForeignKey("skill_schedules.id"), nullable=True, index=True)
    web_app_instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("web_app_instances.id"), nullable=True, index=True
    )
    initiating_action: Mapped[str | None] = mapped_column(String(128), nullable=True)
    function_capability_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    skill: Mapped["Skill"] = relationship(back_populates="runs", foreign_keys=[skill_id])
    version: Mapped["SkillVersion | None"] = relationship(foreign_keys=[version_id])
    caller_skill: Mapped["Skill | None"] = relationship(foreign_keys=[caller_skill_id])
    caller_version: Mapped["SkillVersion | None"] = relationship(foreign_keys=[caller_version_id])


class FunctionAccessApproval(Base):
    __tablename__ = "function_access_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    caller_skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    target_skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    approval_request_id: Mapped[int] = mapped_column(ForeignKey("approval_requests.id"), nullable=False, index=True)
    target_contract_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    caller_skill: Mapped["Skill"] = relationship(
        foreign_keys=[caller_skill_id],
        back_populates="function_access_approvals_as_caller",
    )
    target_skill: Mapped["Skill"] = relationship(
        foreign_keys=[target_skill_id],
        back_populates="function_access_approvals_as_target",
    )
    approval_request: Mapped["ApprovalRequest"] = relationship()


class IntegrationConnection(Base):
    __tablename__ = "integration_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    secret_store_id: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_reference: Mapped[str] = mapped_column(String(256), nullable=False)
    credential_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="token")
    passphrase_secret_store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    passphrase_secret_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_login: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    configured_resource_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    last_validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntegrationAuthorization(Base):
    __tablename__ = "integration_authorizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    contract_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    approval_request_id: Mapped[int] = mapped_column(ForeignKey("approval_requests.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    skill: Mapped["Skill"] = relationship(back_populates="integration_authorizations")
    approval_request: Mapped["ApprovalRequest"] = relationship()


class IntegrationAuditRecord(Base):
    __tablename__ = "integration_audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("skill_versions.id"), nullable=False, index=True)
    skill_run_id: Mapped[int | None] = mapped_column(ForeignKey("skill_runs.id"), nullable=True, index=True)
    web_app_instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("web_app_instances.id"), nullable=True, index=True
    )
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    skill: Mapped["Skill"] = relationship(back_populates="integration_audit_records")
    version: Mapped["SkillVersion"] = relationship()
    skill_run: Mapped["SkillRun | None"] = relationship()


class WebAppInstance(Base):
    __tablename__ = "web_app_instances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("skill_versions.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="starting", nullable=False, index=True)
    runner_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    upstream_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    relay_container_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    process_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capability_token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    skill: Mapped["Skill"] = relationship(back_populates="web_app_instances")
    version: Mapped["SkillVersion"] = relationship()
    sessions: Mapped[list["WebAppSession"]] = relationship(
        back_populates="instance",
        cascade="all, delete-orphan",
    )
    audit_records: Mapped[list["WebAppAuditRecord"]] = relationship(
        back_populates="instance",
        cascade="all, delete-orphan",
    )


class WebAppSession(Base):
    __tablename__ = "web_app_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("web_app_instances.id"), nullable=False, index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    gateway_host: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instance: Mapped["WebAppInstance"] = relationship(back_populates="sessions")
    skill: Mapped["Skill"] = relationship()


class WebAppAuditRecord(Base):
    __tablename__ = "web_app_audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    instance_id: Mapped[str] = mapped_column(ForeignKey("web_app_instances.id"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("web_app_sessions.id"), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instance: Mapped["WebAppInstance"] = relationship(back_populates="audit_records")
    session: Mapped["WebAppSession | None"] = relationship()


class SkillOperationLock(Base):
    __tablename__ = "skill_operation_locks"

    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    skill: Mapped["Skill"] = relationship()


class SkillSchedule(Base):
    __tablename__ = "skill_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    schedule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), default="America/Toronto", nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    skill: Mapped["Skill"] = relationship(back_populates="schedules")
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(back_populates="schedule")


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True, index=True)
    generation_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("skill_generation_requests.id"),
        nullable=True,
        index=True,
    )
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("skill_schedules.id"), nullable=True, index=True)
    request_scope: Mapped[str] = mapped_column(String(32), default="runtime", nullable=False, index=True)
    request_type: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    requested_dependencies_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    requested_network_domains_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    requested_filesystem_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    user_explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    skill: Mapped["Skill"] = relationship(back_populates="approval_requests")
    generation_request: Mapped["SkillGenerationRequest"] = relationship(back_populates="approval_requests")
    schedule: Mapped["SkillSchedule"] = relationship(back_populates="approval_requests")


class SkillGenerationRequest(Base):
    __tablename__ = "skill_generation_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_skill_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    proposed_display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    requested_permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    requested_dependencies_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    requested_network_domains_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planned", nullable=False, index=True)
    proposed_skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_manager_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    proposed_skill: Mapped["Skill"] = relationship(back_populates="generation_requests")
    approval_requests: Mapped[list["ApprovalRequest"]] = relationship(back_populates="generation_request")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True, index=True)
    generation_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("skill_generation_requests.id"),
        nullable=True,
        index=True,
    )
    user_request: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_count_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    build_workflow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    blueprint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    final_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_reasoning_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    skill: Mapped["Skill | None"] = relationship()
    generation_request: Mapped["SkillGenerationRequest | None"] = relationship()
    steps: Mapped[list["AgentRunStep"]] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
    )

class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), nullable=False, index=True)
    step_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    task_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    approval_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_requests.id"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    input_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    agent_input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    codex_invocations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    agent_run: Mapped["AgentRun"] = relationship(back_populates="steps")
