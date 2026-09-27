from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.integrations.invocation import IntegrationInvocationError, IntegrationInvocationService
from app.models import ActSession, ActTurn, InvocationApproval, McpAuditRecord
from app.schemas.agents import AgentOpportunityReport
from app.services.act_telegram_file_service import (
    ActTelegramFileCapability,
    ActTelegramFileError,
)
from app.services.agent_policy_service import (
    OPPORTUNITY_REPORT_TOOL_ID,
    AgentPermissionError,
    AgentPolicyService,
)
from app.services.agent_proposal_service import AgentProposalService
from app.services.browser_authentication_bridge import BrowserAuthenticationBridgeClient
from app.services.browser_authentication_service import (
    BrowserAuthenticationCapability,
    BrowserAuthenticationError,
)
from app.services.integration_service import build_default_integration_service
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
        if target.target_id == OPPORTUNITY_REPORT_TOOL_ID:
            return self._write_opportunity_report(input_json, context)
        if target.target_id != "plan_approval_request":
            raise InvocationExecutionError("not_exposed", "Agent-private target is unavailable")
        if (
            context.principal_kind != "agent"
            or context.agent_id != "assistant"
            or context.agent_session_id is None
        ):
            raise InvocationExecutionError(
                "agent_permission_denied",
                "Plan requests require an authenticated Assistant session",
            )
        try:
            policy = AgentPolicyService(self.db)
            policy.require_session("assistant", context.agent_session_id)
            policy.require_function("assistant", target.target_id)
            proposal = AgentProposalService(self.db).submit(context, input_json)
        except AgentPermissionError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        except ValueError as exc:
            raise InvocationExecutionError("invalid_input", str(exc)) from None
        return InvocationOutcome(
            status="pending_approval",
            output={"status": proposal.status, "proposal_id": proposal.id},
        )

    def _write_opportunity_report(
        self,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if (
            context.principal_kind != "agent"
            or context.agent_id != "assistant"
            or context.agent_session_id is None
            or context.agent_turn_id is None
        ):
            raise InvocationExecutionError(
                "agent_permission_denied", "Report writing requires an active Assistant assessment turn"
            )
        try:
            policy = AgentPolicyService(self.db)
            policy.require_session("assistant", context.agent_session_id)
            policy.require_function("assistant", OPPORTUNITY_REPORT_TOOL_ID)
        except AgentPermissionError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        session = self.db.get(ActSession, context.agent_session_id)
        turn = self.db.get(ActTurn, context.agent_turn_id)
        first_turn_id = self.db.scalar(
            select(ActTurn.id).where(ActTurn.session_id == context.agent_session_id)
            .order_by(ActTurn.id).limit(1)
        )
        if (
            session is None or session.origin != "assessment"
            or turn is None or turn.session_id != session.id
            or turn.status != "running" or turn.id != first_turn_id
        ):
            raise InvocationExecutionError(
                "agent_permission_denied", "Report writing is limited to an active assessment"
            )
        prior = self.db.scalar(select(McpAuditRecord).where(
            McpAuditRecord.agent_turn_id == turn.id,
            McpAuditRecord.function_id == OPPORTUNITY_REPORT_TOOL_ID,
            McpAuditRecord.status == "succeeded",
        ).order_by(McpAuditRecord.id.desc()).limit(1))
        if prior is not None and prior.resource and prior.resource.startswith("notion-page:"):
            return InvocationOutcome(
                status="succeeded",
                output={"report_id": prior.resource.removeprefix("notion-page:")},
                audit_resource=prior.resource,
            )
        try:
            report = AgentOpportunityReport.model_validate(input_json)
        except ValidationError:
            raise InvocationExecutionError("invalid_input", "Opportunity report input is invalid") from None
        children = [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [
                    {"type": "text", "text": {"content": f"{item.name} — {item.description}"}},
                ]},
            }
            for item in report.opportunities
        ]
        trusted = InvocationContextFactory.trusted_system(
            "assistant_opportunity_report",
            initiating_action=f"assistant_assessment_turn_{turn.id}",
        )
        try:
            result = IntegrationInvocationService(
                self.db,
                compatibility_service=build_default_integration_service(self.db),
            ).execute(trusted, "notion.report.create", {
                "name": f"Opportunity Scout — {datetime.now(UTC).date().isoformat()}",
                "select": "Opportunities",
                "children": children,
            })
        except IntegrationInvocationError as exc:
            raise InvocationExecutionError(exc.error_type, str(exc)) from None
        report_id = result.output["id"]
        return InvocationOutcome(
            status="succeeded",
            output={"report_id": report_id},
            audit_resource=f"notion-page:{report_id}",
        )

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
