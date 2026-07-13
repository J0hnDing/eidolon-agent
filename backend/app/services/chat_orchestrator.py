from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SkillGenerationRequest
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.codex_service import CodexService
from app.services.direct_chat_service import DirectChatService
from app.services.permission_service import PermissionService
from app.services.skill_plan_service import SkillPlanService


@dataclass
class ChatOrchestrator:
    db: Session
    direct_chat_service: DirectChatService | None = None
    skill_plan_service: SkillPlanService | None = None
    codex_service: CodexService | None = None

    def __post_init__(self) -> None:
        if self.direct_chat_service is None:
            self.direct_chat_service = DirectChatService(db=self.db)
        if self.skill_plan_service is None:
            self.skill_plan_service = SkillPlanService(db=self.db)

    def handle_message(
        self,
        message: str,
        mode: str = "chat",
        generation_request_id: int | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if mode == "project":
            generation_request = self._project_generation_request_for_message(
                message,
                generation_request_id=generation_request_id,
                conversation_id=conversation_id,
            )
            agent_run = AgentWorkflowService(
                self.db,
                codex_service=self.codex_service,
                project_root=self.codex_service.project_root if self.codex_service is not None else None,
            ).create_build_run(generation_request)
            self.db.refresh(generation_request)
            decision = (agent_run.final_summary_json or {}).get("decision_json", {}).get("decision")
            if decision == "ask_user_for_input" or generation_request.status == "needs_input":
                return {
                    "type": "project_needs_input",
                    "message": "ProductManager needs a little more information before blueprinting.",
                    "question": str((agent_run.final_summary_json or {}).get("user_prompt") or agent_run.summary or ""),
                    "generation_request": generation_request,
                    "agent_run": agent_run,
                }
            if decision in {"stop_inplausible", "stop_unsupported"}:
                return {
                    "type": "project_not_plausible",
                    "message": "I would not turn that into a skill yet.",
                    "reason": str((agent_run.final_summary_json or {}).get("user_summary") or agent_run.summary or ""),
                }
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
        conversation_id: str | None = None,
    ) -> SkillGenerationRequest:
        plan = self.skill_plan_service.build_generation_plan(message)
        plan["project_conversation"] = [{"role": "user", "content": message}]
        if conversation_id:
            plan["frontend_conversation_id"] = conversation_id
        generation_request = SkillGenerationRequest(
            user_message=message,
            proposed_skill_name=plan["skill_name"],
            proposed_display_name=plan["display_name"],
            proposed_skill_type=plan["skill_type"],
            plan_json=plan,
            requested_permissions_json=plan["requested_permissions"],
            requested_dependencies_json=plan["requested_dependencies"],
            requested_network_domains_json=plan["requested_network_domains"],
            risk_level=plan["risk_level"],
            status="planned",
        )
        self.db.add(generation_request)
        self.db.commit()
        self.db.refresh(generation_request)
        return generation_request

    def _project_generation_request_for_message(
        self,
        message: str,
        *,
        generation_request_id: int | None,
        conversation_id: str | None,
    ) -> SkillGenerationRequest:
        if generation_request_id is not None:
            generation_request = self.db.get(SkillGenerationRequest, generation_request_id)
            if generation_request is not None and generation_request.status == "needs_input":
                return self._append_project_reply(generation_request, message)

        pending = self.db.scalar(
            select(SkillGenerationRequest)
            .where(SkillGenerationRequest.status == "needs_input")
            .order_by(SkillGenerationRequest.updated_at.desc(), SkillGenerationRequest.id.desc())
        )
        if pending is not None:
            pending_conversation = (pending.plan_json or {}).get("frontend_conversation_id")
            if conversation_id is None or pending_conversation in {None, conversation_id}:
                return self._append_project_reply(pending, message)

        return self.create_generation_request(message, conversation_id=conversation_id)

    def _append_project_reply(
        self,
        generation_request: SkillGenerationRequest,
        message: str,
    ) -> SkillGenerationRequest:
        existing_plan = dict(generation_request.plan_json or {})
        conversation = list(existing_plan.get("project_conversation") or [])
        if not conversation:
            conversation.append({"role": "user", "content": generation_request.user_message})
        conversation.append({"role": "user", "content": message})
        combined_message = "\n\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}"
            for item in conversation
            if isinstance(item, dict)
        )
        plan = self.skill_plan_service.build_generation_plan(combined_message)
        plan["project_conversation"] = conversation
        plan["original_user_message"] = existing_plan.get("original_user_message") or generation_request.user_message
        if existing_plan.get("frontend_conversation_id"):
            plan["frontend_conversation_id"] = existing_plan["frontend_conversation_id"]
        generation_request.user_message = combined_message
        generation_request.proposed_skill_name = plan["skill_name"]
        generation_request.proposed_display_name = plan["display_name"]
        generation_request.proposed_skill_type = plan["skill_type"]
        generation_request.plan_json = plan
        generation_request.requested_permissions_json = plan["requested_permissions"]
        generation_request.requested_dependencies_json = plan["requested_dependencies"]
        generation_request.requested_network_domains_json = plan["requested_network_domains"]
        generation_request.risk_level = plan["risk_level"]
        generation_request.status = "planned"
        generation_request.error_message = None
        self.db.commit()
        self.db.refresh(generation_request)
        return generation_request
