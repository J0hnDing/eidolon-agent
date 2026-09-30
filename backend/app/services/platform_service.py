from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.context_factory import InvocationContextFactory
from app.execution.executor import InvocationExecutor
from app.execution.types import InvocationExecutionError, InvocationTargetRef
from app.services.integration_service import IntegrationService

NOTION_DONE_ARCHIVE_SERVICE_ID = "backend.notion.todo.cleanup_done"
# Keep the existing identifier as a compatibility alias for persisted schedules and callers.
NOTION_DONE_CLEANUP_SERVICE_ID = NOTION_DONE_ARCHIVE_SERVICE_ID
QUERCUS_SYNC_SERVICE_ID = "backend.quercus.knowledge.sync"
ASSISTANT_ASSESSMENT_SERVICE_ID = "backend.assistant.assessment"
MAX_CLEANUP_PAGES = 100
PAGE_SIZE = 100
MAX_REPORTED_ITEMS = 100


class PlatformServiceError(ValueError):
    pass


@dataclass(frozen=True)
class PlatformScheduleDefinition:
    service_id: str
    schedule_id: int
    job_id: str
    name: str
    time: str
    timezone: str = "America/Toronto"


PLATFORM_SCHEDULES = (
    PlatformScheduleDefinition(
        service_id=NOTION_DONE_CLEANUP_SERVICE_ID,
        schedule_id=0,
        job_id="backend_notion_todo_cleanup_daily",
        name="Daily Notion Done Archive",
        time="03:00",
    ),
    PlatformScheduleDefinition(
        service_id=QUERCUS_SYNC_SERVICE_ID,
        schedule_id=-1,
        job_id="backend_quercus_knowledge_sync_daily",
        name="Daily Quercus Knowledge Sync",
        time="10:00",
    ),
)
PLATFORM_SCHEDULE_BY_ID = {definition.service_id: definition for definition in PLATFORM_SCHEDULES}


@dataclass
class NotionDoneArchiveService:
    executor: InvocationExecutor
    context: InvocationContext

    def _invoke(self, operation_id: str, input_json: dict[str, Any]) -> dict[str, Any]:
        outcome = self.executor.execute(
            InvocationTargetRef(category="integration", target_id=operation_id),
            input_json,
            self.context,
        )
        if outcome.output is None:
            raise InvocationExecutionError("internal_failure", "Integration returned no output")
        return outcome.output

    def run(self) -> dict[str, Any]:
        scanned_count = 0
        matched_count = 0
        archived_count = 0
        archived_ids: list[str] = []
        archived_ids_truncated = False
        failures: list[dict[str, str | None]] = []
        failures_truncated = False
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for _page_number in range(MAX_CLEANUP_PAGES):
            list_input: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor is not None:
                list_input["start_cursor"] = cursor
            try:
                page = self._invoke("notion.todo.list", list_input)
            except InvocationExecutionError as exc:
                return self._result(
                    status="partial" if scanned_count else "failed",
                    scanned_count=scanned_count,
                    matched_count=matched_count,
                    archived_count=archived_count,
                    archived_ids=archived_ids,
                    archived_ids_truncated=archived_ids_truncated,
                    failures=failures,
                    failures_truncated=failures_truncated,
                    error_type=exc.error_type,
                )

            todos = page["todos"]
            scanned_count += len(todos)
            for todo in todos:
                if todo["done"] is not True or todo.get("archived") is True:
                    continue
                matched_count += 1
                todo_id = str(todo["id"])
                try:
                    self._invoke("notion.todo.update", {"id": todo_id, "archived": True})
                except InvocationExecutionError as exc:
                    if len(failures) < MAX_REPORTED_ITEMS:
                        failures.append({"id": todo_id, "error_type": exc.error_type})
                    else:
                        failures_truncated = True
                    continue
                archived_count += 1
                if len(archived_ids) < MAX_REPORTED_ITEMS:
                    archived_ids.append(todo_id)
                else:
                    archived_ids_truncated = True

            if page["has_more"] is False:
                return self._result(
                    status="partial" if failures else "succeeded",
                    scanned_count=scanned_count,
                    matched_count=matched_count,
                    archived_count=archived_count,
                    archived_ids=archived_ids,
                    archived_ids_truncated=archived_ids_truncated,
                    failures=failures,
                    failures_truncated=failures_truncated,
                )

            next_cursor = page["next_cursor"]
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                return self._result(
                    status="partial" if scanned_count else "failed",
                    scanned_count=scanned_count,
                    matched_count=matched_count,
                    archived_count=archived_count,
                    archived_ids=archived_ids,
                    archived_ids_truncated=archived_ids_truncated,
                    failures=failures,
                    failures_truncated=failures_truncated,
                    error_type="invalid_pagination",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return self._result(
            status="partial",
            scanned_count=scanned_count,
            matched_count=matched_count,
            archived_count=archived_count,
            archived_ids=archived_ids,
            archived_ids_truncated=archived_ids_truncated,
            failures=failures,
            failures_truncated=failures_truncated,
            error_type="page_limit_reached",
        )

    @staticmethod
    def _result(
        *,
        status: str,
        scanned_count: int,
        matched_count: int,
        archived_count: int,
        archived_ids: list[str],
        archived_ids_truncated: bool,
        failures: list[dict[str, str | None]],
        failures_truncated: bool,
        error_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "scanned_count": scanned_count,
            "matched_count": matched_count,
            "archived_count": archived_count,
            "archived_ids": archived_ids,
            "archived_ids_truncated": archived_ids_truncated,
            "failures": failures,
            "failures_truncated": failures_truncated,
            "error_type": error_type,
        }


# The class name remains available for callers that imported the pre-archive cleanup service.
NotionDoneCleanupService = NotionDoneArchiveService


@dataclass
class PlatformServiceDispatcher:
    db: Session
    integrations: IntegrationService | None = None
    executor: InvocationExecutor | None = None

    def invoke(self, service_id: str) -> dict[str, Any]:
        if service_id == NOTION_DONE_CLEANUP_SERVICE_ID:
            executor = self.executor or InvocationExecutor(
                self.db,
                integration_service=self.integrations,
            )
            context = InvocationContextFactory.trusted_system(
                NOTION_DONE_CLEANUP_SERVICE_ID,
                origin="scheduler",
                initiating_action="Daily Notion Done Archive",
            )
            return NotionDoneArchiveService(executor, context).run()
        if service_id == QUERCUS_SYNC_SERVICE_ID:
            from app.services.quercus_service import QuercusSyncService

            return QuercusSyncService(self.db).run()
        raise PlatformServiceError("Unknown platform service")
