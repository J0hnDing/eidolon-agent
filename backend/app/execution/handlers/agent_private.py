from __future__ import annotations

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.models import InvocationApproval
from app.services.act_telegram_file_service import (
    ActTelegramFileCapability,
    ActTelegramFileError,
)
from app.services.agent_policy_service import AgentPermissionError, AgentPolicyService
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
        if target.target_id == "browser.authenticate":
            return self._authenticate_browser(input_json, context)
        if target.target_id == "telegram.send_file":
            return self._send_telegram_file(input_json, context)
        raise InvocationExecutionError("not_exposed", "Agent-private target is unavailable")

    def _authenticate_browser(
        self,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if (
            context.principal_kind != "agent"
            or context.agent_id != "act"
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
            policy.require_session("act", context.agent_session_id)
            policy.require_function("act", "browser.authenticate")
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
            or context.agent_id != "act"
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
            policy.require_session("act", context.agent_session_id)
            policy.require_function("act", "telegram.send_file")
            result = ActTelegramFileCapability(self.db).send(
                context.agent_session_id,
                path,
                caption,
            )
        except AgentPermissionError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        except (ActTelegramFileError, TelegramProviderError, TelegramServiceError) as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        return InvocationOutcome(status="succeeded", output=result)

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> InvocationOutcome:
        raise InvocationExecutionError("invalid_target", "Agent-private targets do not use this approval path")
