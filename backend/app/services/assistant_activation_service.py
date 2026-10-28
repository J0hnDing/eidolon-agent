"""One durable agenda activation path, including calendar recurring tasks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ActTurn, AgentProposal, AssistantAgendaItem, AssistantAgendaOccurrence, AssistantState
from app.services.assistant_recurrence import (
    finish_occurrence,
    latest_at,
    next_at,
    occurrence_context,
    reconcile,
    utc,
)


class AssistantActivationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _state(self) -> AssistantState:
        from app.services.assistant_service import AssistantService

        return AssistantService(self.db).state()

    def status(self) -> dict:
        from app.services.agent_memory_service import AgentMemoryService
        from app.services.assistant_changes import AssistantChangeService
        from app.services.assistant_daily_plan import daily_plan_status
        from app.services.assistant_initialization import AssistantInitializationService
        from app.services.assistant_planning import daily_interaction_status
        from app.services.assistant_questions import AssistantQuestionService
        from app.services.assistant_session_clock import REFRESH_TIME, refresh_boundary

        state = self._state()
        now = datetime.now(UTC)
        return {
            "enabled": state.enabled, "timezone": state.timezone,
            "primary_session_id": state.primary_session_id,
            "thread_started_at": state.thread_started_at, "refresh_pending": state.refresh_pending,
            "refresh_time": REFRESH_TIME,
            "next_refresh_at": refresh_boundary(now, state.timezone, next_boundary=True),
            "memory_root": str(AgentMemoryService(self.db).memory_root),
            "change_tracking": AssistantChangeService(self.db).status(),
            "initialization": AssistantInitializationService(self.db).status(),
            "question_asking": AssistantQuestionService(self.db).status(state, now),
            "daily_interaction": daily_interaction_status(self.db, state.timezone, now),
            "daily_plan": daily_plan_status(self.db, state.timezone, now),
        }

    def configure(self, payload: dict) -> dict:
        from app.schemas.assistant import AssistantSettingsUpdate

        values = AssistantSettingsUpdate.model_validate(payload).model_dump(exclude_unset=True)
        state = self._state()
        for name, value in values.items():
            setattr(state, name, value)
        self.db.commit()
        return self.status()

    def run_now(self, item_id: int, expected_revision: int) -> dict:
        return self._activate(item_id=item_id, expected_revision=expected_revision)

    def tick(self) -> dict:
        return self._activate()

    def _activate(self, *, item_id: int | None = None, expected_revision: int | None = None) -> dict:
        from app.services.act_session_service import ActSessionService
        from app.services.assistant_initialization import AssistantInitializationService
        from app.services.assistant_service import AssistantService

        state = self._state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        self.db.refresh(state)
        manual_item = None
        if item_id is not None:
            manual_item = self.db.get(AssistantAgendaItem, item_id, populate_existing=True)
            if manual_item is None or manual_item.revision != expected_revision:
                raise ValueError("Agenda item changed or no longer exists; read it again")
            if not manual_item.recurrence_json or manual_item.kind != "task" or manual_item.status != "open":
                raise ValueError("Run now requires an active recurring task")
        if not state.enabled:
            self.db.commit()
            return {"status": "disabled"}
        now = datetime.now(UTC)
        session = AssistantService(self.db).primary_session(commit=False)
        if self.db.scalar(select(ActTurn.id).where(
            ActTurn.session_id == session.id, ActTurn.status.in_(("running", "queued")),
        ).limit(1)):
            self.db.commit()
            return {"status": "busy", "session_id": session.id}
        ready = reconcile(self.db, now)
        selected = next((pair for pair in ready if pair[0].id == item_id), None) if manual_item else (
            ready[0] if ready else None)
        if manual_item is not None and selected is None:
            # Manual work has its own instant; it does not consume the next scheduled run.
            # An explicit retry may bypass its delay, but retains prior-attempt evidence.
            occurrence = self.db.scalar(select(AssistantAgendaOccurrence).where(
                AssistantAgendaOccurrence.agenda_item_id == item_id,
                AssistantAgendaOccurrence.schedule_version == manual_item.schedule_version,
                AssistantAgendaOccurrence.status == "failed",
                AssistantAgendaOccurrence.scheduled_at >= latest_at(manual_item.recurrence_json, now),
            ).order_by(AssistantAgendaOccurrence.scheduled_at.desc()).limit(1))
            if occurrence is None:
                occurrence = AssistantAgendaOccurrence(agenda_item_id=item_id,
                    schedule_version=manual_item.schedule_version, scheduled_at=now, status="failed", attempts=0)
            selected = (manual_item, occurrence)
        if selected and selected[1].id is None:
            self.db.add(selected[1])
            self.db.flush()
        agenda = [selected[0]] if selected else [item for item in self.db.scalars(
            select(AssistantAgendaItem).where(
                AssistantAgendaItem.status.in_(("open", "waiting")),
                (AssistantAgendaItem.kind != "question") | (AssistantAgendaItem.status == "open"),
                AssistantAgendaItem.due_at <= now,
            ).order_by(AssistantAgendaItem.due_at, AssistantAgendaItem.id))
            if not item.recurrence_json][:30]
        if not selected and agenda:
            from app.services.assistant_daily_plan import daily_plan_status

            plan = daily_plan_status(self.db, state.timezone, now)
            interaction_tasks = {item["id"] for item in plan["items"]["interaction"] if item["kind"] == "task"}
            # A casual message must belong to one personal interaction, rather
            # than make a batch of distinct planned conversations look delivered.
            interaction = next((item for item in agenda if item.id in interaction_tasks), None)
            if interaction is not None:
                agenda = [interaction]
        executions = [] if selected else list(self.db.scalars(select(AgentProposal).where(
            AgentProposal.execution_status.in_(("succeeded", "failed", "cancelled", "interrupted")),
            AgentProposal.execution_state_json["assistant_outcome_turn_id"].as_integer().is_(None),
        ).order_by(AgentProposal.id).limit(20)))
        initialize_atlas = False if selected else AssistantInitializationService(self.db).ready(now)
        kind = "agenda" if agenda else "execution" if executions else "initialization" if initialize_atlas else None
        if kind is None:
            self.db.commit()
            return {"status": "idle"}
        activation = {"kind": kind, "at": now.isoformat(), "manual": manual_item is not None,
            "initialize_atlas": initialize_atlas,
            "agenda": [{"id": item.id, "revision": item.revision, "title": item.title,
                        "kind": item.kind, "question_category": item.question_category,
                        "due_at": utc(item.due_at).isoformat() if item.due_at else None,
                        "recurrence": item.recurrence_json, "notes": item.notes} for item in agenda],
            "executions": [self._execution_context(item) for item in executions]}
        prompt = ("Review this Assistant activation using current context. Reassess what is useful, "
                  "advance authorized work, maintain the agenda and understanding, and communicate only "
                  "when it helps the user. An empty response is appropriate when no message is useful.")
        if selected:
            item, occurrence = selected
            previous_turn_id = occurrence.turn_id
            occurrence.attempts += 1
            activation["occurrence"] = {**occurrence_context(occurrence, item.recurrence_json),
                                        "previous_turn_id": previous_turn_id}
            if item.completion_requirement:
                activation["completion_requirement"] = item.completion_requirement
            activation["agenda"] = [{"id": item.id, "revision": item.revision,
                                     "kind": item.kind, "title": item.title}]
            prompt = item.notes or item.title
        turn = ActSessionService(self.db, agent_id="assistant").enqueue_turn(session.id, prompt, commit=False)
        turn.assistant_context_json = activation
        if selected:
            occurrence.turn_id, occurrence.status, occurrence.retry_at = turn.id, "queued", None
            item.due_at = next_at(item.recurrence_json, now)
        for item in agenda:
            if item.kind != "question" and not item.recurrence_json:
                item.due_at = None
            item.last_turn_id = turn.id
        for proposal in executions:
            proposal.execution_state_json = {**(proposal.execution_state_json or {}),
                                             "assistant_outcome_turn_id": turn.id}
        # enqueue_turn snapshots the paired chat; background results use that same path.
        if turn.delivery_connection_id is not None:
            turn.delivery_provider, turn.delivery_status = "telegram", "pending"
        self.db.commit()
        from app.services.agent_dispatchers import agent_dispatchers

        agent_dispatchers["assistant"].notify()
        return {"status": "queued", "session_id": session.id, "turn_id": turn.id, "kind": kind}

    def on_turn_completed(self, turn: ActTurn) -> None:
        finish_occurrence(self.db, turn, datetime.now(UTC))
        from app.services.assistant_changes import AssistantChangeService
        from app.services.assistant_initialization import AssistantInitializationService

        AssistantChangeService(self.db).finish(turn)
        AssistantInitializationService(self.db).finish(turn)
        self.db.commit()
        activation = turn.assistant_context_json or {}
        for snapshot in activation.get("agenda", []):
            if snapshot["kind"] != "question":
                continue
            item = self.db.get(AssistantAgendaItem, snapshot["id"])
            if (item and item.status == "open" and item.last_turn_id == turn.id
                    and item.revision == snapshot["revision"]):
                # No question was sent or rescheduled. Retry evaluation without a busy loop.
                item.due_at = datetime.now(UTC) + timedelta(minutes=15)
                item.revision += 1
        self.db.commit()
        if activation and turn.status in {"failed", "cancelled", "interrupted"}:
            for snapshot in activation.get("agenda", []):
                if snapshot["kind"] == "question" or activation.get("occurrence"):
                    continue
                item = self.db.get(AssistantAgendaItem, snapshot["id"])
                if item and item.last_turn_id == turn.id and item.revision == snapshot["revision"]:
                    item.status = "waiting"
                    item.notes = (item.notes + "\nActivation interrupted or failed; review the outcome before continuing.").strip()
                    item.revision += 1
            for execution in activation.get("executions", []):
                title = ("Review outcome: " + execution["title"])[:200]
                existing = self.db.scalar(select(AssistantAgendaItem.id).where(
                    AssistantAgendaItem.title == title,
                    AssistantAgendaItem.status.in_(("open", "waiting")),
                ))
                if existing is None:
                    self.db.add(AssistantAgendaItem(
                        title=title, status="waiting", source_session_id=turn.session_id,
                        last_turn_id=turn.id,
                        notes="Outcome evaluation was interrupted. Check evidence before taking further action.\n"
                              + json.dumps(execution, ensure_ascii=False, default=str)[:10000],
                    ))
            self.db.commit()

        self._continue_planned_tasks(turn, activation)

    def _continue_planned_tasks(self, turn: ActTurn, activation: dict) -> None:
        """Keep selected responsibilities awake if execution forgot to resolve them."""
        if activation.get("occurrence") or turn.cancel_requested_at is not None:
            return
        from app.services.assistant_daily_plan import daily_plan_status

        now = datetime.now(UTC)
        state = self._state()
        plan = daily_plan_status(self.db, state.timezone, now)
        planned = {item["id"]: (category, item) for category, items in plan["items"].items() for item in items}
        for snapshot in activation.get("agenda", []):
            selection = planned.get(snapshot["id"])
            if selection is None or snapshot["kind"] != "task":
                continue
            category, progress = selection
            item = self.db.get(AssistantAgendaItem, snapshot["id"])
            if (item is None or item.last_turn_id != turn.id or item.recurrence_json
                    or item.status == "dismissed" or item.due_at is not None or progress["completed"]):
                continue
            if turn.status == "succeeded":
                if item.status == "open" or category == "interaction" and item.status == "done":
                    item.status = "open"
                    item.due_at = now + timedelta(minutes=15)
                    item.revision += 1
            elif (turn.status in {"failed", "interrupted"} and item.status == "waiting"
                  and item.revision == snapshot["revision"] + 1):
                # The existing failure path recorded uncertainty. Wake a review
                # of actual effects, rather than silently losing today's work.
                item.due_at = now + timedelta(minutes=15)
                item.revision += 1
        self.db.commit()

    def _execution_context(self, proposal: AgentProposal) -> dict:
        turn = self.db.get(ActTurn, proposal.act_turn_id) if proposal.act_turn_id else None
        if turn is not None:
            from app.services.agent_memory_service import AgentMemoryService

            content = AgentMemoryService(self.db).read_turn_content(turn)
            result = (content["assistant_message"] or turn.error_message or "") if content["available"] else "Chat history has expired."
        else:
            result = proposal.execution_state_json
        return {"proposal_id": proposal.id, "title": proposal.title,
                "status": proposal.execution_status,
                "result": result[:6000] if isinstance(result, str) else result}

