"""Durable understanding and agenda, independent of any conversation's lifetime."""

import json
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ActLiveMessage,
    ActSession,
    ActTurn,
    AgentPolicy,
    AgentProposal,
    AssistantAgendaItem,
    AssistantAgendaOccurrence,
    AssistantFollowUp,
    AssistantState,
)
from app.schemas.assistant import AgendaCreate, AgendaUpdate


class AssistantService:
    def __init__(self, db: Session):
        self.db = db

    def primary_session(self, *, origin: str = "assistant", commit: bool = True) -> ActSession:
        """Resolve the single permanent chat under the singleton database lock."""
        state = self.state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        self.db.refresh(state)
        session = self.db.get(ActSession, state.primary_session_id) if state.primary_session_id else None
        if session is None or session.agent_id != "assistant":
            session = self.db.scalar(select(ActSession).where(
                ActSession.agent_id == "assistant", ActSession.status == "active",
            ).order_by(ActSession.updated_at.desc(), ActSession.id.desc()).limit(1))
        if session is None:
            from app.services.act_session_service import ActSessionService

            session = ActSessionService(self.db, agent_id="assistant")._new_session(origin=origin)
        session.status = "active"
        if state.primary_session_id != session.id:
            state.primary_session_id = session.id
            state.thread_started_at = None
        # Retain legacy conversations and their completed history, but give new
        # work exactly one owner. Delivery snapshots remain on the queued turns.
        old_ids = select(ActSession.id).where(
            ActSession.agent_id == "assistant", ActSession.id != session.id,
        )
        self.db.execute(update(ActTurn).where(
            ActTurn.session_id.in_(old_ids), ActTurn.status == "queued",
        ).values(session_id=session.id))
        self.db.execute(update(ActSession).where(
            ActSession.id.in_(old_ids), ActSession.status == "active",
        ).values(status="archived"))
        if commit:
            self.db.commit()
        return session

    def request_refresh(self, *, commit: bool = True) -> dict:
        session = self.primary_session(commit=False)
        state = self.state()
        state.refresh_pending = True
        if commit:
            self.db.commit()
            from app.services.agent_dispatchers import agent_dispatchers

            agent_dispatchers["assistant"].notify()
        return {"status": "refresh_pending", "session_id": session.id}

    def state(self) -> AssistantState:
        state = self.db.get(AssistantState, 1)
        if state is None:
            try:
                with self.db.begin_nested():
                    state = AssistantState(id=1)
                    self.db.add(state)
                    self.db.flush()
                    policy = self.db.get(AgentPolicy, "assistant")
                    if policy is not None:
                        policy.policy_json = {**policy.policy_json, "read_only": False}
                        policy.revision += 1
                    for proposal in self.db.scalars(select(AgentProposal).where(
                        AgentProposal.execution_status.in_(["succeeded", "cancelled"]),
                    )):
                        proposal.execution_state_json = {**(proposal.execution_state_json or {}),
                                                         "assistant_outcome_turn_id": 0}
                    # Preserve old follow-ups as responsibilities to reconsider, never stored messages to send.
                    migrated_proposals = set(self.db.scalars(select(AssistantFollowUp.proposal_id)))
                    for old in self.db.scalars(select(AssistantFollowUp).where(
                        AssistantFollowUp.status.in_(["waiting", "scheduled", "sent"]),
                    )):
                        proposal = self.db.get(AgentProposal, old.proposal_id)
                        self.db.add(AssistantAgendaItem(
                            kind="question", title=old.message[:200],
                            notes=f"Reconsider this former follow-up before asking.\n{old.message}\n"
                                  f"Depends on action {old.proposal_id}. Original timing: "
                                  + json.dumps(old.timing_json, default=str),
                            due_at=old.due_at, source_session_id=old.session_id,
                            status="open" if proposal and proposal.execution_status == "succeeded" else "waiting",
                        ))
                        old.status = "migrated"
                        migrated_proposals.add(old.proposal_id)
                    for proposal in self.db.scalars(select(AgentProposal).where(
                        AgentProposal.status.in_(["pending", "approved"]), AgentProposal.follow_up_json.is_not(None),
                    )):
                        if proposal.id in migrated_proposals:
                            continue
                        follow_up = proposal.follow_up_json
                        # SQLite JSON null is not SQL NULL, so the SQL predicate
                        # also selects proposals without an actual follow-up.
                        if not isinstance(follow_up, dict):
                            continue
                        message = follow_up.get("message")
                        if not isinstance(message, str) or not message.strip():
                            continue
                        self.db.add(AssistantAgendaItem(
                            kind="question", title=message[:200],
                            status="waiting", source_session_id=proposal.source_session_id,
                            notes=f"Reconsider after action {proposal.id} ({proposal.title}) completes. "
                                  "Do not ask if denied or unsuccessful. Original follow-up: "
                                  + json.dumps(follow_up, default=str),
                        ))
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                state = self.db.get(AssistantState, 1)
        from app.services.assistant_recurrence import migrate_schedules

        migrate_schedules(self.db, state)
        return state

    def agenda(self, *, active_only: bool = False) -> list[dict]:
        self.state()
        query = select(AssistantAgendaItem)
        if active_only:
            query = query.where(AssistantAgendaItem.status.in_(["open", "waiting"]))
        return [self.serialize_item(row) for row in self.db.scalars(query.order_by(
            AssistantAgendaItem.updated_at.desc(), AssistantAgendaItem.id.desc(),
        ))]

    def create_item(self, payload: AgendaCreate, *, session_id: int | None = None,
                    turn_id: int | None = None) -> dict:
        self.state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        values = payload.model_dump()
        recurrence = values.pop("recurrence")
        if recurrence:
            recurrence = {key: value for key, value in recurrence.items() if value is not None}
        values["recurrence_json"] = recurrence
        if recurrence:
            from app.services.assistant_recurrence import next_at

            values["recurrence_start_at"] = datetime.now(UTC)
            values["due_at"] = next_at(recurrence, values["recurrence_start_at"])
            if values["status"] != "open":
                values["due_at"] = None
        if (values["kind"] == "question" and values["question_category"] in {"important", "temporal"}
                and values["status"] == "open" and values["due_at"] is None):
            values["due_at"] = datetime.now(UTC)
        if values["status"] in {"done", "dismissed"}:
            values["due_at"] = None
        # A repeated tool call must not multiply an unresolved responsibility.
        query = select(AssistantAgendaItem).where(
            AssistantAgendaItem.kind == values["kind"], AssistantAgendaItem.title == values["title"],
            AssistantAgendaItem.notes == values["notes"],
            AssistantAgendaItem.completion_requirement == values["completion_requirement"],
            AssistantAgendaItem.status.in_(["open", "waiting"]),
        )
        if not recurrence:
            query = query.where(AssistantAgendaItem.due_at == values["due_at"])
        existing = next((item for item in self.db.scalars(query) if item.recurrence_json == recurrence), None)
        if values["kind"] == "question" and values["status"] in {"open", "waiting"}:
            title = self._question_title(values["title"])
            existing = next((item for item in self.db.scalars(select(AssistantAgendaItem).where(
                AssistantAgendaItem.kind == "question", AssistantAgendaItem.status.in_(("open", "waiting")),
            )) if self._question_title(item.title) == title), existing)
        if existing is not None:
            self.db.commit()
            return self.serialize_item(existing)
        # Deleted questions still have delivery receipts. Never reuse those IDs for a new item.
        from app.services.assistant_recurrence import next_item_id

        item_id = next_item_id(self.db)
        item = AssistantAgendaItem(id=item_id, **values, source_session_id=session_id, last_turn_id=turn_id)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return self.serialize_item(item)

    def update_item(self, item_id: int, payload: AgendaUpdate, *, turn_id: int | None = None) -> dict:
        self.state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        values = payload.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if not values:
            raise ValueError("No agenda changes supplied")
        if values.get("status") in {"done", "dismissed"}:
            values["due_at"] = None
        current = self.db.get(AssistantAgendaItem, item_id, populate_existing=True)
        if current is None or current.revision != payload.expected_revision:
            self.db.rollback()
            raise ValueError("Agenda item changed or no longer exists; read it again")
        recurrence = values.pop("recurrence", current.recurrence_json)
        if recurrence:
            recurrence = {key: value for key, value in recurrence.items() if value is not None}
        if not recurrence:
            if values.get("completion_requirement") is not None:
                raise ValueError("Completion requirements apply to recurring tasks")
            values["completion_requirement"] = None
        elif (current.completion_requirement == "interaction_today"
                and values.get("completion_requirement", current.completion_requirement) != current.completion_requirement):
            raise ValueError("Remove recurrence before clearing or changing its completion requirement")
        if recurrence and values.get("kind", current.kind) != "task":
            raise ValueError("Only tasks can recur")
        if (values.get("completion_requirement", current.completion_requirement) is not None
                and (values.get("kind", current.kind) != "task" or not recurrence)):
            raise ValueError("Completion requirements apply to recurring tasks")
        if recurrence and values.get("due_at") is not None:
            raise ValueError("Recurring task activation time is managed by its recurrence")
        if "recurrence" in payload.model_fields_set:
            values["recurrence_json"] = recurrence
            if recurrence != current.recurrence_json:
                values["schedule_version"] = current.schedule_version + 1
                values["recurrence_start_at"] = datetime.now(UTC) if recurrence else None
                values["due_at"] = None
        if recurrence:
            from app.services.assistant_recurrence import next_at

            values["due_at"] = (next_at(recurrence, datetime.now(UTC))
                                if values.get("status", current.status) == "open" else None)
        if (current and values.get("kind", current.kind) == "question"
                and values.get("question_category", current.question_category) in {"important", "temporal"}
                and values.get("status", current.status) == "open"
                and values.get("due_at", current.due_at) is None):
            values["due_at"] = datetime.now(UTC)
        if (current and values.get("kind", current.kind) == "question"
                and values.get("status", current.status) in {"open", "waiting"}):
            title = self._question_title(values.get("title", current.title))
            if any(self._question_title(item.title) == title for item in self.db.scalars(
                select(AssistantAgendaItem).where(AssistantAgendaItem.id != item_id,
                    AssistantAgendaItem.kind == "question", AssistantAgendaItem.status.in_(("open", "waiting"))),
            )):
                raise ValueError("An overlapping question already exists; merge it into that question")
        values.update(revision=AssistantAgendaItem.revision + 1, updated_at=datetime.now(UTC))
        if turn_id is not None:
            values["last_turn_id"] = turn_id
        claimed = self.db.execute(update(AssistantAgendaItem).where(
            AssistantAgendaItem.id == item_id, AssistantAgendaItem.revision == payload.expected_revision,
        ).values(**values))
        if not claimed.rowcount:
            self.db.rollback()
            raise ValueError("Agenda item changed or no longer exists; read it again")
        self.db.commit()
        return self.serialize_item(self.db.get(AssistantAgendaItem, item_id))

    def delete_item(self, item_id: int, expected_revision: int) -> None:
        from sqlalchemy import delete
        claimed = self.db.execute(delete(AssistantAgendaItem).where(
            AssistantAgendaItem.id == item_id, AssistantAgendaItem.revision == expected_revision,
        ))
        if not claimed.rowcount:
            self.db.rollback()
            raise ValueError("Agenda item changed or no longer exists; read it again")
        self.db.commit()

    def context(self, session_id: int) -> dict:
        from app.services.assistant_changes import AssistantChangeService
        from app.services.assistant_initialization import AssistantInitializationService
        from app.services.assistant_planning import daily_interaction_status
        from app.services.assistant_questions import AssistantQuestionService

        state = self.state()
        agenda = self.agenda(active_only=True)
        from app.services.agent_memory_service import AgentMemoryService

        memory = AgentMemoryService(self.db)
        proposals = list(self.db.scalars(select(AgentProposal).where(or_(
            AgentProposal.status == "pending",
            AgentProposal.execution_status.in_(("queued", "running")),
            and_(AgentProposal.execution_status.in_(("succeeded", "failed", "cancelled", "interrupted")),
                 AgentProposal.execution_state_json["assistant_outcome_turn_id"].as_integer().is_(None)),
        )).order_by(AgentProposal.id.desc()).limit(20)))
        agenda_context = []
        for item in agenda[:30]:
            entry = {key: item[key] for key in ("id", "revision", "kind", "title", "status", "due_at")}
            if item["recurrence"]:
                entry["recurrence"] = item["recurrence"]
                if item["completion_requirement"]:
                    entry["completion_requirement"] = item["completion_requirement"]
            else:
                entry["notes"] = item["notes"][:1500]
                if len(item["notes"]) > 1500:
                    entry["notes_truncated"] = True
            if item["kind"] == "question":
                entry["question_category"] = item["question_category"]
            agenda_context.append(entry)
        changes = AssistantChangeService(self.db).status()
        return {
            "now": datetime.now(UTC).isoformat(), "timezone": state.timezone,
            "change_tracking": {"pending_count": changes["pending_count"], "sources": [
                {key: source[key] for key in ("source", "succeeded_at", "error", "pending_count")}
                for source in changes["sources"]
            ]},
            "initialization": AssistantInitializationService(self.db).status(),
            "question_asking": AssistantQuestionService(self.db).status(state, datetime.now(UTC)),
            "daily_interaction": daily_interaction_status(self.db, state.timezone, datetime.now(UTC)),
            "agenda": agenda_context,
            "agenda_total": len(agenda),
            "memory": {
                "root": str(memory.memory_root),
                "facts_directory": "memory/facts-outside-atlas/",
                "daily_summaries": memory.list_daily_summaries(limit=3),
                "chat_history_directory": "memory/chat-history/",
                "agenda": "Use assistant.agenda.list; agenda is stored separately.",
            },
            "actions": [{"id": p.id, "description": p.title, "status": p.status,
                         "execution_status": p.execution_status} for p in proposals],
        }

    def remember(self, payload: dict) -> dict:
        from app.services.agent_memory_service import AgentMemoryService

        memory = AgentMemoryService(self.db)
        values = {
            "key": payload["key"], "value": payload["value"], "category": "facts",
            "provenance_json": {"attribution": payload["attribution"], "evidence": payload["evidence"],
                                "updated_at": datetime.now(UTC).isoformat()},
        }
        if payload.get("id") is not None:
            if "expected_value" not in payload:
                raise ValueError("Read the current fact before correcting it")
            return memory.update_fact(payload["id"], values, expected_value=payload["expected_value"])
        return memory.create_fact(values)

    def request_approval(self, payload: dict, session_id: int, turn_id: int | None = None) -> dict:
        from app.services.agent_proposal_service import AgentProposalService
        from app.services.assistant_actions import proposal_fingerprint

        action = {"type": "act", "description": payload["description"], "instruction": payload["instruction"]}
        provenance = {"kind": "assistant", "reason": payload["reason"],
                      "session_id": session_id, "turn_id": turn_id}
        fingerprint = proposal_fingerprint(action, provenance)
        proposal = self.db.scalar(select(AgentProposal).where(
            AgentProposal.source_session_id == session_id, AgentProposal.status == "pending",
            AgentProposal.action_json == action, AgentProposal.rationale == payload["reason"],
        ).limit(1)) or self.db.scalar(select(AgentProposal).where(AgentProposal.fingerprint == fingerprint))
        if proposal is None:
            proposal = AgentProposal(
                source_session_id=session_id, title=payload["description"], rationale=payload["reason"],
                instruction=payload["instruction"], actions=payload["description"], references_json=[],
                fingerprint=fingerprint, action_json=action, provenance_json=provenance,
            )
            self.db.add(proposal)
            self.db.commit()
            service = AgentProposalService(self.db)
            service.mirror(proposal)
            service._telegram(proposal, initial=True)
        return {"proposal_id": proposal.id, "status": proposal.status,
                "execution_status": proposal.execution_status}

    def decide_approval(self, payload: dict, session_id: int, turn_id: int | None) -> dict:
        from app.services.agent_proposal_service import AgentProposalService

        turn = self.db.get(ActTurn, turn_id) if turn_id is not None else None
        proposal = self.db.get(AgentProposal, payload["proposal_id"])
        if turn is None or turn.session_id != session_id or turn.status != "running":
            raise ValueError("Only the current user message can supply an approval decision")
        live_reply = self.db.scalar(select(ActLiveMessage).where(
            ActLiveMessage.turn_id == turn.id, ActLiveMessage.role == "user",
            ActLiveMessage.reply_to_message_id.is_not(None),
        ).order_by(ActLiveMessage.id.desc()).limit(1))
        if live_reply is not None:
            user_message, message_at = live_reply.text, live_reply.created_at
        elif not turn.assistant_context_json and not turn.backend_message_kind:
            user_message, message_at = turn.user_message, turn.created_at
        else:
            raise ValueError("Only the current user message can supply an approval decision")
        statement = payload["user_statement"].strip()
        if not statement or statement not in user_message:
            raise ValueError("Decision evidence must quote the current user's message")
        if proposal is None or proposal.source_session_id != session_id:
            raise ValueError("Action was not proposed in this conversation")
        if not proposal.action_json or proposal.action_json.get("type") != "act":
            raise ValueError("Editable function sequences require approval of their current review in the UI")
        if proposal.created_at.replace(tzinfo=UTC) > message_at.replace(tzinfo=UTC):
            raise ValueError("The action must have been presented before this user message")
        if proposal.status != "pending":
            raise ValueError("Action has already been decided")
        proposal.execution_state_json = {**(proposal.execution_state_json or {}),
            "user_decision": {"turn_id": turn.id, "statement": statement,
                              "live_message_id": live_reply.id if live_reply else None}}
        proposal = AgentProposalService(self.db).decide(proposal.id, payload["approve"])
        return {"proposal_id": proposal.id, "status": proposal.status, "execution_status": proposal.execution_status}

    @staticmethod
    def _question_title(title: str) -> str:
        return " ".join(title.split()).casefold().rstrip(" ?.! ")

    def serialize_item(self, item: AssistantAgendaItem) -> dict:
        values = {key: getattr(item, key) for key in (
            "id", "kind", "question_category", "title", "notes", "status", "due_at", "revision", "source_session_id",
            "last_turn_id", "created_at", "updated_at",
            "completion_requirement",
        )}
        for key in ("due_at", "created_at", "updated_at"):
            value = values[key]
            if value is not None:
                values[key] = value.replace(tzinfo=value.tzinfo or UTC).isoformat()
        from app.services.assistant_recurrence import serialize_occurrence

        values["recurrence"] = item.recurrence_json
        last = self.db.scalar(select(AssistantAgendaOccurrence).where(
            AssistantAgendaOccurrence.agenda_item_id == item.id,
        ).order_by(AssistantAgendaOccurrence.scheduled_at.desc(), AssistantAgendaOccurrence.id.desc()).limit(1))
        values["last_occurrence"] = serialize_occurrence(last) if last else None
        return values
