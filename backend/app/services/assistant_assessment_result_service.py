"""Backend ownership of a structured Assistant assessment result."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationTargetRef
from app.models import ActSession, ActTurn, AgentProposal
from app.schemas.assistant_assessment import AssistantAssessmentResult
from app.services.agent_proposal_service import AgentProposalService
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.function_catalog_service import FunctionCatalogService


def deterministic_catalog(db: Session) -> dict[str, dict]:
    return {
        entry["id"]: entry
        for entry in FunctionCatalogService(db).list_entries(refresh=True)
        if entry.get("availability") == "available"
        and entry.get("mcp_exposed") is True
        and entry.get("agent_selectable", True) is not False
        and entry.get("requires_invocation_approval") is not True
        and entry.get("uses_codex") is not True
        and entry.get("category") in {"integration", "user", "backend_core"}
        and isinstance(entry.get("input_schema"), dict)
    }


def validate_action(action: dict, catalog: dict[str, dict], follow_up: dict | None = None) -> None:
    if action["type"] == "act":
        if follow_up and follow_up["timing"]["type"] == "after_calendar_step":
            raise ValueError("Calendar step follow-up requires a function action")
        return
    for step in action["steps"]:
        entry = catalog.get(step["function"])
        if entry is None:
            raise ValueError(f"Function {step['function']} is unavailable for deterministic execution")
        try:
            Draft202012Validator(entry["input_schema"]).validate(step["arguments"])
        except ValidationError as exc:
            raise ValueError(f"Invalid arguments for {step['function']}: {exc.message[:200]}") from None
        if _has_placeholder(step["arguments"]):
            raise ValueError("Function arguments must be completely resolved")
    if follow_up and follow_up["timing"]["type"] == "after_calendar_step":
        index = follow_up["timing"]["step_index"]
        if index >= len(action["steps"]):
            raise ValueError("Follow-up calendar step is outside the action")
        step = action["steps"][index]
        if step["function"] != "google_calendar.event.create":
            raise ValueError("Follow-up calendar step must create an event")
        end = step["arguments"].get("end", {}).get("date_time")
        if not isinstance(end, str) or datetime.fromisoformat(end.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Follow-up calendar step requires a timed event end")


def _has_placeholder(value: object) -> bool:
    if isinstance(value, dict):
        return any(_has_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    return isinstance(value, str) and any(token in value.casefold() for token in (
        "{{", "}}", "<todo", "<goal", "<event", "tbd", "to be determined", "placeholder",
    ))


def proposal_fingerprint(action: dict, provenance: dict) -> str:
    return hashlib.sha256(json.dumps(
        {"action": action, "provenance": provenance},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


class AssistantAssessmentResultService:
    def __init__(self, db: Session):
        self.db = db

    def accept(self, turn: ActTurn, result: AssistantAssessmentResult) -> None:
        session = self.db.get(ActSession, turn.session_id)
        if session is None or session.agent_id != "assistant" or session.origin != "assessment":
            raise ValueError("Structured assessment belongs to an Assistant assessment session")
        catalog = deterministic_catalog(self.db)
        specs: list[tuple[dict, dict, dict | None]] = []
        for item in result.todo_doer.proposals:
            specs.append((item.action.model_dump(), {"kind": "todo", "todo_id": item.todo_id}, None))
        for item in result.goal_doer.proposals:
            specs.append((item.action.model_dump(), {
                "kind": "goal", "target": item.target.model_dump(), "reason": item.reason,
            }, item.follow_up.model_dump(mode="json") if item.follow_up else None))
        for opportunity in result.opportunity_scout.opportunities:
            for item in opportunity.proposals:
                specs.append((item.action.model_dump(), {
                    "kind": "opportunity", "title": opportunity.title,
                }, item.follow_up.model_dump(mode="json") if item.follow_up else None))
        for action, _provenance, follow_up in specs:
            validate_action(action, catalog, follow_up)
        self._write_report(turn, result)
        proposals = []
        seen_fingerprints: set[str] = set()
        for action, provenance, follow_up in specs:
            fingerprint = proposal_fingerprint(action, provenance)
            if (fingerprint in seen_fingerprints
                    or self.db.scalar(select(AgentProposal.id).where(AgentProposal.fingerprint == fingerprint)) is not None):
                continue
            seen_fingerprints.add(fingerprint)
            references = []
            if provenance["kind"] == "todo":
                references = [f"todo:{provenance['todo_id']}"]
            elif provenance["kind"] == "goal":
                target = provenance["target"]
                references = [f"{target['type']}:{target['id']}"]
            proposal = AgentProposal(
                source_session_id=turn.session_id,
                title=action["description"], rationale=provenance.get("reason", ""),
                instruction=action.get("instruction", ""), actions=action["description"],
                references_json=references, fingerprint=fingerprint,
                action_json=action, provenance_json=provenance, follow_up_json=follow_up,
            )
            try:
                with self.db.begin_nested():
                    self.db.add(proposal)
                    self.db.flush()
            except IntegrityError:
                continue
            proposals.append(proposal)
        session.proposal_count += len(proposals)
        for item in result.goal_doer.questions:
            target = item.target.model_dump()
            name = resolve_target_name(self.db, target)
            label = f"Goal: {name}" if name != "Goal" else "Goal"
            message = ActTurn(
                session_id=turn.session_id, user_message="", assistant_message=f"{label}\n{item.question}",
                backend_message_kind="goal_question", status="succeeded", activity_json=[],
                created_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            )
            self.db.add(message)
        self.db.commit()
        service = AgentProposalService(self.db)
        for proposal in proposals:
            service.mirror(proposal)
            service._telegram(proposal, initial=True)

    def _write_report(self, turn: ActTurn, result: AssistantAssessmentResult) -> None:
        if turn.assessment_report_id:
            return
        children = [{
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [
                {"type": "text", "text": {"content": chunk}}
                for chunk in _chunks(f"{item.title} — {item.description}", 1900)
            ]},
        } for item in result.opportunity_scout.opportunities]
        context = InvocationContextFactory.trusted_system(
            "assistant_opportunity_report", initiating_action=f"assistant_assessment_turn_{turn.id}",
        )
        outcome = InvocationExecutor(self.db).execute(
            InvocationTargetRef(category="integration", target_id="notion.report.create"),
            {"name": f"Opportunity Scout — {datetime.now(UTC).date().isoformat()}",
             "select": "Opportunities", "children": children}, context,
        )
        if outcome.status != "succeeded" or not isinstance(outcome.output, dict):
            raise RuntimeError("Opportunity Scout report could not be written")
        turn.assessment_report_id = str(outcome.output["id"])
        self.db.commit()


def _chunks(text: str, size: int) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)]


def resolve_target_name(db: Session, target: dict) -> str:
    kind = target.get("type")
    record_id = target.get("id")
    fallback = "Todo" if kind == "todo" else "Goal"
    if not isinstance(record_id, str) or not record_id:
        return fallback
    try:
        if kind == "todo":
            executor = InvocationExecutor(db)
            context = InvocationContextFactory.trusted_system("assistant_reference_display")
            arguments = {"page_size": 100}
            for _ in range(100):
                outcome = executor.execute(
                    InvocationTargetRef(category="integration", target_id="notion.todo.list"),
                    arguments, context,
                )
                page = outcome.output or {}
                for todo in page.get("todos", []):
                    if todo.get("id") == record_id:
                        name = todo.get("title")
                        return name.strip() if isinstance(name, str) and name.strip() else fallback
                if not page.get("has_more") or not page.get("next_cursor"):
                    break
                arguments["start_cursor"] = page["next_cursor"]
            return fallback
        provider = UrllibAtlasProviderAdapter()
        records = provider._record_list(provider._request(
            "/api/records?category=goal", None, timeout=10,
            max_bytes=2_000_000, method="GET",
        ), "goal")
        for record in records:
            if record.get("id") == record_id:
                name = record.get("title")
                return name.strip() if isinstance(name, str) and name.strip() else fallback
        return fallback
    except Exception:
        return fallback
