import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import SkillGenerationRequest
from app.schemas.common import ChatIntent, SkillType
from app.services.permission_service import PermissionService
from app.services.project_plausibility import ProjectPlausibilityResult, ProjectPlausibilityService


UNSAFE_PATTERNS = (
    "delete files",
    "deletes files",
    "trade stocks",
    "trades stocks",
    "financial trading",
    "send replies without asking",
    "send emails without asking",
    "browser cookies",
    "ssh keys",
    "arbitrary shell",
    "shell commands",
    "make purchases",
    "post publicly",
)


@dataclass
class ChatOrchestrator:
    db: Session
    plausibility_service: ProjectPlausibilityService | None = None

    def __post_init__(self) -> None:
        if self.plausibility_service is None:
            self.plausibility_service = ProjectPlausibilityService()

    def classify(self, message: str, mode: str = "chat") -> ChatIntent:
        normalized = message.lower()
        if any(pattern in normalized for pattern in UNSAFE_PATTERNS):
            return "UNSAFE_OR_UNSUPPORTED"
        if mode == "project":
            return "CREATE_SKILL_PROPOSAL"
        return "DIRECT_ANSWER"

    def handle_message(self, message: str, mode: str = "chat") -> dict[str, Any]:
        intent = self.classify(message, mode)
        if intent == "UNSAFE_OR_UNSUPPORTED":
            return {
                "type": "unsafe_or_unsupported",
                "message": "That request is unsafe or unsupported for this MVP.",
            }
        if intent == "CREATE_SKILL_PROPOSAL":
            review = self.plausibility_service.evaluate(message)
            if not review.plausible:
                return {
                    "type": "project_not_plausible",
                    "message": "I would not turn that into a skill yet.",
                    "reason": review.reason,
                    "optional_projects": review.optional_projects,
                }
            generation_request = self.create_generation_request(message, review)
            permission_request = PermissionService(self.db).create_build_time_request(generation_request)
            return {
                "type": "skill_generation_plan",
                "generation_request": generation_request,
                "permission_request": permission_request,
            }
        return {
            "type": "direct_answer",
            "message": "I can answer that directly. Switch to Project mode when you want me to propose a reusable skill.",
        }

    def create_generation_request(
        self,
        message: str,
        plausibility_review: ProjectPlausibilityResult | None = None,
    ) -> SkillGenerationRequest:
        plan = self.build_generation_plan(message)
        if plausibility_review is not None:
            plan["plausibility_review"] = {
                "plausible": plausibility_review.plausible,
                "reason": plausibility_review.reason,
                "optional_projects": plausibility_review.optional_projects,
            }
        generation_request = SkillGenerationRequest(
            user_message=message,
            proposed_skill_name=plan["skill_name"],
            proposed_display_name=plan["display_name"],
            proposed_skill_type=plan["skill_type"],
            plan_json=plan,
            requested_permissions_json=plan["requested_permissions"],
            requested_dependencies_json=plan["requested_dependencies"],
            requested_network_domains_json=plan["requested_permissions"]["network"],
            risk_level=plan["risk_level"],
            status="awaiting_approval",
        )
        self.db.add(generation_request)
        self.db.commit()
        self.db.refresh(generation_request)
        return generation_request

    def build_generation_plan(self, message: str) -> dict[str, Any]:
        skill_type = self.infer_skill_type(message)
        skill_name = self.infer_skill_name(message, skill_type)
        permissions = self.infer_permissions(message, skill_type)
        dependencies = self.infer_dependencies(message, permissions)
        risk_level = self.infer_risk_level(permissions, dependencies)
        files_to_generate = ["manifest.json", "README.md"]
        if skill_type in {"instruction", "hybrid"}:
            files_to_generate.append("SKILL.md")
        if skill_type in {"automation", "hybrid"}:
            files_to_generate.extend(["skill.py", "tests/test_skill.py"])

        return {
            "goal": message,
            "skill_name": skill_name,
            "display_name": skill_name.replace("_", " ").replace("-", " ").title(),
            "skill_type": skill_type,
            "files_to_generate": files_to_generate,
            "expected_input": self.expected_input_for(message, skill_type),
            "expected_output": self.expected_output_for(skill_type),
            "requested_permissions": permissions,
            "requested_network_domains": permissions["network"],
            "requested_dependencies": dependencies,
            "tests_required": skill_type in {"automation", "hybrid"},
            "validation_steps": [
                "validate manifest.json",
                "inspect generated files",
                "run tests for automation or hybrid skills",
            ],
            "risk_level": risk_level,
            "automatic_actions_blocked": [
                "installing the skill",
                "running the skill",
                "installing packages without approval",
            ],
        }

    def infer_skill_type(self, message: str) -> SkillType:
        normalized = message.lower()
        wants_instruction = "instruction" in normalized or "how you analyze" in normalized
        wants_code = any(word in normalized for word in ("automation", "tool", "summarize", "digest", "tracking"))
        if wants_instruction and wants_code:
            return "hybrid"
        if wants_instruction:
            return "instruction"
        return "automation"

    def infer_skill_name(self, message: str, skill_type: SkillType) -> str:
        normalized = message.lower()
        if "ai" in normalized and any(term in normalized for term in ("nvidia", "amd", "broadcom", "chip")):
            return "ai_infra_news_digest"
        if "stock" in normalized and skill_type == "instruction":
            return "stock_analysis_instruction"
        words = re.findall(r"[a-zA-Z0-9]+", normalized)
        stop_words = {
            "create",
            "make",
            "build",
            "reusable",
            "skill",
            "automation",
            "tool",
            "that",
            "for",
            "the",
            "and",
            "my",
            "me",
            "a",
            "an",
        }
        useful = [word for word in words if word not in stop_words][:5]
        base = "_".join(useful) or "generated_skill"
        if not base.endswith(("skill", "digest", "workflow", "instruction")):
            base = f"{base}_skill"
        return base[:80]

    def infer_permissions(self, message: str, skill_type: SkillType) -> dict[str, Any]:
        if skill_type == "instruction":
            return {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": [],
                "secrets": [],
                "shell": False,
            }

        normalized = message.lower()
        network = []
        if any(term in normalized for term in ("news", "sources", "nvidia", "amd", "broadcom")):
            network = ["reuters.com", "apnews.com", "nvidia.com", "amd.com", "broadcom.com"]
        filesystem_read = []
        if "local file" in normalized or "local files" in normalized:
            filesystem_read = ["./input"]
        return {
            "network": network,
            "filesystem_read": filesystem_read,
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        }

    def infer_dependencies(self, message: str, permissions: dict[str, Any]) -> list[str]:
        normalized = message.lower()
        dependencies = []
        if permissions["network"] or "rss" in normalized:
            dependencies.extend(["requests", "feedparser"])
        return dependencies

    def infer_risk_level(self, permissions: dict[str, Any], dependencies: list[str]) -> str:
        if permissions["secrets"] or permissions["shell"]:
            return "high"
        if permissions["network"] or permissions["filesystem_read"] or dependencies:
            return "medium"
        return "low"

    def expected_input_for(self, message: str, skill_type: SkillType) -> dict[str, Any]:
        if skill_type == "instruction":
            return {"request": "string"}
        if "news" in message.lower():
            return {"topics": ["Nvidia", "AMD", "Broadcom", "data centers"], "max_items": 10}
        return {"input": "object"}

    def expected_output_for(self, skill_type: SkillType) -> dict[str, Any]:
        if skill_type == "instruction":
            return {"instructions": "markdown"}
        return {"title": "string", "items": [], "warnings": []}
