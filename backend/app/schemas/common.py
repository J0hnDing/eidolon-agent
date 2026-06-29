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
SkillStatus = Literal["proposed", "installed", "disabled", "failed", "deleted"]
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
