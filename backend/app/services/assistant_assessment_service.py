from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.models import ActTelegramBinding, ActTurn, AgentProposal, AssistantAssessmentState, TelegramBotConnection
from app.services.act_session_service import (
    ActSessionError,
    ActSessionService,
    AssistantSessionCapacityError,
)
from app.services.act_workspace_service import ensure_act_workspace
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.github_provider import IntegrationProviderError

ASSISTANT_ASSESSMENT_INTERVAL = timedelta(days=3)
ASSISTANT_ASSESSMENT_INSTRUCTION = "Do an assessment now."


class AssistantAssessmentError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class AssistantAssessmentService:
    def __init__(
        self,
        db: Session,
        *,
        now: Callable[[], datetime] | None = None,
        executor: InvocationExecutor | None = None,
    ) -> None:
        self.db = db
        self._now = now or utc_now
        self.executor = executor or InvocationExecutor(db)

    def notify_completed(self, turn: ActTurn) -> None:
        import logging

        from app.services.telegram_service import TelegramService

        proposals = list(self.db.scalars(select(AgentProposal).where(
            AgentProposal.source_session_id == turn.session_id
        )))
        connection = self.db.scalar(select(TelegramBotConnection).where(
            TelegramBotConnection.role == "assistant_agent", TelegramBotConnection.status == "connected"
        ))
        if connection is not None and connection.paired_chat_id:
            binding = self.db.get(ActTelegramBinding, connection.id)
            if binding is None:
                self.db.add(ActTelegramBinding(connection_id=connection.id, active_session_id=turn.session_id))
            else:
                binding.active_session_id = turn.session_id
            turn.delivery_connection_id = connection.id
            turn.delivery_chat_id = connection.paired_chat_id
            turn.delivery_status = "pending"
            self.db.commit()
        try:
            TelegramService(self.db).execute_notification({
                "title": "Assessment Success" if turn.status == "succeeded" else "Assessment Fail",
                "description": f"You have {len(proposals)} proposals.",
            })
        except Exception:
            logging.getLogger(__name__).warning("Assistant assessment Telegram notification could not be delivered")

    def status(self) -> dict[str, Any]:
        state = self._state()
        return self._serialize(state)

    def configure(self, enabled: bool) -> dict[str, Any]:
        state = self._state()
        if enabled and not state.enabled:
            anchor_at = self._as_utc(self._now())
            state.enabled = True
            state.anchor_at = anchor_at
            state.next_run_at = anchor_at + ASSISTANT_ASSESSMENT_INTERVAL
        elif not enabled and state.enabled:
            state.enabled = False
            state.anchor_at = None
            state.next_run_at = None
        self.db.commit()
        self.db.refresh(state)
        return self._serialize(state)

    def reconcile(self) -> dict[str, Any]:
        state = self._state()
        if state.enabled and state.anchor_at is None:
            state.anchor_at = self._as_utc(self._now())
        if state.enabled and state.next_run_at is None:
            state.next_run_at = self._as_utc(state.anchor_at) + ASSISTANT_ASSESSMENT_INTERVAL
        if not state.enabled and state.next_run_at is not None:
            state.next_run_at = None
        self.db.commit()
        self.db.refresh(state)
        return self._serialize(state)

    def latest_due_at(self, now: datetime | None = None) -> datetime | None:
        state = self._state()
        if not state.enabled or state.anchor_at is None:
            return None
        anchor_at = self._as_utc(state.anchor_at)
        first_due_at = anchor_at + ASSISTANT_ASSESSMENT_INTERVAL
        checked_at = self._as_utc(now or self._now())
        if first_due_at > checked_at:
            return None
        elapsed = checked_at - first_due_at
        return first_due_at + ASSISTANT_ASSESSMENT_INTERVAL * int(
            elapsed / ASSISTANT_ASSESSMENT_INTERVAL
        )

    def run_now(self) -> dict[str, Any]:
        state = self._state()
        if not state.enabled:
            raise AssistantAssessmentError("Assistant assessments are paused")
        return self._queue_assessment(state, scheduled_for_at=None)

    def run_scheduled(self, scheduled_for_at: datetime) -> dict[str, Any]:
        state = self._state()
        scheduled_for_at = self._as_utc(scheduled_for_at)
        if not state.enabled:
            result = {
                "status": "blocked",
                "reason": "Assistant assessments were paused before execution",
            }
            self._record_result(state, result["status"], scheduled_for_at=scheduled_for_at)
            return result
        return self._queue_assessment(state, scheduled_for_at=scheduled_for_at)

    def _queue_assessment(
        self,
        state: AssistantAssessmentState,
        *,
        scheduled_for_at: datetime | None,
    ) -> dict[str, Any]:
        try:
            self.cleanup_proposals()
            sessions = ActSessionService(self.db, agent_id="assistant")
            session = sessions.create_session(origin="assessment", commit=False)
            turn = sessions.enqueue_turn(
                session.id,
                ASSISTANT_ASSESSMENT_INSTRUCTION,
                commit=True,
            )
        except AssistantSessionCapacityError as exc:
            self.db.rollback()
            message = str(exc)
            state = self._state()
            self._record_result(state, "blocked", scheduled_for_at=scheduled_for_at)
            return {"status": "blocked", "reason": message}
        except ActSessionError as exc:
            self.db.rollback()
            state = self._state()
            self._record_result(state, "failed", scheduled_for_at=scheduled_for_at)
            return {"status": "failed", "reason": str(exc)}
        except Exception as exc:
            self.db.rollback()
            state = self._state()
            self._record_result(state, "failed", scheduled_for_at=scheduled_for_at)
            return {
                "status": "failed",
                "reason": f"Assistant assessment could not be queued: {type(exc).__name__}",
            }

        self._record_result(state, "queued", scheduled_for_at=scheduled_for_at)
        return {
            "status": "queued",
            "session_id": session.id,
            "turn_id": turn.id,
        }

    def cleanup_proposals(self) -> None:
        proposals = list(self.db.scalars(select(AgentProposal)))
        references = {
            reference for proposal in proposals for reference in proposal.references_json
            if reference.startswith(("todo:", "goal:")) and reference.partition(":")[2]
        }
        inactive: set[str] = set()
        todo_refs = {ref for ref in references if ref.startswith("todo:")}
        if todo_refs:
            try:
                context = InvocationContextFactory.trusted_system(
                    "assistant_assessment_cleanup",
                    initiating_action="Assistant proposal reference cleanup",
                )
                active = set()
                payload: dict[str, Any] = {"page_size": 100}
                seen = set()
                for _ in range(100):
                    outcome = self.executor.execute(
                        InvocationTargetRef(category="integration", target_id="notion.todo.list"),
                        payload,
                        context,
                    )
                    if outcome.output is None:
                        raise InvocationExecutionError(
                            "internal_failure",
                            "Notion todo listing returned no output",
                        )
                    page = outcome.output
                    active.update(f"todo:{todo['id']}" for todo in page["todos"] if not todo["done"])
                    if not page["has_more"]:
                        inactive.update(todo_refs - active)
                        break
                    cursor = page["next_cursor"]
                    if not cursor or cursor in seen:
                        break
                    seen.add(cursor)
                    payload["start_cursor"] = cursor
            except InvocationExecutionError:
                # Unavailable or incomplete sources are not evidence of deletion.
                pass
        goal_refs = {ref for ref in references if ref.startswith("goal:")}
        if goal_refs:
            try:
                inactive.update(self._inactive_goals(goal_refs))
            except IntegrationProviderError:
                pass
        removed = [proposal for proposal in proposals if inactive.intersection(proposal.references_json)]
        if not removed:
            return
        directory = ensure_act_workspace().knowledge / "assistant" / "plans"
        for proposal in removed:
            # Only backend-assigned proposal IDs determine the deletion path.
            (directory / f"{proposal.id}.json").unlink(missing_ok=True)
            self.db.delete(proposal)
        self.db.commit()

    @staticmethod
    def _inactive_goals(references: set[str]) -> set[str]:
        from urllib.parse import quote

        provider = UrllibAtlasProviderAdapter()
        records = provider._record_list(provider._request(
            "/api/records?category=goal", None, timeout=10, max_bytes=2_000_000, method="GET"
        ), "goal")
        present = {f"goal:{record['id']}" for record in records if not record.get("trashed", False)}
        inactive = references - present
        for reference in references & present:
            goal_id = quote(reference.partition(":")[2], safe="")
            result = provider._request(
                f"/api/goals/{goal_id}/progression", None, timeout=10, max_bytes=2_000_000, method="GET"
            )
            if result["goal"]["progress"] >= 100:
                inactive.add(reference)
        return inactive

    def _record_result(
        self,
        state: AssistantAssessmentState,
        status: str,
        *,
        scheduled_for_at: datetime | None,
    ) -> None:
        state.last_run_at = self._as_utc(self._now())
        state.last_status = status
        if scheduled_for_at is not None and state.enabled:
            state.next_run_at = scheduled_for_at + ASSISTANT_ASSESSMENT_INTERVAL
        elif not state.enabled:
            state.next_run_at = None
        self.db.commit()
        self.db.refresh(state)

    def _state(self) -> AssistantAssessmentState:
        state = self.db.get(AssistantAssessmentState, 1)
        if state is None:
            state = AssistantAssessmentState(id=1, enabled=False)
            self.db.add(state)
            self.db.commit()
            self.db.refresh(state)
        return state

    @staticmethod
    def _serialize(state: AssistantAssessmentState) -> dict[str, Any]:
        return {
            "enabled": state.enabled,
            "next_run_at": state.next_run_at,
            "last_run_at": state.last_run_at,
            "last_status": state.last_status,
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
