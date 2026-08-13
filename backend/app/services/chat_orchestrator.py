from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SkillGenerationRequest
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.codex_service import CodexService
from app.services.direct_chat_service import DirectChatService
from app.services.permission_service import PermissionService


@dataclass
class ChatOrchestrator:
    db: Session
    direct_chat_service: DirectChatService | None = None
    codex_service: CodexService | None = None

    def __post_init__(self) -> None:
        if self.direct_chat_service is None:
            self.direct_chat_service = DirectChatService(db=self.db)

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
        placeholder_name = f"pending_project_{uuid4().hex[:12]}"
        plan: dict[str, Any] = {
            "project_conversation": [{"role": "user", "content": message}],
        }
        if conversation_id:
            plan["frontend_conversation_id"] = conversation_id
        generation_request = SkillGenerationRequest(
            user_message=message,
            proposed_skill_name=placeholder_name,
            proposed_display_name="Pending project",
            plan_json=plan,
            requested_permissions_json={},
            requested_dependencies_json=[],
            requested_network_domains_json=[],
            risk_level="low",
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
        plan = dict(existing_plan)
        plan["project_conversation"] = conversation
        plan["original_user_message"] = existing_plan.get("original_user_message") or generation_request.user_message
        plan.pop("pending_user_prompt", None)
        generation_request.user_message = combined_message
        generation_request.plan_json = plan
        generation_request.status = "planned"
        generation_request.error_message = None
        self.db.commit()
        self.db.refresh(generation_request)
        return generation_request
