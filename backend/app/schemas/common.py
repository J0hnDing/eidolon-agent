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
RiskLevel = Literal["low", "medium", "high"]
SkillStatus = Literal["proposed", "installed", "disabled", "failed", "deleted"]
SkillRunStatus = Literal["pending", "running", "succeeded", "failed", "blocked"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired"]
