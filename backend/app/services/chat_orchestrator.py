from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import SkillGenerationRequest
from app.services.direct_chat_service import DirectChatService
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.permission_service import PermissionService
from app.services.project_plausibility import ProjectPlausibilityResult, ProjectPlausibilityService
from app.services.skill_plan_service import SkillPlanService


@dataclass
class ChatOrchestrator:
    db: Session
    plausibility_service: ProjectPlausibilityService | None = None
    direct_chat_service: DirectChatService | None = None
    skill_plan_service: SkillPlanService | None = None

    def __post_init__(self) -> None:
        if self.plausibility_service is None:
            self.plausibility_service = ProjectPlausibilityService()
        if self.direct_chat_service is None:
            self.direct_chat_service = DirectChatService()
        if self.skill_plan_service is None:
            self.skill_plan_service = SkillPlanService()

    def handle_message(self, message: str, mode: str = "chat") -> dict[str, Any]:
        if mode == "project":
            review = self.plausibility_service.evaluate(message)
            if not review.plausible:
                return {
                    "type": "project_not_plausible",
                    "message": "I would not turn that into a skill yet.",
                    "reason": review.reason,
                    "optional_projects": review.optional_projects,
                }
            generation_request = self.create_generation_request(message, review)
            AgentWorkflowService(self.db).create_build_run(generation_request)
            permission_request = PermissionService(self.db).create_build_time_request(generation_request)
            return {
                "type": "skill_generation_plan",
                "generation_request": generation_request,
                "permission_request": permission_request,
            }
        return {
            "type": "direct_answer",
            "message": self.direct_chat_service.answer(message),
        }

    def create_generation_request(
        self,
        message: str,
        plausibility_review: ProjectPlausibilityResult | None = None,
    ) -> SkillGenerationRequest:
        plan = self.skill_plan_service.build_generation_plan(message)
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
