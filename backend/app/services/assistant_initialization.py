"""A finite Atlas reading pass. Persist references and receipts, never an Atlas mirror."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import ActSession, ActTurn, AssistantState
from app.services.assistant_atlas_reader import initial_tasks, read_task
from app.services.atlas_lifecycle_service import atlas_lifecycle_service
from app.services.integration_service import IntegrationError, build_default_integration_service

_SYNTHESIS = {"operation": "assistant.synthesize", "input": {}}


def _identity() -> str:
    return hashlib.sha256(str(atlas_lifecycle_service.directory).encode()).hexdigest()


def _key(task: dict) -> str:
    return json.dumps([task["operation"], task["input"]], sort_keys=True, separators=(",", ":"))


def _advance(pending: list, seen: set, step: dict) -> list:
    pending = pending[1:]
    if step["continuation"]:
        return [step["continuation"], *pending]
    children = []
    for task in step["next_tasks"]:
        if _key(task) not in seen:
            seen.add(_key(task))
            children.append(task)
    return pending[:-1] + children + pending[-1:] if pending else children


class AssistantInitializationService:
    def __init__(self, db: Session):
        self.db = db

    def _state(self) -> AssistantState:
        from app.services.assistant_service import AssistantService

        return AssistantService(self.db).state()

    def status(self) -> dict:
        progress = self._state().initialization_json
        if progress.get("source") != _identity():
            return {"status": "pending", "reviewed_batches": 0, "remaining": None, "error": None}
        return {"status": "complete" if not progress["pending"] else "blocked" if progress.get("error") else "reading",
                "reviewed_batches": progress["batches"], "remaining": len(progress["pending"]),
                "error": progress.get("error")}

    def ready(self, now: datetime) -> bool:
        progress = self._state().initialization_json
        if progress.get("source") == _identity():
            if not progress["pending"]:
                return False
            if progress.get("retry_at") and datetime.fromisoformat(progress["retry_at"]) > now:
                return False
        return build_default_integration_service(self.db).provider_connected("atlas")

    def read(self, session_id: int, turn_id: int) -> dict:
        session, turn = self.db.get(ActSession, session_id), self.db.get(ActTurn, turn_id)
        if (session is None or session.agent_id != "assistant" or session.status != "active"
                or turn is None or turn.session_id != session_id or turn.status != "running"
                or turn.cancel_requested_at is not None):
            raise ValueError("Initialization requires the current running Assistant turn")
        state = self._state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        self.db.refresh(state)
        source = _identity()
        progress = state.initialization_json
        if progress.get("source") != source:
            at = datetime.now(UTC).isoformat()
            tasks = initial_tasks(at) + [_SYNTHESIS]
            progress = {"source": source, "at": at, "pending": tasks,
                        "seen": [_key(task) for task in tasks], "batches": 0}
            state.initialization_json = progress
        if not progress["pending"]:
            self.db.commit()
            return {"phase": "complete", "status": self.status()}
        task, batch = progress["pending"][0], progress["batches"]
        self.db.commit()  # Full Atlas reads happen outside the state write lock.
        pending, seen = list(progress["pending"]), set(progress["seen"])
        items, steps, size, image = [], [], 0, None
        try:
            for _ in range(8):
                task = pending[0]
                if items and task == _SYNTHESIS:
                    break
                result = ({"data": {"phase": "synthesis"}, "next_tasks": [], "continuation": None}
                          if task == _SYNTHESIS else read_task(self.db, task, session_id=session_id,
                                                             turn_id=turn_id, at=progress["at"]))
                length = len(json.dumps(result["data"], ensure_ascii=True))
                if items and (size + length > 48000 or "_initialization_image" in result):
                    break  # Leave this reference for the next batch; no body is persisted.
                items.append({"reference": task, "data": result["data"]})
                steps.append({"task": task, "next_tasks": result["next_tasks"],
                              "continuation": result["continuation"]})
                pending = _advance(pending, seen, result)
                size += length
                image = result.get("_initialization_image")
                if image or task == _SYNTHESIS or size >= 48000:
                    break
        except IntegrationError as exc:
            self.db.rollback()
            self.db.refresh(state)
            current = state.initialization_json
            if current.get("source") == source and current.get("batches") == batch:
                pending = list(current["pending"])
                if exc.error_type == "stale_revision":
                    # Restart this enumeration/document, without losing other completed reads.
                    head = {"operation": task["operation"], "input": dict(task["input"])}
                    for name in ("cursor", "offset", "sha256"):
                        head["input"].pop(name, None)
                    # Earlier items in this uncommitted batch must also be reread.
                    pending = [head if entry == task else entry for entry in pending]
                state.initialization_json = {**current, "pending": pending, "error": exc.error_type,
                    "retry_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat()}
                self.db.commit()
            raise ValueError(f"Atlas initialization could not read the next item: {exc.error_type}") from None
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        self.db.refresh(state)
        self.db.refresh(turn)
        if (_identity() != source or state.initialization_json.get("source") != source
                or state.initialization_json.get("batches") != batch or turn.status != "running"
                or turn.cancel_requested_at is not None):
            self.db.rollback()
            raise ValueError("Initialization changed while reading; continue from current state")
        digest = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        previous = (turn.assistant_review_json or {}).get("initialization", {})
        marker = {"source": source, "batch": batch, "steps": steps, "digest": digest,
                  "reviewed": bool(previous.get("digest") == digest and previous.get("reviewed"))}
        turn.assistant_review_json = {**(turn.assistant_review_json or {}), "initialization": marker}
        state.initialization_json = {**state.initialization_json, "error": None, "retry_at": None}
        self.db.commit()
        output = {"phase": "synthesis" if items[0]["reference"] == _SYNTHESIS else "reading",
                  "items": items, "status": self.status()}
        if image:
            output["_initialization_image"] = image
        return output

    def acknowledge(self, turn_id: int) -> dict:
        """Record review of this turn's delivered batch, without copying Atlas facts."""
        turn = self.db.get(ActTurn, turn_id)
        session = self.db.get(ActSession, turn.session_id) if turn else None
        if (session is None or session.agent_id != "assistant" or session.status != "active"
                or turn.status != "running" or turn.cancel_requested_at is not None):
            raise ValueError("Initialization acknowledgement requires the current running Assistant turn")
        state = self._state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        self.db.refresh(state)
        self.db.refresh(turn)
        marker = (turn.assistant_review_json or {}).get("initialization")
        progress = state.initialization_json
        if (turn.status != "running" or turn.cancel_requested_at is not None
                or not marker or not marker.get("steps") or not marker.get("digest")
                or not progress or not progress.get("pending")
                or marker.get("source") != _identity() or marker.get("source") != progress.get("source")
                or marker.get("batch") != progress.get("batches")):
            self.db.rollback()
            raise ValueError("Read the current Atlas initialization batch before acknowledging its review")
        turn.assistant_review_json = {**(turn.assistant_review_json or {}),
                                      "initialization": {**marker, "reviewed": True}}
        self.db.commit()
        return {"acknowledged": True, "batch": marker["batch"], "status": self.status()}

    def finish(self, turn: ActTurn) -> None:
        marker = (turn.assistant_review_json or {}).get("initialization")
        if not marker and not (turn.assistant_context_json or {}).get("initialize_atlas"):
            return
        state = self._state()
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        self.db.refresh(state)
        progress = state.initialization_json
        if not progress or progress.get("source") != _identity() or not progress["pending"]:
            return
        if marker and (marker["source"] != progress["source"] or marker["batch"] != progress["batches"]):
            return  # Idempotent completion/recovery, or a different Atlas instance.
        if (turn.status != "succeeded" or turn.cancel_requested_at is not None
                or not marker or not marker.get("reviewed")):
            state.initialization_json = {**progress,
                "error": progress.get("error") or "Read the Atlas batch and acknowledge its review before finishing",
                "retry_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat()}
            return
        pending, seen = list(progress["pending"]), set(progress["seen"])
        for step in marker["steps"]:
            if not pending or pending[0] != step["task"]:
                raise ValueError("Initialization batch no longer matches its source queue")
            pending = _advance(pending, seen, step)
        state.initialization_json = {**progress, "pending": pending, "seen": sorted(seen),
                                    "batches": progress["batches"] + 1, "error": None, "retry_at": None}
