from typing import Literal

MemoryCategory = Literal[
    "interests",
    "goals",
    "preferences",
    "routines",
    "trusted_sources",
    "blocked_sources",
    "writing_style",
    "risk_tolerance",
]
RiskLevel = Literal["low", "medium", "high", "blocked"]
SkillRuntime = Literal["function", "web_app"]
SkillStatus = Literal["building", "proposed", "installed", "failed", "deleted"]
SkillRunStatus = Literal["pending", "running", "succeeded", "partial", "failed", "blocked"]
WebAppInstanceStatus = Literal["starting", "ready", "healthy", "unhealthy", "stopped", "failed"]
WebAppSessionStatus = Literal["active", "closed", "expired"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired", "superseded"]
PermissionRequestScope = Literal["build_time", "runtime"]
ScheduleStatus = Literal["pending", "active", "paused", "denied"]
ScheduleType = Literal["daily", "weekly", "interval"]
GenerationRequestStatus = Literal[
    "planned",
    "needs_input",
    "awaiting_approval",
    "approved",
    "generating",
    "generated",
    "failed",
    "cancelled",
]
AgentRunType = Literal["build_skill", "repair_skill", "update_skill"]
SkillVersionStatus = Literal["active", "draft", "proposed_update", "archived", "discarded"]
SkillVersionActor = Literal["user", "agent", "system"]
AgentRunStatus = Literal["pending", "running", "waiting_for_approval", "paused", "succeeded", "failed", "cancelled", "blocked"]
AgentStepName = Literal["product_manager", "builder", "tester"]
AgentRunStepStatus = Literal[
    "pending",
    "running",
    "waiting_for_approval",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
    "blocked",
]
