"""Shared, user-editable workspace memory and bounded complete-message archives.

SQLite retains active conversation text and runtime metadata. Completed chat
content lives only in these files. The small private cursor file prevents
deleted or expired archive messages from being restored at synchronization.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import ActLiveMessage, ActSession, ActTurn, AssistantState, MemoryFact
from app.services.act_workspace_service import (
    ActWorkspace,
    ActWorkspaceError,
    agent_memory_lock,
    atomic_workspace_write,
    checked_workspace_path,
    ensure_act_workspace,
    ensure_agent_memory_layout,
)

# These bounds apply across all three agents; retained text is never shortened.
CHAT_HISTORY_RETENTION_DAYS = 30
CHAT_HISTORY_MAX_BYTES = 50 * 1024 * 1024
MAX_FACT_FILE_BYTES = 1024 * 1024
MAX_FACTS = 10000
MAX_DAILY_SUMMARY_CHARACTERS = 1200
MAX_METADATA_BYTES = 1024 * 1024
ARCHIVE_BATCH_SIZE = 250
MAX_PENDING_TURNS = 10000
MAX_MEMORY_PAGE_SIZE = 100
_AGENTS = ("assistant", "act", "observer")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ARCHIVE_NAME = re.compile(r"\d{8}T\d{12}Z-(?:turn|live)-[1-9]\d*-(?:user|assistant)\.json\Z")
_SUMMARY_RECEIPT = re.compile(r"<!-- eidolon-daily-summary (\{[^\n]*\}) -->")
logger = logging.getLogger(__name__)


def sync_chat_history_after_commit(db: Session) -> None:
    """An archive I/O failure must not undo or suppress an accepted chat.

    No cursor advances for an unfinished batch; the next chat/startup sync
    retries the durable database messages.
    """
    try:
        AgentMemoryService(db).sync_chat_history()
    except Exception:
        logger.exception("Chat archive synchronization failed; the next synchronization will retry")


def _timestamp(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


class AgentMemoryService:
    def __init__(self, db: Session | None = None, workspace: ActWorkspace | None = None):
        self.db = db
        self.workspace = workspace or ensure_act_workspace()
        self.root = self.workspace.root.resolve()
        self.memory_root = checked_workspace_path(self.root, self.root / "memory")
        if self.workspace.memory.absolute() != self.memory_root:
            raise ActWorkspaceError("Memory must use the shared workspace memory folder")
        ensure_agent_memory_layout(self.root)
        self.facts = self.memory_root / "facts-outside-atlas"
        self.summaries = self.memory_root / "daily-summaries"
        self.history = self.memory_root / "chat-history"
        self.metadata = self.memory_root / ".agent-memory-state.json"

    def _read(self, path: Path, maximum: int) -> str:
        checked_workspace_path(self.root, path)
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError(f"Memory file exceeds its supported size: {path.name}")
        return raw.decode("utf-8")

    def _write_json(self, path: Path, payload: dict) -> None:
        atomic_workspace_write(self.root, path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _state(self) -> dict:
        checked_workspace_path(self.root, self.metadata)
        if not self.metadata.exists():
            return {}
        payload = json.loads(self._read(self.metadata, MAX_METADATA_BYTES))
        if not isinstance(payload, dict):
            raise ValueError("Invalid agent memory bookkeeping")
        return payload

    def _save_state(self, payload: dict) -> None:
        if len(json.dumps(payload).encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValueError("Agent memory bookkeeping exceeds its supported size")
        self._write_json(self.metadata, payload)

    def _fact_path(self, item_id: int) -> Path:
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
            raise ValueError("Memory ID must be a positive integer")
        return checked_workspace_path(self.root, self.facts / f"{item_id}.json")

    def _list_facts(self) -> list[dict]:
        checked_workspace_path(self.root, self.facts)
        result = []
        for path in self.facts.glob("*.json"):
            checked_workspace_path(self.root, path)
            if not path.stem.isdecimal():
                continue
            if len(result) >= MAX_FACTS:
                raise ValueError("Too many memory facts; inspect the memory folder")
            payload = json.loads(self._read(path, MAX_FACT_FILE_BYTES))
            fact = self._normalize_fact(payload)
            if fact["id"] != int(path.stem):
                raise ValueError(f"Memory ID does not match its filename: {path.name}")
            result.append(fact)
        return sorted(result, key=lambda fact: (fact["updated_at"], fact["id"]), reverse=True)

    @staticmethod
    def _normalize_fact(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Memory fact must be a JSON object")
        item_id = payload.get("id")
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
            raise ValueError("Memory fact must have a positive integer ID")
        key, value = payload.get("key"), payload.get("value")
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise ValueError("Memory key must contain 1 to 128 characters")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Memory value must not be empty")
        sensitivity = payload.get("sensitivity", "normal")
        if not isinstance(sensitivity, str) or not 1 <= len(sensitivity) <= 32:
            raise ValueError("Memory sensitivity must contain 1 to 32 characters")
        if not isinstance(payload.get("user_editable", True), bool):
            raise ValueError("Memory user_editable must be a boolean")
        now = datetime.now(UTC).isoformat()
        provenance = payload.get("provenance_json", payload.get("provenance"))
        if provenance is not None and not isinstance(provenance, dict):
            raise ValueError("Memory provenance must be a JSON object")
        fact = {**payload, "id": item_id, "key": key, "value": value, "category": "facts",
                "source_message_id": payload.get("source_message_id"),
                "sensitivity": sensitivity,
                "user_editable": payload.get("user_editable", True),
                "provenance_json": provenance,
                "created_at": _timestamp(payload.get("created_at")) or now,
                "updated_at": _timestamp(payload.get("updated_at")) or now,
                "expires_at": _timestamp(payload.get("expires_at"))}
        original_category = payload.get("category")
        if original_category and original_category != "facts":
            fact.setdefault("legacy_category", original_category)
        fact.pop("provenance", None)
        if len(json.dumps(fact, ensure_ascii=False).encode("utf-8")) > MAX_FACT_FILE_BYTES:
            raise ValueError("Memory fact exceeds its supported size")
        return fact

    def list_facts(self) -> list[dict]:
        with agent_memory_lock(self.root):
            return self._list_facts()

    def get_fact(self, item_id: int) -> dict:
        with agent_memory_lock(self.root):
            path = self._fact_path(item_id)
            if not path.exists():
                raise LookupError("Memory fact not found")
            fact = self._normalize_fact(json.loads(self._read(path, MAX_FACT_FILE_BYTES)))
            if fact["id"] != item_id:
                raise ValueError("Memory ID does not match its filename")
            return fact

    def create_fact(self, payload: dict) -> dict:
        with agent_memory_lock(self.root):
            existing = self._list_facts()
            if len(existing) >= MAX_FACTS:
                raise ValueError("Too many memory facts")
            if any(fact["key"] == payload.get("key") for fact in existing):
                raise ValueError("Memory key already exists; read and correct that entry")
            state = self._state()
            item_id = max(state.get("next_fact_id", 1), max((fact["id"] for fact in existing), default=0) + 1)
            fact = self._normalize_fact({**payload, "id": item_id})
            self._write_json(self._fact_path(item_id), fact)
            state["next_fact_id"] = item_id + 1
            self._save_state(state)
            return fact

    def update_fact(self, item_id: int, payload: dict, expected_value: str | None = None) -> dict:
        with agent_memory_lock(self.root):
            path = self._fact_path(item_id)
            if not path.exists():
                raise LookupError("Memory fact not found")
            current = self._normalize_fact(json.loads(self._read(path, MAX_FACT_FILE_BYTES)))
            if current["id"] != item_id:
                raise ValueError("Memory ID does not match its filename")
            if not current["user_editable"]:
                raise ValueError("Memory is not editable")
            if expected_value is not None and current["value"] != expected_value:
                raise ValueError("Memory changed; read the current value before correcting it")
            if any(fact["id"] != item_id and fact["key"] == payload.get("key", current["key"])
                   for fact in self._list_facts()):
                raise ValueError("Memory key already exists; read and correct that entry")
            fact = self._normalize_fact({**current, **payload, "id": item_id,
                                         "created_at": current["created_at"],
                                         "updated_at": datetime.now(UTC).isoformat()})
            self._write_json(path, fact)
            return fact

    def delete_fact(self, item_id: int) -> None:
        with agent_memory_lock(self.root):
            path = self._fact_path(item_id)
            if not path.exists():
                raise LookupError("Memory fact not found")
            path.unlink()

    def _summary_path(self, day: str) -> Path:
        self._validate_day(day)
        return checked_workspace_path(self.root, self.summaries / f"{day}.md")

    @staticmethod
    def _validate_day(day: str) -> str:
        if not isinstance(day, str) or not _DAY.fullmatch(day):
            raise ValueError("Supply a memory date as YYYY-MM-DD")
        try:
            date.fromisoformat(day)
        except ValueError as error:
            raise ValueError("Supply a valid memory date as YYYY-MM-DD") from error
        return day

    @staticmethod
    def _validate_page(offset: int, limit: int) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Memory page offset must be a non-negative integer")
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_MEMORY_PAGE_SIZE):
            raise ValueError(f"Memory page limit must be from 1 to {MAX_MEMORY_PAGE_SIZE}")

    def _get_daily_summary(self, day: str) -> dict | None:
        path = self._summary_path(day)
        if not path.exists():
            return None
        content = self._read(path, MAX_FACT_FILE_BYTES)
        match = _SUMMARY_RECEIPT.search(content)
        metadata = json.loads(match[1]) if match else {}
        summary = _SUMMARY_RECEIPT.sub("", content).removeprefix(f"# {day}").strip()
        return {"day": day, "summary": summary, "path": str(path),
                "source_turn_id": metadata.get("source_turn_id")}

    def get_daily_summary(self, day: str) -> dict | None:
        with agent_memory_lock(self.root):
            return self._get_daily_summary(day)

    def write_daily_summary(self, day: str, summary: str, source_turn_id: int | None = None) -> dict:
        path = self._summary_path(day)
        if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_DAILY_SUMMARY_CHARACTERS:
            raise ValueError(f"Daily summary must contain 1 to {MAX_DAILY_SUMMARY_CHARACTERS} characters; "
                             "explicitly state when nothing important happened")
        with agent_memory_lock(self.root):
            existing = self._get_daily_summary(day)
            if existing and source_turn_id is not None and existing["source_turn_id"] == source_turn_id:
                return existing
            receipt = json.dumps({"source_turn_id": source_turn_id})
            atomic_workspace_write(self.root, path,
                                   f"# {day}\n\n<!-- eidolon-daily-summary {receipt} -->\n\n{summary.strip()}\n")
            return self._get_daily_summary(day)

    def list_daily_summaries(self, limit: int = 7) -> list[dict]:
        with agent_memory_lock(self.root):
            days = self._daily_summary_days()
            return [self._get_daily_summary(day) for day in days[:max(0, min(limit, 365))]]

    def _daily_summary_days(self) -> list[str]:
        checked_workspace_path(self.root, self.summaries)
        days = []
        for path in self.summaries.glob("*.md"):
            checked_workspace_path(self.root, path)
            if not path.is_file() or not _DAY.fullmatch(path.stem):
                continue
            try:
                date.fromisoformat(path.stem)
            except ValueError:
                continue
            days.append(path.stem)
        return sorted(days, reverse=True)

    def list_daily_summaries_page(self, offset: int = 0, limit: int = 30) -> dict:
        self._validate_page(offset, limit)
        with agent_memory_lock(self.root):
            days = self._daily_summary_days()
            items = [self._get_daily_summary(day) for day in days[offset:offset + limit]]
            return {"items": items, "total": len(days), "offset": offset, "limit": limit}

    def migrate_legacy(self, db: Session | None = None) -> None:
        db = db if db is not None else self.db
        if db is None:
            raise ValueError("Legacy migration requires a database session")
        with agent_memory_lock(self.root):
            state = self._state()
            highwater = state.get("legacy_fact_highwater", 0)
            rows = list(db.scalars(select(MemoryFact).order_by(MemoryFact.id)))
            for row in rows:
                if row.id <= highwater:
                    # A durable receipt precedes SQL deletion, so a crash or a
                    # later deliberate file deletion must not recreate this fact.
                    db.delete(row)
                    continue
                path = self._fact_path(row.id)
                if not path.exists():
                    payload = {column.name: getattr(row, column.name) for column in MemoryFact.__table__.columns}
                    self._write_json(path, self._normalize_fact(payload))
                fact = self._normalize_fact(json.loads(self._read(path, MAX_FACT_FILE_BYTES)))
                if fact["id"] != row.id:
                    raise ValueError("Legacy memory conflicts with an existing memory file")
                # Preserve existing user edits rather than overwriting on retry.
                highwater = row.id
                state["legacy_fact_highwater"] = highwater
                state["next_fact_id"] = max(state.get("next_fact_id", 1), highwater + 1)
                self._save_state(state)
                db.delete(row)
            assistant = db.get(AssistantState, 1)
            if assistant is not None and assistant.summary:
                path = checked_workspace_path(self.root, self.summaries / "legacy-continuity.md")
                if not state.get("legacy_continuity_migrated"):
                    if not path.exists():
                        atomic_workspace_write(self.root, path, "# Legacy continuity note\n\n"
                                               "Preserved from the former continuity store; this is not a daily reflection.\n\n"
                                               + assistant.summary + "\n")
                    self._read(path, MAX_FACT_FILE_BYTES)
                    state["legacy_continuity_migrated"] = True
                    self._save_state(state)
                assistant.summary = ""
            db.commit()

    def _archive_message(self, agent: str, source: str, source_id: int, role: str, text: str,
                         timestamp: datetime, session_id: int, turn_id: int, cutoff: datetime) -> None:
        normalized = _timestamp(timestamp)
        if datetime.fromisoformat(normalized) < cutoff or not text:
            return
        payload = {"agent": agent, "role": role, "text": text, "timestamp": normalized,
                   "session_id": session_id, "turn_id": turn_id, "source": source, "source_id": source_id}
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        # A single message larger than the entire budget cannot be retained;
        # discard it as a whole, never retain a shortened transcript.
        if len(encoded.encode("utf-8")) > CHAT_HISTORY_MAX_BYTES:
            return
        stamp = datetime.fromisoformat(normalized).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.history / agent / f"{stamp}-{source}-{source_id}-{role}.json"
        checked_workspace_path(self.root, path)
        if not path.exists():
            atomic_workspace_write(self.root, path, encoded)
        # Verify the durable file before clearing its database source. Content
        # edits belong to the user; identity changes cannot acknowledge a row.
        self._read_archive_message(agent, source, source_id, role, timestamp, session_id, turn_id)

    def _read_archive_message(self, agent: str, source: str, source_id: int, role: str,
                              timestamp: datetime, session_id: int, turn_id: int) -> str | None:
        normalized = _timestamp(timestamp)
        stamp = datetime.fromisoformat(normalized).strftime("%Y%m%dT%H%M%S%fZ")
        path = checked_workspace_path(self.root, self.history / agent / f"{stamp}-{source}-{source_id}-{role}.json")
        if not path.exists():
            return None
        payload = json.loads(self._read(path, CHAT_HISTORY_MAX_BYTES))
        identity = {"agent": agent, "role": role, "timestamp": normalized,
                    "session_id": session_id, "turn_id": turn_id, "source": source, "source_id": source_id}
        if (not isinstance(payload, dict) or any(payload.get(key) != value for key, value in identity.items())
                or not isinstance(payload.get("text"), str)):
            raise ValueError(f"Chat archive identity does not match its source: {path.name}")
        return payload["text"]

    def read_turn_content(self, turn: ActTurn, agent_id: str | None = None) -> dict:
        """Project canonical content without hydrating or modifying ORM rows.

        Missing retained files mean deliberately discarded content. In
        particular, never consult a database copy after the archive marker.
        """
        if self.db is None:
            raise ValueError("Reading turn content requires a database session")
        live = list(self.db.scalars(select(ActLiveMessage).where(ActLiveMessage.turn_id == turn.id)))
        if turn.history_archived_at is None:
            return {"user_message": turn.user_message, "assistant_message": turn.assistant_message,
                    "live_messages": {message.id: message.text for message in live}, "available": True}
        if agent_id is None:
            agent_id = self.db.scalar(select(ActSession.agent_id).where(ActSession.id == turn.session_id))
        if agent_id not in _AGENTS:
            raise ValueError("Unknown chat history agent")
        with agent_memory_lock(self.root):
            user = self._read_archive_message(agent_id, "turn", turn.id, "user", turn.created_at,
                                              turn.session_id, turn.id)
            assistant = self._read_archive_message(agent_id, "turn", turn.id, "assistant",
                                                   turn.completed_at or turn.created_at, turn.session_id, turn.id)
            messages = {}
            for message in live:
                if message.role not in ("user", "assistant"):
                    continue
                text = self._read_archive_message(agent_id, "live", message.id, message.role,
                                                  message.created_at, turn.session_id, turn.id)
                if text is not None:
                    messages[message.id] = text
            return {"user_message": user or "", "assistant_message": assistant,
                    "live_messages": messages, "available": user is not None or assistant is not None or bool(messages)}

    def sync_chat_history(self, db: Session | None = None, now: datetime | None = None) -> None:
        db = db if db is not None else self.db
        if db is None:
            raise ValueError("Chat history synchronization requires a database session")
        now = datetime.fromisoformat(_timestamp(now or datetime.now(UTC)))
        cutoff = now - timedelta(days=CHAT_HISTORY_RETENTION_DAYS)
        with agent_memory_lock(self.root):
            state = self._state()
            cursor = state.setdefault("chat_history", {"turn_id": 0, "live_id": 0, "pending_turn_ids": []})
            pending = set(cursor.get("pending_turn_ids", []))
            pending_keys = cursor.setdefault("pending_turn_keys", {})
            exported_terminal = set()
            # Pending turns are revisited until their final answer exists. Other
            # receipts use (creation time, ID), because deleting a Telegram topic
            # can delete the newest rows and SQLite can reuse those integer IDs.
            for turn_id in sorted(pending):
                row = db.execute(select(ActTurn, ActSession.agent_id).join(
                    ActSession, ActSession.id == ActTurn.session_id).where(ActTurn.id == turn_id)
                    .execution_options(populate_existing=True)).first()
                if row is None:
                    pending.discard(turn_id)
                    pending_keys.pop(str(turn_id), None)
                elif (str(turn_id) in pending_keys
                      and pending_keys[str(turn_id)] != self._turn_key(row[0])):
                    # A deleted pending row's integer ID may now belong to a
                    # completely different turn, including after clock rollback.
                    pending.discard(turn_id)
                    pending_keys.pop(str(turn_id), None)
                elif row[0].status not in ("queued", "running"):
                    turn, agent = row
                    if agent in _AGENTS and not self._is_reflection(turn):
                        self._archive_message(agent, "turn", turn.id, "assistant", turn.assistant_message or "",
                                              turn.completed_at or turn.created_at, turn.session_id, turn.id, cutoff)
                        self._archive_terminal_live(db, turn, agent, cutoff, cursor, force=False)
                    pending.discard(turn_id)
                    pending_keys.pop(str(turn_id), None)
                    exported_terminal.add(self._turn_key(turn))
                else:
                    pending_keys[str(turn_id)] = self._turn_key(row[0])
            while True:
                rows = db.execute(select(ActTurn, ActSession.agent_id).join(
                    ActSession, ActSession.id == ActTurn.session_id).where(
                    self._after_cursor(ActTurn, cursor, "turn")).order_by(
                    ActTurn.created_at, ActTurn.id).limit(ARCHIVE_BATCH_SIZE)
                    .execution_options(populate_existing=True)).all()
                if not rows:
                    break
                for turn, agent in rows:
                    if agent in _AGENTS:
                        if (turn.backend_message_kind in (None, "assistant_refresh")
                                and turn.assistant_context_json is None):
                            self._archive_message(agent, "turn", turn.id, "user", turn.user_message,
                                                  turn.created_at, turn.session_id, turn.id, cutoff)
                        if turn.status in ("queued", "running"):
                            pending.add(turn.id)
                            pending_keys[str(turn.id)] = self._turn_key(turn)
                            if len(pending) > MAX_PENDING_TURNS:
                                raise ValueError("Too many unfinished chat turns to archive safely")
                        elif not self._is_reflection(turn):
                            self._archive_message(agent, "turn", turn.id, "assistant", turn.assistant_message or "",
                                                  turn.completed_at or turn.created_at, turn.session_id, turn.id, cutoff)
                            if self._turn_key(turn) not in exported_terminal:
                                self._archive_terminal_live(db, turn, agent, cutoff, cursor, force=True)
                        if turn.status not in ("queued", "running"):
                            exported_terminal.add(self._turn_key(turn))
                    self._advance_cursor(cursor, "turn", turn.id, turn.created_at)
                cursor["pending_turn_ids"] = sorted(pending)
                cursor["cleanup_turn_keys"] = sorted(set(cursor.get("cleanup_turn_keys", [])) | exported_terminal)
                self._save_state(state)
            while True:
                rows = db.execute(select(ActLiveMessage, ActTurn, ActSession.agent_id).join(
                    ActTurn, ActTurn.id == ActLiveMessage.turn_id).join(
                    ActSession, ActSession.id == ActTurn.session_id).where(
                    self._after_cursor(ActLiveMessage, cursor, "live")).order_by(
                    ActLiveMessage.created_at, ActLiveMessage.id).limit(ARCHIVE_BATCH_SIZE)
                    .execution_options(populate_existing=True)).all()
                if not rows:
                    break
                for message, turn, agent in rows:
                    acknowledged = self._turn_key(turn) in set(cursor.get("cleanup_turn_keys", [])) | exported_terminal
                    if (agent in _AGENTS and message.role in ("user", "assistant") and not self._is_reflection(turn)
                            and turn.history_archived_at is None and not acknowledged):
                        self._archive_message(agent, "live", message.id, message.role, message.text,
                                              message.created_at, turn.session_id, message.turn_id, cutoff)
                    self._advance_cursor(cursor, "live", message.id, message.created_at)
                cursor["cleanup_turn_keys"] = sorted(set(cursor.get("cleanup_turn_keys", [])) | exported_terminal)
                self._save_state(state)
            cursor["pending_turn_ids"] = sorted(pending)
            cursor["cleanup_turn_keys"] = sorted(set(cursor.get("cleanup_turn_keys", [])) | exported_terminal)
            self._save_state(state)
            self._prune_history(cutoff)
            # Durable cursor receipts precede SQL erasure. If SQL commit is
            # interrupted, retry clears these rows without recreating files a
            # user or retention already removed. Runtime metadata remains.
            stale_live = select(ActLiveMessage.turn_id).where(ActLiveMessage.text != "")
            completed_query = select(ActTurn).join(ActSession).where(
                ActSession.agent_id.in_(_AGENTS), ActTurn.status.not_in(("queued", "running")),
                or_(ActTurn.history_archived_at.is_(None), ActTurn.user_message != "",
                    ActTurn.assistant_message.is_not(None), ActTurn.id.in_(stale_live))).order_by(ActTurn.id)
            # Polling may have loaded this turn while it was still running.
            # Refresh again here: completion can happen after the cursor scan.
            # Never erase a newly completed answer using an older ORM snapshot.
            while completed := list(db.scalars(completed_query.limit(ARCHIVE_BATCH_SIZE)
                                               .execution_options(populate_existing=True))):
                receipts = set(cursor.get("cleanup_turn_keys", []))
                for turn in completed:
                    agent = db.scalar(select(ActSession.agent_id).where(ActSession.id == turn.session_id))
                    key = self._turn_key(turn)
                    if ((cursor.get("completed_content_migrated") or pending_keys.get(str(turn.id)) == key)
                            and turn.history_archived_at is None
                            and key not in receipts and key not in exported_terminal):
                        # Every new unmarked terminal row requires its own
                        # receipt. Global timestamp/ID bounds alone cannot
                        # identify ID reuse combined with clock rollback.
                        if not self._is_reflection(turn):
                            if (turn.backend_message_kind in (None, "assistant_refresh")
                                    and turn.assistant_context_json is None):
                                self._archive_message(agent, "turn", turn.id, "user", turn.user_message,
                                                      turn.created_at, turn.session_id, turn.id, cutoff)
                            self._archive_message(agent, "turn", turn.id, "assistant", turn.assistant_message or "",
                                                  turn.completed_at or turn.created_at, turn.session_id, turn.id, cutoff)
                            self._archive_terminal_live(db, turn, agent, cutoff, cursor, force=True)
                    receipts.add(key)
                # Receipt capacity and file failures stop before any SQL text
                # is erased. Receipts survive interrupted SQL commits.
                cursor["cleanup_turn_keys"] = sorted(receipts)
                self._save_state(state)
                self._prune_history(cutoff)
                for turn in completed:
                    self._clear_completed_turn(db, turn, now)
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                receipts.difference_update(self._turn_key(turn) for turn in completed)
                cursor["cleanup_turn_keys"] = sorted(receipts)
                self._save_state(state)
            # Existing versions' cursors acknowledge the legacy rows exactly
            # once. Thereafter all new completed rows need a specific receipt.
            cursor["completed_content_migrated"] = True
            cursor["cleanup_turn_keys"] = []
            self._save_state(state)

    def _clear_completed_turn(self, db: Session, turn: ActTurn, now: datetime) -> None:
        # Older installations already have durable cursor receipts. Verify
        # retained files; absent files remain deliberately discarded.
        agent = db.scalar(select(ActSession.agent_id).where(ActSession.id == turn.session_id))
        if turn.history_archived_at is None and not self._is_reflection(turn):
            self._read_archive_message(agent, "turn", turn.id, "user", turn.created_at,
                                       turn.session_id, turn.id)
            self._read_archive_message(agent, "turn", turn.id, "assistant",
                                       turn.completed_at or turn.created_at, turn.session_id, turn.id)
            for message in db.scalars(select(ActLiveMessage).where(ActLiveMessage.turn_id == turn.id)):
                if message.role in ("user", "assistant"):
                    self._read_archive_message(agent, "live", message.id, message.role, message.created_at,
                                               turn.session_id, turn.id)
        if turn.history_archived_at is None:
            turn.assistant_review_json = {**(turn.assistant_review_json or {}), "chat_history": {
                "has_assistant_response": bool((turn.assistant_message or "").strip()),
            }}
        turn.user_message = ""
        turn.assistant_message = None
        turn.history_archived_at = turn.history_archived_at or now
        for message in db.scalars(select(ActLiveMessage).where(ActLiveMessage.turn_id == turn.id)):
            message.text = ""

    def _archive_terminal_live(self, db: Session, turn: ActTurn, agent: str, cutoff: datetime,
                               cursor: dict, *, force: bool) -> None:
        for message in db.scalars(select(ActLiveMessage).where(ActLiveMessage.turn_id == turn.id)):
            stamp = _timestamp(message.created_at)
            previous = cursor.get("live_created_at")
            after = (message.id > cursor.get("live_highwater_id", cursor["live_id"])
                     or previous is not None and (stamp, message.id) > (previous, cursor["live_id"]))
            if message.role in ("user", "assistant") and (force or after):
                self._archive_message(agent, "live", message.id, message.role, message.text,
                                      message.created_at, turn.session_id, turn.id, cutoff)

    @staticmethod
    def _turn_key(turn: ActTurn) -> str:
        return f"{turn.session_id}:{turn.id}:{_timestamp(turn.created_at)}"

    @staticmethod
    def _is_reflection(turn: ActTurn) -> bool:
        activation = turn.assistant_context_json or {}
        return activation.get("kind") == "reflection" or activation.get("reflection") is True

    @staticmethod
    def _after_cursor(model, cursor: dict, prefix: str):
        timestamp = cursor.get(f"{prefix}_created_at")
        highwater = cursor.get(f"{prefix}_highwater_id", cursor[f"{prefix}_id"])
        if not timestamp:
            return model.id > highwater
        created = datetime.fromisoformat(timestamp)
        return or_(model.created_at > created,
                   and_(model.created_at == created, model.id > cursor[f"{prefix}_id"]),
                   model.id > highwater)

    @staticmethod
    def _advance_cursor(cursor: dict, prefix: str, row_id: int, created_at: datetime) -> None:
        """Keep independent time and ID bounds so clock rollback cannot skip text."""
        cursor[f"{prefix}_highwater_id"] = max(row_id, cursor.get(f"{prefix}_highwater_id", cursor[f"{prefix}_id"]))
        previous = cursor.get(f"{prefix}_created_at")
        stamp = _timestamp(created_at)
        if previous is None or (stamp, row_id) > (previous, cursor[f"{prefix}_id"]):
            cursor[f"{prefix}_id"] = row_id
            cursor[f"{prefix}_created_at"] = stamp

    def _history_paths(self) -> list[Path]:
        paths = []
        for agent in _AGENTS:
            directory = checked_workspace_path(self.root, self.history / agent)
            for path in directory.glob("*.json"):
                checked_workspace_path(self.root, path)
                if _ARCHIVE_NAME.fullmatch(path.name):
                    paths.append(path)
        return sorted(paths, key=lambda path: path.name)

    @staticmethod
    def _archive_filename_day(path: Path) -> str | None:
        stamp = path.name.split("-", 1)[0]
        try:
            return datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").date().isoformat()
        except ValueError:
            return None

    def _history_days(self, agent: str | None = None) -> list[str]:
        days = {
            day
            for path in self._history_paths()
            if (agent is None or path.parent.name == agent)
            and path.is_file()
            and (day := self._archive_filename_day(path)) is not None
        }
        return sorted(days, reverse=True)

    def list_chat_history_days_page(self, offset: int = 0, limit: int = 30) -> dict:
        self._validate_page(offset, limit)
        with agent_memory_lock(self.root):
            days = self._history_days()
            return {"items": days[offset:offset + limit], "total": len(days), "offset": offset, "limit": limit}

    def _read_history_message(self, path: Path) -> tuple[dict, str]:
        checked_workspace_path(self.root, path)
        payload = json.loads(self._read(path, CHAT_HISTORY_MAX_BYTES))
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid chat archive message: {path.name}")
        agent = path.parent.name
        if agent not in _AGENTS or payload.get("agent") != agent:
            raise ValueError(f"Chat archive agent does not match its folder: {path.name}")
        if payload.get("role") not in ("user", "assistant") or not isinstance(payload.get("text"), str):
            raise ValueError(f"Invalid chat archive content: {path.name}")
        if payload.get("source") not in ("turn", "live"):
            raise ValueError(f"Invalid chat archive source: {path.name}")
        for field in ("session_id", "turn_id", "source_id"):
            value = payload.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Invalid chat archive identity: {path.name}")
        timestamp = payload.get("timestamp")
        if not isinstance(timestamp, str):
            raise ValueError(f"Invalid chat archive timestamp: {path.name}")
        try:
            normalized_timestamp = _timestamp(timestamp)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid chat archive timestamp: {path.name}") from error
        normalized = {
            "agent": agent,
            "role": payload["role"],
            "text": payload["text"],
            "timestamp": normalized_timestamp,
            "session_id": payload["session_id"],
            "turn_id": payload["turn_id"],
            "source": payload["source"],
            "source_id": payload["source_id"],
        }
        return normalized, normalized_timestamp

    def read_chat_history_page(
        self,
        day: str,
        agent: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        self._validate_day(day)
        self._validate_page(offset, limit)
        if agent is not None and agent not in _AGENTS:
            raise ValueError("Agent must be assistant, act, or observer")
        with agent_memory_lock(self.root):
            paths = [
                path for path in self._history_paths()
                if (agent is None or path.parent.name == agent)
                and path.is_file()
                and self._archive_filename_day(path) == day
            ]
            paths.sort(key=lambda path: (path.name, path.parent.name))
            items = []
            total = 0
            for path in paths:
                message, timestamp = self._read_history_message(path)
                if datetime.fromisoformat(timestamp).date().isoformat() != day:
                    continue
                if offset <= total < offset + limit:
                    items.append(message)
                total += 1
            return {"items": items, "total": total, "offset": offset, "limit": limit}

    def _prune_history(self, cutoff: datetime) -> None:
        retained = []
        cutoff_name = cutoff.strftime("%Y%m%dT%H%M%S%fZ")
        total = 0
        for path in self._history_paths():
            # Filenames are fixed UTC timestamps, so retention does not trust
            # editable message text or require loading the archive into RAM.
            if path.name.split("-", 1)[0] < cutoff_name:
                path.unlink()
            else:
                size = path.stat().st_size
                retained.append((path, size))
                total += size
        for path, size in retained:
            if total <= CHAT_HISTORY_MAX_BYTES:
                break
            path.unlink()
            total -= size

    def read_chat_history(self, day: str, timezone: str, max_characters: int = 60000) -> dict:
        self._summary_path(day)
        zone = ZoneInfo(timezone)
        maximum = max(0, min(max_characters, 200000))
        with agent_memory_lock(self.root):
            messages, paths = [], []
            characters = 0
            total_messages = 0
            for path in self._history_paths():
                stamp = datetime.strptime(path.name.split("-", 1)[0], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
                if stamp.astimezone(zone).date().isoformat() != day:
                    continue
                # Retained files are individually at most the entire budget.
                payload = json.loads(self._read(path, CHAT_HISTORY_MAX_BYTES))
                timestamp = datetime.fromisoformat(_timestamp(payload.get("timestamp")))
                if timestamp.astimezone(zone).date().isoformat() != day:
                    continue
                total_messages += 1
                if len(paths) < 128:
                    paths.append(str(path))
                size = len(json.dumps(payload, ensure_ascii=False))
                if characters + size <= maximum:
                    messages.append(payload)
                    characters += size
            return {"day": day, "timezone": timezone, "messages": messages, "paths": paths,
                    "total_messages": total_messages, "truncated": len(messages) < total_messages,
                    "paths_truncated": len(paths) < total_messages,
                    "archive_directories": [str(self.history / agent) for agent in _AGENTS]}
