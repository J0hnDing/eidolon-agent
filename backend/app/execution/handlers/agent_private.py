from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.models import ActTurn, InvocationApproval
from app.services.act_chat_message_service import ActChatMessageCapability, ActChatMessageError
from app.services.act_telegram_file_service import (
    ActTelegramFileCapability,
    ActTelegramFileError,
)
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
from app.services.assistant_tools import ASSISTANT_TOOL_IDS
from app.services.browser_authentication_bridge import BrowserAuthenticationBridgeClient
from app.services.browser_authentication_service import (
    BrowserAuthenticationCapability,
    BrowserAuthenticationError,
)
from app.services.telegram_provider import TelegramProviderError
from app.services.telegram_service import TelegramServiceError


class AgentPrivateHandler:
    def __init__(self, db: Session) -> None:
        self.db = db

    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if target.target_id in ASSISTANT_TOOL_IDS:
            return self._assistant_tool(target.target_id, input_json, context)
        if target.target_id == "browser.authenticate":
            return self._authenticate_browser(input_json, context)
        if target.target_id == "telegram.send_file":
            return self._send_telegram_file(input_json, context)
        if target.target_id == "chat.send_message":
            return self._send_chat_message(input_json, context)
        raise InvocationExecutionError("not_exposed", "Agent-private target is unavailable")

    def _assistant_tool(self, target_id: str, payload: dict, context: InvocationContext) -> InvocationOutcome:
        from pydantic import ValidationError

        from app.schemas.assistant import AgendaCreate, AgendaUpdate
        from app.services.assistant_service import AssistantService

        if context.principal_kind != "agent" or context.agent_id != "assistant" or context.agent_session_id is None:
            raise InvocationExecutionError("agent_permission_denied", "Assistant tool requires its authenticated session")
        service = AssistantService(self.db)
        try:
            if target_id == "assistant.agenda.list":
                items = service.agenda(active_only=payload.get("active_only", True))
                if "kind" in payload:
                    items = [item for item in items if item["kind"] == payload["kind"]]
                start, limit = payload.get("offset", 0), payload.get("limit", 50)
                output = {"items": items[start:start + limit], "total": len(items),
                          "next_offset": start + limit if start + limit < len(items) else None}
            elif target_id == "assistant.agenda.create":
                output = service.create_item(AgendaCreate.model_validate(payload),
                    session_id=context.agent_session_id, turn_id=context.agent_turn_id)
            elif target_id == "assistant.agenda.update":
                output = service.update_item(payload["id"],
                    AgendaUpdate.model_validate({k: v for k, v in payload.items() if k != "id"}),
                    turn_id=context.agent_turn_id)
            elif target_id == "assistant.plan.save":
                from app.services.assistant_daily_plan import save_daily_plan

                output = save_daily_plan(self.db, payload, session_id=context.agent_session_id,
                                        turn_id=context.agent_turn_id)
            elif target_id == "assistant.initialization.acknowledge":
                from app.services.assistant_initialization import AssistantInitializationService
                if context.agent_turn_id is None:
                    raise ValueError("Initialization acknowledgement requires a running Assistant turn")
                output = AssistantInitializationService(self.db).acknowledge(context.agent_turn_id)
            elif target_id == "assistant.initialization.read":
                from app.services.assistant_initialization import AssistantInitializationService
                if context.agent_turn_id is None:
                    raise ValueError("Initialization requires a running Assistant turn")
                output = AssistantInitializationService(self.db).read(context.agent_session_id, context.agent_turn_id)
            elif target_id == "assistant.changes.list":
                from app.services.assistant_changes import AssistantChangeService
                if context.agent_turn_id is None:
                    raise ValueError("Change review requires a running Assistant turn")
                turn = self.db.get(ActTurn, context.agent_turn_id)
                if turn is None or turn.session_id != context.agent_session_id:
                    raise ValueError("Review belongs to another conversation")
                output = AssistantChangeService(self.db).read(turn.id,
                    offset=payload.get("offset", 0), limit=payload.get("limit", 50))
            elif target_id == "assistant.remember":
                output = service.remember(payload)
            elif target_id.startswith("assistant.memory."):
                from zoneinfo import ZoneInfo

                from app.services.agent_memory_service import AgentMemoryService

                turn = self.db.get(ActTurn, context.agent_turn_id) if context.agent_turn_id else None
                if (turn is None or turn.session_id != context.agent_session_id or turn.status != "running"
                        or turn.cancel_requested_at is not None):
                    raise ValueError("Memory tools require the current running Assistant turn")
                memory = AgentMemoryService(self.db)
                if target_id == "assistant.memory.history.read":
                    try:
                        ZoneInfo(payload["timezone"])
                    except (KeyError, ValueError):
                        raise ValueError("Unknown history timezone") from None
                    memory.sync_chat_history()
                    output = memory.read_chat_history(payload["day"], payload["timezone"])
                else:
                    output = memory.write_daily_summary(payload["day"], payload["summary"], source_turn_id=turn.id)
            elif target_id == "assistant.question.ask":
                from app.services.assistant_questions import AssistantQuestionService
                if context.agent_turn_id is None:
                    raise ValueError("Question asking requires a running Assistant turn")
                output = AssistantQuestionService(self.db).ask(payload["id"], payload["text"],
                    payload["expected_revision"], session_id=context.agent_session_id, turn_id=context.agent_turn_id)
            elif target_id == "assistant.approval.decide":
                output = service.decide_approval(payload, context.agent_session_id, context.agent_turn_id)
            else:
                output = service.request_approval(payload, context.agent_session_id, context.agent_turn_id)
        except (ValueError, ValidationError) as exc:
            self.db.rollback()
            raise InvocationExecutionError("invalid_input", str(exc)[:512]) from None
        except ActChatMessageError as exc:
            self.db.rollback()
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        return InvocationOutcome(status="succeeded", output=output)

    def _authenticate_browser(
        self,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if (
            context.principal_kind != "agent"
            or context.agent_id not in {"act", "assistant"}
            or context.agent_session_id is None
        ):
            raise InvocationExecutionError(
                "agent_permission_denied",
                "Browser authentication requires an authenticated Act session",
            )
        identity = input_json.get("identity")
        if not isinstance(identity, str):
            raise InvocationExecutionError("invalid_input", "Browser identity is required")
        try:
            policy = AgentPolicyService(self.db)
            policy.require_session(context.agent_id, context.agent_session_id)
            policy.require_function(context.agent_id, "browser.authenticate")
            result = BrowserAuthenticationCapability(
                self.db,
                bridge=BrowserAuthenticationBridgeClient(),
            ).authenticate(identity)
        except AgentPermissionError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        except BrowserAuthenticationError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        return InvocationOutcome(status="succeeded", output=result)

    def _send_telegram_file(
        self,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if (
            context.principal_kind != "agent"
            or context.agent_id not in {"act", "assistant"}
            or context.agent_session_id is None
        ):
            raise InvocationExecutionError(
                "agent_permission_denied",
                "Telegram workspace delivery requires an authenticated Act session",
            )
        path = input_json.get("path")
        caption = input_json.get("caption")
        if not isinstance(path, str) or (caption is not None and not isinstance(caption, str)):
            raise InvocationExecutionError("invalid_input", "A workspace file path and optional caption are required")
        try:
            policy = AgentPolicyService(self.db)
            policy.require_session(context.agent_id, context.agent_session_id)
            policy.require_function(context.agent_id, "telegram.send_file")
            result = ActTelegramFileCapability(self.db, agent_id=context.agent_id).send(
                context.agent_session_id,
                path,
                caption,
            )
        except AgentPermissionError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        except (ActTelegramFileError, TelegramProviderError, TelegramServiceError) as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        return InvocationOutcome(status="succeeded", output=result)

    def _send_chat_message(self, input_json: dict, context: InvocationContext) -> InvocationOutcome:
        if (context.principal_kind != "agent" or context.agent_id not in {"act", "assistant"}
                or context.agent_session_id is None):
            raise InvocationExecutionError("agent_permission_denied", "Live chat messaging requires an authenticated Act session")
        if set(input_json) - {"text", "wait_for_reply", "timeout_seconds"}:
            raise InvocationExecutionError("invalid_input", "Only message text and reply waiting options are accepted")
        try:
            policy = AgentPolicyService(self.db)
            policy.require_session(context.agent_id, context.agent_session_id)
            policy.require_function(context.agent_id, "chat.send_message")
            result = ActChatMessageCapability(self.db).send(
                context.agent_session_id, input_json.get("text"),
                input_json.get("wait_for_reply", False), input_json.get("timeout_seconds", 120),
            )
        except (AgentPermissionError, ActChatMessageError, TelegramProviderError, TelegramServiceError) as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        return InvocationOutcome(status="succeeded", output=result)

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> InvocationOutcome:
        raise InvocationExecutionError("invalid_target", "Agent-private targets do not use this approval path")
