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
SkillType = Literal["instruction", "automation", "hybrid"]
InterfaceType = Literal["chat", "tool", "hidden"]
SkillStatus = Literal["building", "proposed", "installed", "disabled", "failed", "deleted"]
SkillRunStatus = Literal["pending", "running", "succeeded", "failed", "blocked"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired", "superseded"]
PermissionRequestScope = Literal["build_time", "runtime"]
ScheduleStatus = Literal["pending", "active", "paused", "denied", "deleted"]
ScheduleType = Literal["daily", "weekly", "interval"]
ChatIntent = Literal[
    "DIRECT_ANSWER",
    "CREATE_SKILL_PROPOSAL",
    "USE_EXISTING_SKILL",
    "MODIFY_EXISTING_SKILL",
    "APPROVAL_REQUIRED",
    "UNSAFE_OR_UNSUPPORTED",
]
GenerationRequestStatus = Literal[
    "planned",
    "awaiting_approval",
    "approved",
    "generating",
    "generated",
    "failed",
    "cancelled",
]
AgentRunType = Literal["build_skill", "repair_skill", "update_skill"]
AgentRunStatus = Literal["pending", "running", "waiting_for_approval", "succeeded", "failed", "cancelled", "blocked"]
AgentStepName = Literal["product_manager", "builder", "tester", "security_reviewer"]
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
ProductManagerDecision = Literal[
    "request_permission",
    "build_next_milestone",
    "run_tests",
    "repair_current_milestone",
    "ask_user_for_input",
    "finish_ready_for_review",
    "stop_failed",
    "stop_unsupported",
]
