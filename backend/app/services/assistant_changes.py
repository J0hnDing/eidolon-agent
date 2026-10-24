"""Backend-owned source comparison and durable, turn-scoped review receipts."""

from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.models import ActSession, ActTurn, AssistantChange, AssistantSourceState, AssistantState


class AssistantChangeService:
    def __init__(self, db: Session):
        self.db = db

    def record_snapshots(self, snapshots: dict) -> None:
        """Commit only complete source reads. Failed reads cannot change the baseline."""
        now = datetime.now(UTC)
        # Source network reads finish before taking this write lock.
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        for source, result in snapshots.items():
            state = self.db.get(AssistantSourceState, source)
            if state is None:
                state = AssistantSourceState(source=source, scope="", snapshot_json={})
                self.db.add(state)
            state.checked_at = now
            if not result.get("complete"):
                state.error = (result.get("error") or "Source review was incomplete")[:512]
                continue
            items, scope = result["items"], result["scope"]
            old = state.snapshot_json
            baseline = state.succeeded_at is None
            same_scope = state.scope == scope
            for item_id, item in items.items():
                if item_id in old and old[item_id] == item:
                    continue
                kind = "updated" if item_id in old else (
                    "baseline" if baseline else "added" if same_scope else "entered_scope"
                )
                self.db.add(AssistantChange(source=source, item_id=item_id, kind=kind,
                    before_json=old.get(item_id), after_json=item, detected_at=now))
            if same_scope and not baseline:
                for item_id in old.keys() - items.keys():
                    self.db.add(AssistantChange(source=source, item_id=item_id, kind="removed_from_scope",
                        before_json=old[item_id], after_json=None, detected_at=now))
            state.scope, state.snapshot_json = scope, items
            state.succeeded_at, state.error = now, None
        self.db.commit()

    def status(self) -> dict:
        counts = dict(self.db.execute(select(AssistantChange.source, func.count()).where(
            AssistantChange.reviewed_at.is_(None),
        ).group_by(AssistantChange.source)).all())
        return {
            "pending_count": sum(counts.values()),
            "sources": [{"source": s.source, "checked_at": s.checked_at,
                         "succeeded_at": s.succeeded_at, "error": s.error,
                         "pending_count": counts.get(s.source, 0)}
                        for s in self.db.scalars(select(AssistantSourceState).order_by(AssistantSourceState.source))],
        }

    def prepare(self, turn: ActTurn) -> None:
        """Collect once per turn, including an empty/failed collection, across recovery."""
        session = self.db.get(ActSession, turn.session_id)
        if session is None or session.agent_id != "assistant" or turn.status != "running":
            raise ValueError("Change review requires the current running Assistant turn")
        if "sources" in (turn.assistant_review_json or {}):
            return
        from app.services.assistant_sources import collect_sources

        snapshots = collect_sources(self.db, session_id=turn.session_id)
        self.record_snapshots(snapshots)
        turn.assistant_review_json = {
            **(turn.assistant_review_json or {}),
            "prepared_at": datetime.now(UTC).isoformat(),
            "sources": [name for name, result in snapshots.items() if result.get("complete")],
            "unavailable_sources": {name: result.get("error") or "Incomplete source read"
                                    for name, result in snapshots.items() if not result.get("complete")},
        }
        self.db.commit()

    def read(self, turn_id: int, *, offset: int = 0, limit: int = 30) -> dict:
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid change review page")
        turn = self.db.get(ActTurn, turn_id)
        if turn is None:
            raise ValueError("Assistant review turn not found")
        self.prepare(turn)
        from app.services.assistant_sources import available_sources

        prepared_sources = set(turn.assistant_review_json["sources"])
        sources = prepared_sources & available_sources(self.db, session_id=turn.session_id) if prepared_sources else set()
        self.reconcile()
        # Serialize page claims so two Assistant conversations cannot consume the
        # same changes. Earlier failed turns remain eligible for a later review.
        self.db.execute(update(AssistantState).where(AssistantState.id == 1).values(id=1))
        terminal = select(ActTurn.id).where(ActTurn.status.not_in(("running", "queued")))
        eligible = (
            AssistantChange.reviewed_at.is_(None),
            AssistantChange.source.in_(sources),
            or_(AssistantChange.review_turn_id.is_(None), AssistantChange.review_turn_id == turn.id,
                AssistantChange.review_turn_id.in_(terminal)),
        )
        total = self.db.scalar(select(func.count()).select_from(AssistantChange).where(*eligible))
        rows = list(self.db.scalars(select(AssistantChange).where(*eligible).order_by(
            AssistantChange.id,
        ).offset(offset).limit(limit)))
        for row in rows:
            row.review_turn_id = turn.id
        self.db.commit()
        return {
            "items": [{"id": row.id, "source": row.source, "item_id": row.item_id, "kind": row.kind,
                       "before": row.before_json, "after": row.after_json,
                       "detected_at": row.detected_at.isoformat()} for row in rows],
            "total": total, "next_offset": offset + limit if offset + limit < total else None,
            "unavailable_sources": turn.assistant_review_json["unavailable_sources"],
        }

    def finish(self, turn: ActTurn) -> None:
        """Called in the same transaction that publishes successful turn status."""
        if turn.status == "succeeded":
            self.db.execute(update(AssistantChange).where(
                AssistantChange.review_turn_id == turn.id, AssistantChange.reviewed_at.is_(None),
            ).values(reviewed_at=turn.completed_at or datetime.now(UTC)))

    def reconcile(self) -> None:
        # Covers a terminal success saved by older/recovery paths before its receipt.
        self.db.execute(update(AssistantChange).where(
            AssistantChange.reviewed_at.is_(None),
            AssistantChange.review_turn_id.in_(select(ActTurn.id).where(ActTurn.status == "succeeded")),
        ).values(reviewed_at=datetime.now(UTC)))
