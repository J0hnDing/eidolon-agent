from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    IntegrationConnection,
    QuercusCourse,
    QuercusCourseExclusion,
    QuercusSyncResource,
)
from app.schemas.integration import QuercusConnectionStatus, QuercusCourseRead
from app.services.act_workspace_service import ACT_ROOT, ensure_act_workspace
from app.services.quercus_processing_service import QuercusProcessingService
from app.services.quercus_provider import QuercusProvider, QuercusProviderError
from app.services.quercus_runtime import QUERCUS_WORK_LOCK
from app.services.secret_store import SecretStore, SecretStoreError, default_secret_store

QUERCUS_PROVIDER = "quercus"
QUERCUS_SECRET_NAMESPACE = "quercus"
MAX_TOKEN_LENGTH = 8192
MAX_AUTOMATIC_FILE_BYTES = 8 * 1024**3
MAX_STREAM_CHUNK_BYTES = 1024 * 1024
_SYNC_LOCK = QUERCUS_WORK_LOCK
_FILE_LINK_RE = re.compile(
    r"(?P<url>(?:https://q\.utoronto\.ca)?/(?:api/v1/)?"
    r"(?:courses/[A-Za-z0-9_-]+/)?files/(?P<id>[A-Za-z0-9_-]+)"
    r"(?:/(?:download|preview))?(?:\?[^\"'<>\s]*)?)",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class QuercusError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class _SafeHtml(HTMLParser):
    blocked = {"script", "style", "form", "iframe", "object", "embed", "button", "textarea", "select", "option"}
    blocked_void = {"meta", "link", "input"}
    allowed_schemes = {"http", "https", "mailto"}

    def __init__(self, rewrite_url: Callable[[str], str | None] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0
        self.rewrite_url = rewrite_url

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self.blocked_void:
            return
        if tag in self.blocked:
            self.depth += 1
            return
        if self.depth:
            return
        safe_attrs: list[str] = []
        for name, value in attrs:
            name = name.casefold()
            if name.startswith("on") or name in {"style", "srcdoc"} or value is None:
                continue
            if name in {"href", "src"}:
                scheme = value.split(":", 1)[0].casefold() if ":" in value else ""
                if scheme and scheme not in self.allowed_schemes:
                    continue
                if self.rewrite_url is not None:
                    value = self.rewrite_url(value)
                    if value is None:
                        continue
            if name not in {"href", "src", "alt", "title", "colspan", "rowspan"}:
                continue
            safe_attrs.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self.blocked | self.blocked_void:
            return
        self.handle_starttag(tag, attrs)
        if not self.depth:
            self.parts[-1] = self.parts[-1][:-1] + " />"

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.blocked_void:
            return
        if tag in self.blocked:
            self.depth = max(0, self.depth - 1)
            return
        if not self.depth:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(html.escape(data))

    def result(self) -> str:
        return "".join(self.parts).strip()


def sanitize_html(value: Any, rewrite_url: Callable[[str], str | None] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _SafeHtml(rewrite_url)
    parser.feed(value)
    parser.close()
    return parser.result()


@dataclass
class QuercusService:
    db: Session
    secret_store: SecretStore | None = None
    provider_factory: Callable[[str], QuercusProvider] = QuercusProvider
    queue_sync: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if self.secret_store is None:
            try:
                self.secret_store = default_secret_store()
            except SecretStoreError:
                self.secret_store = None

    def status(self) -> QuercusConnectionStatus:
        connection = self._connection()
        courses = self._stored_courses()
        if connection is None:
            return QuercusConnectionStatus(connected=False, status="disconnected", courses=courses)
        available = self.secret_store is not None and connection.secret_store_id == self.secret_store.implementation_id
        status = connection.status if available else "unavailable"
        return QuercusConnectionStatus(
            connected=available and status == "connected",
            status=status,
            account_name=connection.account_login,
            account_id=connection.account_id,
            last_validated_at=connection.last_validated_at,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            error_type=connection.error_type if status != "connected" else None,
            courses=courses,
        )

    def put_connection(self, token: str) -> QuercusConnectionStatus:
        if not token.strip() or len(token) > MAX_TOKEN_LENGTH:
            raise QuercusError("invalid_input", "Quercus token must be a non-empty bounded string")
        if self.secret_store is None:
            raise QuercusError("connection_unavailable", "Operating-system secret storage is unavailable")
        try:
            profile = self.provider_factory(token).profile()
        except QuercusProviderError as exc:
            raise QuercusError(exc.error_type, str(exc)) from None
        account_id = str(profile["id"])
        account_name = str(profile.get("name") or profile.get("short_name") or "Quercus user")[:128]
        existing = self._connection()
        retained = self.db.scalar(select(QuercusCourse.course_id).limit(1))
        if existing is not None and existing.account_id != account_id and retained is not None:
            raise QuercusError(
                "identity_conflict",
                "Remove all retained Quercus course copies before connecting a different account",
            )
        try:
            reference = self.secret_store.put(token, namespace=QUERCUS_SECRET_NAMESPACE)
        except SecretStoreError:
            raise QuercusError("connection_unavailable", "Operating-system secret storage is unavailable") from None
        finally:
            token = ""
        now = utc_now()
        old_reference = existing.secret_reference if existing is not None else None
        if existing is None:
            existing = IntegrationConnection(
                provider=QUERCUS_PROVIDER,
                secret_store_id=self.secret_store.implementation_id,
                secret_reference=reference,
                credential_kind="token",
                status="connected",
                account_login=account_name,
                account_id=account_id,
                last_validated_at=now,
            )
            self.db.add(existing)
        else:
            existing.secret_store_id = self.secret_store.implementation_id
            existing.secret_reference = reference
            existing.status = "connected"
            existing.account_login = account_name
            existing.account_id = account_id
            existing.error_type = None
            existing.last_validated_at = now
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.secret_store.delete(reference, namespace=QUERCUS_SECRET_NAMESPACE)
            raise QuercusError("internal_failure", "Quercus connection could not be saved safely") from None
        if old_reference and old_reference != reference:
            try:
                self.secret_store.delete(old_reference, namespace=QUERCUS_SECRET_NAMESPACE)
            except SecretStoreError:
                pass
        return self.status()

    def remove_connection(self) -> QuercusConnectionStatus:
        connection = self._connection()
        if connection is None:
            return self.status()
        if self.secret_store is not None and self.secret_store.implementation_id == connection.secret_store_id:
            try:
                self.secret_store.delete(connection.secret_reference, namespace=QUERCUS_SECRET_NAMESPACE)
            except SecretStoreError:
                raise QuercusError("connection_unavailable", "Quercus credential could not be removed") from None
        self.db.delete(connection)
        self.db.commit()
        return self.status()

    def courses(self) -> list[QuercusCourseRead]:
        provider, connection = self._provider()
        try:
            remote = provider.courses()
        except QuercusProviderError as exc:
            self._mark_connection_error(connection, exc.error_type)
            raise QuercusError(exc.error_type, str(exc)) from None
        excluded = self._excluded_course_ids(connection.account_id)
        stored = {
            course.course_id: course
            for course in self.db.scalars(select(QuercusCourse)).all()
            if course.course_id not in excluded
        }
        result: list[QuercusCourseRead] = []
        remote_ids: set[str] = set()
        for item in remote:
            course_id = str(item["id"])
            if course_id in excluded:
                continue
            remote_ids.add(course_id)
            row = stored.get(course_id)
            result.append(self._course_read(item, row))
        for course_id, row in stored.items():
            if course_id not in remote_ids:
                result.append(self._stored_course_read(row))
        return self._sort_courses(result)

    def select_courses(self, course_ids: list[str]) -> list[QuercusCourseRead]:
        normalized = list(dict.fromkeys(str(item).strip() for item in course_ids if str(item).strip()))
        if len(normalized) != len(course_ids):
            raise QuercusError("invalid_input", "Course IDs must be unique, non-empty strings")
        provider, connection = self._provider()
        try:
            excluded = self._excluded_course_ids(connection.account_id)
            available = {
                str(item["id"]): item
                for item in provider.courses()
                if str(item["id"]) not in excluded
            }
        except QuercusProviderError as exc:
            self._mark_connection_error(connection, exc.error_type)
            raise QuercusError(exc.error_type, str(exc)) from None
        unknown = sorted(set(normalized) - set(available))
        if unknown:
            raise QuercusError("not_found", "One or more selected courses are not accessible")
        existing = {row.course_id: row for row in self.db.scalars(select(QuercusCourse)).all()}
        for row in existing.values():
            row.selected = row.course_id in normalized
        for course_id in normalized:
            item = available[course_id]
            row = existing.get(course_id)
            if row is None:
                row = QuercusCourse(
                    course_id=course_id,
                    account_id=connection.account_id,
                    name=self._course_name(item),
                    course_code=self._optional_text(item.get("course_code"), 256),
                    term_name=self._term_name(item),
                    enrollment_state=self._optional_text(item.get("_enrollment_state"), 32),
                    selected=True,
                    local_path=self._unique_course_path(item),
                )
                self.db.add(row)
                existing[course_id] = row
            else:
                row.account_id = connection.account_id
                row.name = self._course_name(item)
                row.course_code = self._optional_text(item.get("course_code"), 256)
                row.term_name = self._term_name(item)
                row.enrollment_state = self._optional_text(item.get("_enrollment_state"), 32)
                row.selected = True
        self.db.commit()
        if self.queue_sync is not None:
            self.queue_sync()
        result = [self._course_read(item, existing.get(course_id)) for course_id, item in available.items()]
        for course_id, row in existing.items():
            if course_id not in available:
                result.append(self._stored_course_read(row))
        return self._sort_courses(result)

    def delete_course(self, course_id: str) -> None:
        if not _SYNC_LOCK.acquire(blocking=False):
            raise QuercusError(
                "operation_in_progress",
                "Quercus synchronization or processing is in progress; try deleting the course again shortly",
            )
        try:
            row = self.db.get(QuercusCourse, course_id)
            connection = self._connection()
            account_id = row.account_id if row is not None else connection.account_id if connection else None
            if account_id is None:
                raise QuercusError("not_found", "Quercus course was not found")
            if row is not None:
                root = (ACT_ROOT / "knowledge" / "quercus").resolve()
                target = (root / row.local_path).resolve()
                if target.parent != root:
                    raise QuercusError("internal_failure", "Stored Quercus course path is invalid")
                if target.exists():
                    shutil.rmtree(target)
                self.db.execute(
                    delete(QuercusSyncResource).where(QuercusSyncResource.course_id == course_id)
                )
                self.db.delete(row)
            if self.db.get(QuercusCourseExclusion, (account_id, course_id)) is None:
                self.db.add(QuercusCourseExclusion(account_id=account_id, course_id=course_id))
            self.db.commit()
        finally:
            _SYNC_LOCK.release()

    def _provider(self) -> tuple[QuercusProvider, IntegrationConnection]:
        connection = self._connection()
        if connection is None or connection.status != "connected" or self.secret_store is None:
            raise QuercusError("connection_unavailable", "Quercus connection is unavailable")
        if connection.secret_store_id != self.secret_store.implementation_id:
            raise QuercusError("connection_unavailable", "Quercus credential store is unavailable")
        try:
            token = self.secret_store.get(connection.secret_reference, namespace=QUERCUS_SECRET_NAMESPACE)
        except SecretStoreError:
            raise QuercusError("connection_unavailable", "Stored Quercus credential is unavailable") from None
        return self.provider_factory(token), connection

    def _connection(self) -> IntegrationConnection | None:
        return self.db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == QUERCUS_PROVIDER))

    def _excluded_course_ids(self, account_id: str) -> set[str]:
        return set(
            self.db.scalars(
                select(QuercusCourseExclusion.course_id).where(
                    QuercusCourseExclusion.account_id == account_id
                )
            ).all()
        )

    def _mark_connection_error(self, connection: IntegrationConnection, error_type: str) -> None:
        connection.status = "invalid" if error_type == "invalid_credential" else "connected"
        connection.error_type = error_type
        self.db.commit()

    def _stored_courses(self) -> list[QuercusCourseRead]:
        return [self._stored_course_read(row) for row in self.db.scalars(select(QuercusCourse)).all()]

    @classmethod
    def _stored_course_read(cls, row: QuercusCourse) -> QuercusCourseRead:
        return QuercusCourseRead(
            course_id=row.course_id,
            name=row.name,
            course_code=row.course_code,
            term_name=row.term_name,
            enrollment_state=row.enrollment_state,
            selected=row.selected,
            retained=not row.selected,
            local_path=f"knowledge/quercus/{row.local_path}",
            last_sync_started_at=row.last_sync_started_at,
            last_sync_completed_at=row.last_sync_completed_at,
            last_sync_status=row.last_sync_status,
            last_error_type=row.last_error_type,
            skipped_file_count=row.skipped_file_count,
            last_processing_started_at=row.last_processing_started_at,
            last_processing_completed_at=row.last_processing_completed_at,
            last_processing_status=row.last_processing_status,
            last_processing_error_type=row.last_processing_error_type,
            processed_file_count=row.processed_file_count,
            failed_processing_count=row.failed_processing_count,
        )

    @classmethod
    def _course_read(cls, item: dict[str, Any], row: QuercusCourse | None) -> QuercusCourseRead:
        if row is not None:
            result = cls._stored_course_read(row)
            return result.model_copy(
                update={
                    "name": cls._course_name(item),
                    "course_code": cls._optional_text(item.get("course_code"), 256),
                    "term_name": cls._term_name(item),
                    "enrollment_state": cls._optional_text(item.get("_enrollment_state"), 32),
                }
            )
        return QuercusCourseRead(
            course_id=str(item["id"]),
            name=cls._course_name(item),
            course_code=cls._optional_text(item.get("course_code"), 256),
            term_name=cls._term_name(item),
            enrollment_state=cls._optional_text(item.get("_enrollment_state"), 32),
        )

    def _unique_course_path(self, item: dict[str, Any]) -> str:
        base = _slug(" - ".join(part for part in (str(item.get("course_code") or ""), self._course_name(item)) if part))
        used = {row.local_path.casefold() for row in self.db.scalars(select(QuercusCourse)).all()}
        return _unique_name(base, used)

    @staticmethod
    def _course_name(item: dict[str, Any]) -> str:
        return str(item.get("name") or item.get("course_code") or "Untitled course")[:512]

    @staticmethod
    def _term_name(item: dict[str, Any]) -> str | None:
        term = item.get("term")
        return QuercusService._optional_text(term.get("name") if isinstance(term, dict) else None, 256)

    @staticmethod
    def _optional_text(value: Any, limit: int) -> str | None:
        return str(value)[:limit] if value not in {None, ""} else None

    @staticmethod
    def _sort_courses(courses: list[QuercusCourseRead]) -> list[QuercusCourseRead]:
        order = {"active": 0, "invited_or_pending": 1, "completed": 2}
        return sorted(
            courses,
            key=lambda item: (order.get(item.enrollment_state or "", 3), item.name.casefold()),
        )


@dataclass
class QuercusSyncService:
    db: Session
    quercus: QuercusService | None = None

    def __post_init__(self) -> None:
        self.quercus = self.quercus or QuercusService(self.db)
        self.root = ensure_act_workspace().quercus
        self.processing = QuercusProcessingService(self.db)

    def run(self) -> dict[str, Any]:
        if not _SYNC_LOCK.acquire(blocking=False):
            return {"status": "partial", "course_count": 0, "failed_count": 0, "error_type": "sync_in_progress"}
        try:
            return self._run_locked()
        finally:
            self.processing.close()
            _SYNC_LOCK.release()

    def _run_locked(self) -> dict[str, Any]:
        self.processing.prepare_locked()
        selected = self.db.scalars(select(QuercusCourse).where(QuercusCourse.selected.is_(True))).all()
        if not selected:
            return {
                "status": "succeeded",
                "course_count": 0,
                "succeeded_count": 0,
                "failed_count": 0,
                "error_type": None,
            }
        try:
            provider, connection = self.quercus._provider()
            remote_courses = {str(item["id"]): item for item in provider.courses()}
        except (QuercusError, QuercusProviderError) as exc:
            now = utc_now()
            for course in selected:
                course.last_sync_status = "failed"
                course.last_error_type = exc.error_type
                course.last_sync_completed_at = now
            connection = self.quercus._connection()
            if connection is not None:
                connection.status = "invalid" if exc.error_type == "invalid_credential" else connection.status
                connection.error_type = exc.error_type
            self.db.commit()
            return {
                "status": "failed",
                "course_count": len(selected),
                "failed_count": len(selected),
                "error_type": exc.error_type,
            }
        succeeded = 0
        partial = 0
        for course in selected:
            remote = remote_courses.get(course.course_id)
            if remote is None:
                course.last_sync_status = "failed"
                course.last_error_type = "not_found"
                course.last_sync_completed_at = utc_now()
                partial += 1
                self.db.commit()
                continue
            if self._sync_course(provider, connection, course, remote):
                succeeded += 1
            else:
                partial += 1
        return {
            "status": "partial" if partial else "succeeded",
            "course_count": len(selected),
            "succeeded_count": succeeded,
            "failed_count": partial,
            "error_type": None,
        }

    def _sync_course(
        self,
        provider: QuercusProvider,
        connection: IntegrationConnection,
        course: QuercusCourse,
        remote: dict[str, Any],
    ) -> bool:
        course.sync_generation += 1
        generation = course.sync_generation
        course.last_sync_started_at = utc_now()
        course.last_sync_status = "running"
        course.last_error_type = None
        self.db.commit()
        course_root = (self.root / course.local_path).resolve()
        if course_root.parent != self.root.resolve():
            course.last_sync_status = "failed"
            course.last_error_type = "invalid_local_path"
            self.db.commit()
            return False
        course_root.mkdir(parents=True, exist_ok=True)
        (course_root / "files" / "raw").mkdir(parents=True, exist_ok=True)
        (course_root / "files" / "processed").mkdir(parents=True, exist_ok=True)
        failures: list[str] = []
        assignments: list[dict[str, Any]] = []
        try:
            if not self._sync_files(provider, course, course_root, generation, [remote]):
                failures.append("file")
        except (QuercusProviderError, OSError, ValueError):
            failures.append("file")
        self.processing.process_course_locked(course)
        try:
            self._sync_course_overview(course, course_root, remote, generation)
        except Exception:
            failures.append("course")
        for resource_type, loader, writer in (
            ("assignment", lambda: provider.assignments(course.course_id), self._assignment_markdown),
            ("page", lambda: provider.pages(course.course_id), self._page_markdown),
            ("announcement", lambda: provider.announcements(course.course_id), self._announcement_markdown),
            ("classic_quiz", lambda: provider.classic_quizzes(course.course_id), self._quiz_markdown),
            ("new_quiz", lambda: provider.new_quizzes(course.course_id), self._quiz_markdown),
        ):
            try:
                complete_enumeration = True
                if resource_type == "page":
                    items, complete_enumeration = provider.pages_for_sync(course.course_id)
                else:
                    items = loader()
                if resource_type == "assignment":
                    assignments = items
                self._sync_text_group(
                    course,
                    course_root,
                    resource_type,
                    items,
                    generation,
                    writer,
                    complete_enumeration=complete_enumeration,
                )
            except (QuercusProviderError, OSError, ValueError):
                failures.append(resource_type)
        try:
            self._sync_grades(course, course_root, remote, assignments, generation)
        except Exception:
            failures.append("grades")
        try:
            self._sync_modules(provider, course, course_root, generation)
        except (QuercusProviderError, OSError, ValueError):
            failures.append("module")
        course.last_sync_completed_at = utc_now()
        course.last_sync_status = "partial" if failures else "succeeded"
        course.last_error_type = failures[0] if failures else None
        course.skipped_file_count = len(
            self.db.scalars(
                select(QuercusSyncResource).where(
                    QuercusSyncResource.course_id == course.course_id,
                    QuercusSyncResource.resource_type == "file",
                    QuercusSyncResource.download_state.like("skipped%"),
                )
            ).all()
        )
        connection.status = "connected"
        connection.error_type = None
        connection.last_validated_at = utc_now()
        self.db.commit()
        return not failures

    def _sync_course_overview(
        self, course: QuercusCourse, course_root: Path, item: dict[str, Any], generation: int
    ) -> None:
        content = [f"# {course.name}", ""]
        for label, value in (
            ("Course code", course.course_code),
            ("Term", course.term_name),
            ("Start", item.get("start_at")),
            ("End", item.get("end_at")),
        ):
            if value:
                content.append(f"- {label}: {value}")
        body = self._sanitize_resource_html(course, "README.md", item.get("syllabus_body"))
        if body:
            content.extend(["", "## Syllabus", "", body])
        self._upsert_text(course, "course", course.course_id, "README.md", "\n".join(content) + "\n", generation)

    def _sync_text_group(
        self,
        course: QuercusCourse,
        course_root: Path,
        resource_type: str,
        items: list[dict[str, Any]],
        generation: int,
        writer: Callable[[QuercusCourse, dict[str, Any], str], str],
        *,
        complete_enumeration: bool = True,
    ) -> None:
        folder = {
            "assignment": "assignments",
            "page": "pages",
            "announcement": "announcements",
            "classic_quiz": "quizzes",
            "new_quiz": "quizzes",
        }[resource_type]
        for item in items:
            if resource_type == "page":
                resource_id = str(item.get("url") or item.get("page_id") or item.get("id") or "")
            else:
                resource_id = str(item.get("id") or item.get("page_id") or item.get("url") or "")
            if not resource_id:
                continue
            title = str(item.get("title") or item.get("name") or "Untitled")
            relative = self._resource_path(course, resource_type, resource_id, folder, title, ".md")
            self._upsert_text(
                course,
                resource_type,
                resource_id,
                relative,
                writer(course, item, relative),
                generation,
                item=item,
            )
        if complete_enumeration:
            self._cleanup_missing(course, course_root, resource_type, generation)

    def _sync_modules(
        self, provider: QuercusProvider, course: QuercusCourse, course_root: Path, generation: int
    ) -> None:
        modules = provider.modules(course.course_id)
        modules.sort(key=lambda item: int(item.get("position") or 0))
        for module in modules:
            resource_id = str(module.get("id") or "")
            if not resource_id:
                continue
            position = int(module.get("position") or 0)
            title = str(module.get("name") or "Untitled module")
            folder_name = f"{position:02d}-{_slug(title)}"
            relative = self._resource_path(course, "module", resource_id, "modules", folder_name, "/README.md")
            lines = [f"# {title}", ""]
            for module_item in sorted(module.get("items") or [], key=lambda item: int(item.get("position") or 0)):
                if not isinstance(module_item, dict):
                    continue
                indent = "  " * max(0, int(module_item.get("indent") or 0))
                item_title = str(module_item.get("title") or "Untitled item")
                target = self._module_item_target(course, module_item, relative)
                lines.append(f"{indent}- [{item_title}]({target})" if target else f"{indent}- {item_title}")
            self._upsert_text(
                course, "module", resource_id, relative, "\n".join(lines) + "\n", generation, item=module
            )
        self._cleanup_missing(course, course_root, "module", generation)

    def _sync_grades(
        self,
        course: QuercusCourse,
        course_root: Path,
        remote: dict[str, Any],
        assignments: list[dict[str, Any]],
        generation: int,
    ) -> None:
        lines = [f"# Grades — {course.name}", ""]
        enrollments = remote.get("enrollments")
        if isinstance(enrollments, list):
            for enrollment in enrollments:
                grades = enrollment.get("grades") if isinstance(enrollment, dict) else None
                if not isinstance(grades, dict):
                    continue
                for label, key in (
                    ("Current score", "current_score"),
                    ("Current grade", "current_grade"),
                    ("Final score", "final_score"),
                    ("Final grade", "final_grade"),
                ):
                    if grades.get(key) is not None:
                        lines.append(f"- {label}: {grades[key]}")
        lines.extend(["", "## Assignments", "", "| Assignment | Score | Grade | Submitted |", "| --- | ---: | --- | --- |"])
        for item in assignments:
            submission = item.get("submission")
            if not isinstance(submission, dict):
                continue
            lines.append(
                "| {name} | {score} | {grade} | {submitted} |".format(
                    name=str(item.get("name") or "Untitled").replace("|", "\\|"),
                    score=submission.get("score") if submission.get("score") is not None else "",
                    grade=str(submission.get("grade") or "").replace("|", "\\|"),
                    submitted="Yes" if submission.get("submitted_at") else "No",
                )
            )
        self._upsert_text(course, "grades", "self", "grades/grades.md", "\n".join(lines) + "\n", generation)

    def _sync_files(
        self,
        provider: QuercusProvider,
        course: QuercusCourse,
        course_root: Path,
        generation: int,
        discovery_sources: list[Any],
    ) -> bool:
        folders = provider.folders(course.course_id)
        folder_names = {
            str(folder.get("id")): _safe_canvas_folder(folder.get("full_name") or folder.get("name"))
            for folder in folders
            if isinstance(folder, dict) and folder.get("id") is not None
        }
        complete_listing = True
        failed = False
        try:
            files = provider.files(course.course_id)
        except QuercusProviderError as exc:
            if exc.error_type != "provider_forbidden":
                raise
            complete_listing = False
            files, failed = self._linked_files(provider, course, discovery_sources)
        for item in files:
            try:
                self._sync_file_item(provider, course, course_root, generation, folder_names, item)
            except (QuercusProviderError, OSError, ValueError):
                failed = True
        self.db.commit()
        if complete_listing:
            self._cleanup_missing(course, course_root, "file", generation)
        return not failed

    def _linked_files(
        self, provider: QuercusProvider, course: QuercusCourse, discovery_sources: list[Any]
    ) -> tuple[list[dict[str, Any]], bool]:
        sources = list(discovery_sources)
        source_failed = False
        for loader in (
            provider.modules,
            provider.assignment_index,
            provider.announcements,
        ):
            try:
                sources.extend(loader(course.course_id))
            except QuercusProviderError as exc:
                if exc.error_type not in {"provider_forbidden", "not_found"}:
                    source_failed = True
        try:
            pages, _complete_enumeration = provider.pages_for_sync(course.course_id)
            sources.extend(pages)
        except QuercusProviderError as exc:
            if exc.error_type not in {"provider_forbidden", "not_found"}:
                source_failed = True
        references = _discover_file_references(sources)
        files: list[dict[str, Any]] = []
        for resource_id, reference in references.items():
            if isinstance(reference.get("size"), int):
                files.append(reference)
                continue
            try:
                metadata = provider.file(resource_id, reference.get("_reference_url"))
            except QuercusProviderError as exc:
                if exc.error_type not in {"provider_forbidden", "not_found"}:
                    source_failed = True
                    continue
                metadata = dict(reference)
                metadata["_inaccessible"] = True
            else:
                metadata = {**reference, **metadata}
            files.append(metadata)
        return files, source_failed

    def _sync_file_item(
        self,
        provider: QuercusProvider,
        course: QuercusCourse,
        course_root: Path,
        generation: int,
        folder_names: dict[str, str],
        item: dict[str, Any],
    ) -> None:
        resource_id = str(item.get("id") or "")
        if not resource_id:
            return
        size = item.get("size") if isinstance(item.get("size"), int) else None
        filename = _filename(str(item.get("display_name") or item.get("filename") or item.get("title") or "file"))
        folder = folder_names.get(str(item.get("folder_id")), "")
        relative = self._resource_path(
            course, "file", resource_id, f"files/raw/{folder}".rstrip("/"), filename, ""
        )
        fingerprint = _fingerprint(
            {
                "updated_at": item.get("updated_at"),
                "size": size,
                "content_type": item.get("content-type") or item.get("content_type"),
                "filename": filename,
            }
        )
        row = self._resource(course, "file", resource_id)
        if item.get("_inaccessible") or item.get("hidden_for_user") or item.get("locked"):
            self._record_file(
                row, course, resource_id, relative, fingerprint, generation, item, "skipped_inaccessible"
            )
            self._delete_relative(course_root, row.relative_path if row else relative)
            if row is not None:
                self.processing.remove_processed(course_root, row)
            return
        if size is None:
            self._record_file(
                row, course, resource_id, relative, fingerprint, generation, item, "skipped_unknown_size"
            )
            self._delete_relative(course_root, row.relative_path if row else relative)
            if row is not None:
                self.processing.remove_processed(course_root, row)
            return
        if size > MAX_AUTOMATIC_FILE_BYTES:
            self._record_file(
                row, course, resource_id, relative, fingerprint, generation, item, "skipped_too_large"
            )
            self._delete_relative(course_root, row.relative_path if row else relative)
            if row is not None:
                self.processing.remove_processed(course_root, row)
            return
        target = course_root / PurePosixPath(relative)
        unchanged = (
            row is not None
            and row.content_fingerprint == fingerprint
            and row.download_state == "downloaded"
        )
        if not unchanged or not target.is_file():
            response = provider.open_file(resource_id, item.get("url") or item.get("_reference_url"))
            try:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_AUTOMATIC_FILE_BYTES:
                    self._record_file(
                        row,
                        course,
                        resource_id,
                        relative,
                        fingerprint,
                        generation,
                        item,
                        "skipped_too_large",
                    )
                    current = row or self._resource(course, "file", resource_id)
                    self._delete_relative(course_root, current.relative_path if current else relative)
                    if current is not None:
                        self.processing.remove_processed(course_root, current)
                    return
                self._atomic_stream(target, response)
            finally:
                response.close()
        self._record_file(row, course, resource_id, relative, fingerprint, generation, item, "downloaded")

    def _record_file(
        self,
        row: QuercusSyncResource | None,
        course: QuercusCourse,
        resource_id: str,
        relative: str,
        fingerprint: str,
        generation: int,
        item: dict[str, Any],
        state: str,
    ) -> None:
        row = row or QuercusSyncResource(
            course_id=course.course_id,
            resource_type="file",
            resource_id=resource_id,
            content_fingerprint=fingerprint,
            last_seen_generation=generation,
        )
        if row.id is None:
            self.db.add(row)
        row.content_fingerprint = fingerprint
        row.relative_path = relative
        row.size_bytes = item.get("size") if isinstance(item.get("size"), int) else None
        row.content_type = str(item.get("content-type") or item.get("content_type") or "")[:256] or None
        row.remote_updated_at = str(item.get("updated_at") or "")[:64] or None
        row.parent_resource_id = str(item.get("folder_id") or "")[:128] or None
        row.download_state = state
        row.last_seen_generation = generation

    def _upsert_text(
        self,
        course: QuercusCourse,
        resource_type: str,
        resource_id: str,
        relative: str,
        content: str,
        generation: int,
        *,
        item: dict[str, Any] | None = None,
    ) -> None:
        encoded = content.encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        row = self._resource(course, resource_type, resource_id)
        actual_relative = row.relative_path if row is not None and row.relative_path else relative
        target = self.root / course.local_path / PurePosixPath(actual_relative)
        if row is None or row.content_fingerprint != fingerprint or not target.is_file():
            _atomic_write(target, encoded)
        if row is None:
            row = QuercusSyncResource(
                course_id=course.course_id,
                resource_type=resource_type,
                resource_id=resource_id,
                content_fingerprint=fingerprint,
                last_seen_generation=generation,
            )
            self.db.add(row)
        row.content_fingerprint = fingerprint
        row.relative_path = actual_relative
        row.remote_updated_at = str((item or {}).get("updated_at") or "")[:64] or None
        row.remote_version = str((item or {}).get("version_number") or "")[:128] or None
        row.download_state = "written"
        row.last_seen_generation = generation
        self.db.commit()

    def _cleanup_missing(self, course: QuercusCourse, course_root: Path, resource_type: str, generation: int) -> None:
        stale = self.db.scalars(
            select(QuercusSyncResource).where(
                QuercusSyncResource.course_id == course.course_id,
                QuercusSyncResource.resource_type == resource_type,
                QuercusSyncResource.last_seen_generation != generation,
            )
        ).all()
        for row in stale:
            self._delete_relative(course_root, row.relative_path)
            self.processing.remove_processed(course_root, row)
            self.db.delete(row)
        self.db.commit()

    def _resource_path(
        self,
        course: QuercusCourse,
        resource_type: str,
        resource_id: str,
        folder: str,
        title: str,
        suffix: str,
    ) -> str:
        row = self._resource(course, resource_type, resource_id)
        if row is not None and row.relative_path:
            return row.relative_path
        base = _slug(title) if suffix else _filename(title)
        candidate = f"{folder}/{base}{suffix}".replace("//", "/")
        used = {
            str(path).casefold()
            for path in self.db.scalars(
                select(QuercusSyncResource.relative_path).where(
                    QuercusSyncResource.course_id == course.course_id,
                    QuercusSyncResource.relative_path.is_not(None),
                )
            ).all()
        }
        if candidate.casefold() not in used:
            return candidate
        stem = base
        number = 2
        while True:
            candidate = f"{folder}/{stem}-{number}{suffix}".replace("//", "/")
            if candidate.casefold() not in used:
                return candidate
            number += 1

    def _module_item_target(self, course: QuercusCourse, item: dict[str, Any], module_relative: str) -> str | None:
        type_map = {
            "Assignment": "assignment",
            "Page": "page",
            "Quiz": "classic_quiz",
            "File": "file",
            "Discussion": "announcement",
        }
        resource_type = type_map.get(str(item.get("type")))
        resource_ids = [item.get("content_id"), item.get("page_url")]
        if not resource_type or not any(resource_id is not None for resource_id in resource_ids):
            return str(item.get("external_url")) if item.get("external_url") else None
        row = next(
            (
                self._resource(course, resource_type, str(resource_id))
                for resource_id in resource_ids
                if resource_id is not None
            ),
            None,
        )
        if row is None or not row.relative_path:
            return str(item.get("html_url")) if item.get("html_url") else None
        module_dir = PurePosixPath(module_relative).parent
        target = self.processing.preferred_relative_path(row)
        return posixpath.relpath(target, module_dir.as_posix()) if target else None

    def _sanitize_resource_html(self, course: QuercusCourse, relative: str, value: Any) -> str:
        source_dir = PurePosixPath(relative).parent

        def rewrite_url(url: str) -> str:
            parsed = urlparse(url)
            match = re.search(r"/files/([^/?#]+)", parsed.path)
            if match is None:
                return url
            row = self._resource(course, "file", match.group(1))
            if row is None or not row.relative_path or row.download_state != "downloaded":
                return url
            target = self.processing.preferred_relative_path(row)
            return posixpath.relpath(target, source_dir.as_posix()) if target else url

        return sanitize_html(value, rewrite_url)

    def _resource(self, course: QuercusCourse, resource_type: str, resource_id: str) -> QuercusSyncResource | None:
        return self.db.scalar(
            select(QuercusSyncResource).where(
                QuercusSyncResource.course_id == course.course_id,
                QuercusSyncResource.resource_type == resource_type,
                QuercusSyncResource.resource_id == resource_id,
            )
        )

    @staticmethod
    def _delete_relative(course_root: Path, relative: str | None) -> None:
        if not relative:
            return
        target = (course_root / PurePosixPath(relative)).resolve()
        if target == course_root or course_root not in target.parents:
            raise ValueError("Quercus resource path escaped its course root")
        if target.is_file():
            target.unlink()
        parent = target.parent
        while parent != course_root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    @staticmethod
    def _atomic_stream(target: Path, response: Any) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        total = 0
        try:
            with NamedTemporaryFile(dir=target.parent, prefix=".quercus-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                while True:
                    chunk = response.read(MAX_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_AUTOMATIC_FILE_BYTES:
                        raise QuercusProviderError("response_too_large", "Quercus file exceeded 8 GiB")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _assignment_markdown(self, course: QuercusCourse, item: dict[str, Any], relative: str) -> str:
        lines = [f"# {item.get('name') or 'Untitled assignment'}", ""]
        for label, key in (("Due", "due_at"), ("Available from", "unlock_at"), ("Closes", "lock_at"), ("Points", "points_possible")):
            if item.get(key) is not None:
                lines.append(f"- {label}: {item[key]}")
        submission = item.get("submission")
        if isinstance(submission, dict):
            lines.extend(["", "## Your submission", ""])
            for label, key in (("Submitted", "submitted_at"), ("Score", "score"), ("Grade", "grade"), ("Status", "workflow_state")):
                if submission.get(key) is not None:
                    lines.append(f"- {label}: {submission[key]}")
            comments = submission.get("submission_comments")
            if isinstance(comments, list):
                for comment in comments:
                    if isinstance(comment, dict) and comment.get("comment"):
                        lines.extend(["", self._sanitize_resource_html(course, relative, comment["comment"])])
        body = self._sanitize_resource_html(course, relative, item.get("description"))
        if body:
            lines.extend(["", "## Description", "", body])
        return "\n".join(lines) + "\n"

    def _page_markdown(self, course: QuercusCourse, item: dict[str, Any], relative: str) -> str:
        body = self._sanitize_resource_html(course, relative, item.get("body"))
        return f"# {item.get('title') or 'Untitled page'}\n\n{body}\n"

    def _announcement_markdown(self, course: QuercusCourse, item: dict[str, Any], relative: str) -> str:
        lines = [f"# {item.get('title') or 'Untitled announcement'}", ""]
        if item.get("posted_at"):
            lines.extend([f"Posted: {item['posted_at']}", ""])
        lines.append(self._sanitize_resource_html(course, relative, item.get("message")))
        return "\n".join(lines) + "\n"

    def _quiz_markdown(self, course: QuercusCourse, item: dict[str, Any], relative: str) -> str:
        lines = [f"# {item.get('title') or 'Untitled quiz'}", ""]
        for label, key in (
            ("Due", "due_at"),
            ("Available from", "unlock_at"),
            ("Closes", "lock_at"),
            ("Points", "points_possible"),
            ("Time limit", "time_limit"),
            ("Allowed attempts", "allowed_attempts"),
        ):
            if item.get(key) is not None:
                lines.append(f"- {label}: {item[key]}")
        body = self._sanitize_resource_html(course, relative, item.get("description") or item.get("instructions"))
        if body:
            lines.extend(["", body])
        questions = item.get("_questions") or item.get("_items") or item.get("_submission_questions")
        if isinstance(questions, list) and questions:
            lines.extend(["", "## Currently visible questions", ""])
            for index, question in enumerate(questions, start=1):
                if not isinstance(question, dict):
                    continue
                entry = question.get("entry") if isinstance(question.get("entry"), dict) else question
                question_body = self._sanitize_resource_html(
                    course,
                    relative,
                    entry.get("item_body") or entry.get("question_text") or entry.get("title")
                )
                lines.extend([f"### {index}", "", question_body or "Question details are not available.", ""])
        submission = item.get("_submission")
        if isinstance(submission, dict):
            visible = submission.get("quiz_submissions")
            if isinstance(visible, list) and visible:
                lines.extend(["", "## Your visible result", ""])
                result = visible[0] if isinstance(visible[0], dict) else {}
                for label, key in (("Score", "score"), ("Kept score", "kept_score"), ("Attempt", "attempt"), ("Finished", "finished_at")):
                    if result.get(key) is not None:
                        lines.append(f"- {label}: {result[key]}")
        assignment_submission = item.get("_assignment_submission")
        if isinstance(assignment_submission, dict):
            lines.extend(["", "## Your visible result", ""])
            for label, key in (("Score", "score"), ("Grade", "grade"), ("Attempt", "attempt")):
                if assignment_submission.get(key) is not None:
                    lines.append(f"- {label}: {assignment_submission[key]}")
            comments = assignment_submission.get("submission_comments")
            if isinstance(comments, list):
                for comment in comments:
                    if isinstance(comment, dict) and comment.get("comment"):
                        lines.extend(
                            ["", self._sanitize_resource_html(course, relative, comment["comment"])]
                        )
        return "\n".join(lines) + "\n"


class QuercusSyncDispatcher:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory
        self._lock = threading.Lock()
        self._pending = False
        self._running = False

    def request(self) -> None:
        with self._lock:
            self._pending = True
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, name="quercus-sync", daemon=True).start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._running = False
                    return
                self._pending = False
            with self.session_factory() as db:
                QuercusSyncService(db).run()


def _discover_file_references(values: list[Any]) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}

    def remember(resource_id: Any, item: dict[str, Any] | None = None, reference_url: str | None = None) -> None:
        normalized_id = str(resource_id or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", normalized_id):
            return
        reference = references.setdefault(normalized_id, {"id": normalized_id})
        if item is not None:
            for key in (
                "display_name",
                "filename",
                "title",
                "folder_id",
                "size",
                "content-type",
                "content_type",
                "updated_at",
                "url",
                "hidden_for_user",
                "locked",
            ):
                if item.get(key) is not None:
                    reference[key] = item[key]
        if reference_url and (
            not reference.get("_reference_url")
            or "verifier=" in reference_url.casefold()
        ):
            reference["_reference_url"] = reference_url

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if str(value.get("type") or "").casefold() == "file":
                reference_url = next(
                    (
                        str(value[key])
                        for key in ("url", "html_url")
                        if isinstance(value.get(key), str)
                    ),
                    None,
                )
                remember(
                    value.get("content_id"),
                    {"title": value.get("title")},
                    reference_url,
                )
            matched_ids: set[str] = set()
            for child in value.values():
                if not isinstance(child, str):
                    continue
                for match in _FILE_LINK_RE.finditer(html.unescape(child)):
                    matched_ids.add(match.group("id"))
                    remember(match.group("id"), reference_url=match.group("url").rstrip("),.;"))
            object_id = str(value.get("id") or "")
            if object_id in matched_ids and (value.get("filename") or value.get("display_name")):
                remember(object_id, value)
            for child in value.values():
                walk(child)
            return
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if isinstance(value, str):
            for match in _FILE_LINK_RE.finditer(html.unescape(value)):
                remember(match.group("id"), reference_url=match.group("url").rstrip("),.;"))

    walk(values)
    return references


def _slug(value: str) -> str:
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", value).strip(" .-")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized[:120] or "untitled"


def _filename(value: str) -> str:
    path = Path(value)
    stem = _slug(path.stem)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", path.suffix)[:20]
    return f"{stem}{suffix}"


def _unique_name(base: str, used: set[str]) -> str:
    if base.casefold() not in used:
        return base
    number = 2
    while f"{base}-{number}".casefold() in used:
        number += 1
    return f"{base}-{number}"


def _safe_canvas_folder(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parts = [_slug(part) for part in re.split(r"[/\\]+", value) if part.strip()]
    if parts and parts[0].casefold() in {"course files", "files"}:
        parts = parts[1:]
    return "/".join(parts)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix=".quercus-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
