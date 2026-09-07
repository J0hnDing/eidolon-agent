from __future__ import annotations

from collections.abc import Generator
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import QuercusCourse, QuercusCourseExclusion, QuercusSyncResource
from app.services import act_workspace_service, quercus_service
from app.services.quercus_processing_service import PROCESSING_MARKER
from app.services.quercus_provider import QuercusProviderError
from app.services.quercus_service import (
    MAX_AUTOMATIC_FILE_BYTES,
    QuercusError,
    QuercusService,
    QuercusSyncDispatcher,
    QuercusSyncService,
    sanitize_html,
)
from app.services.secret_store import FakeSecretStore


class FileResponse(BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content))}


class FakeQuercusProvider:
    def __init__(self, account_id: str = "user-1") -> None:
        self.account_id = account_id
        self.open_count = 0
        self.file_metadata_count = 0
        self.files_forbidden = False
        self.page_failure = False
        self.page_listing_not_found = False
        self.course_values = [
            {
                "id": "course-1",
                "name": "Design / Systems",
                "course_code": "CSC 400",
                "term": {"name": "Fall 2026"},
                "_enrollment_state": "active",
                "syllabus_body": '<p>Course overview</p><script>ignore()</script>',
                "enrollments": [{"grades": {"current_score": 91, "current_grade": "A"}}],
            }
        ]
        self.assignment_values = [
            {
                "id": "assignment-1",
                "name": "Project: One",
                "description": (
                    '<p onclick="bad()">Build it '
                    '<a href="https://q.utoronto.ca/courses/course-1/files/file-1/download">notes</a></p>'
                ),
                "updated_at": "2026-09-01T10:00:00Z",
                "submission": {
                    "score": 18,
                    "grade": "18",
                    "submitted_at": "2026-09-01T09:00:00Z",
                    "submission_comments": [{"comment": "Good work"}],
                },
            }
        ]
        self.page_values: list[dict[str, Any]] = [
            {
                "page_id": "page-1",
                "url": "welcome",
                "title": "Welcome",
                "body": '<p>Read me</p><form><input value="secret"></form>',
                "updated_at": "2026-09-01T10:00:00Z",
            }
        ]
        self.module_page_values: list[dict[str, Any]] = [
            {
                "page_id": "page-2",
                "url": "instructions-regarding-problem-sets",
                "title": "Instructions Regarding Problem Sets",
                "body": "<p>Submit the suggested exercises by email.</p>",
                "updated_at": "2026-09-06T10:00:00Z",
            }
        ]
        self.file_values = [
            {
                "id": "file-1",
                "display_name": "notes.txt",
                "folder_id": "folder-1",
                "size": 5,
                "content_type": "text/plain",
                "updated_at": "2026-09-01T10:00:00Z",
                "url": "https://q.utoronto.ca/files/file-1/download",
            },
            {
                "id": "file-large",
                "display_name": "lecture.mp4",
                "folder_id": "folder-1",
                "size": MAX_AUTOMATIC_FILE_BYTES + 1,
                "content_type": "video/mp4",
            },
            {
                "id": "file-unknown",
                "display_name": "unknown.bin",
                "folder_id": "folder-1",
                "size": None,
            },
        ]

    def profile(self) -> dict[str, Any]:
        return {"id": self.account_id, "name": f"User {self.account_id}"}

    def courses(self) -> list[dict[str, Any]]:
        return self.course_values

    def assignments(self, course_id: str) -> list[dict[str, Any]]:
        return self.assignment_values

    def assignment_index(self, course_id: str) -> list[dict[str, Any]]:
        return self.assignment_values

    def pages(self, course_id: str) -> list[dict[str, Any]]:
        if self.page_listing_not_found:
            raise QuercusProviderError("not_found", "page listing unavailable")
        if self.page_failure:
            raise QuercusProviderError("provider_unavailable", "page listing failed")
        return self.page_values

    def pages_from_modules(self, course_id: str) -> list[dict[str, Any]]:
        return self.module_page_values

    def pages_for_sync(self, course_id: str) -> tuple[list[dict[str, Any]], bool]:
        try:
            return self.pages(course_id), True
        except QuercusProviderError as exc:
            if exc.error_type not in {"provider_forbidden", "not_found"}:
                raise
            return self.pages_from_modules(course_id), False

    def announcements(self, course_id: str) -> list[dict[str, Any]]:
        return [{"id": "news-1", "title": "News", "message": "Hello"}]

    def classic_quizzes(self, course_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "quiz-1",
                "title": "Quiz",
                "_questions": [{"question_text": "Visible prompt"}],
                "_submission": {"quiz_submissions": [{"score": 4, "attempt": 1}]},
            }
        ]

    def new_quizzes(self, course_id: str) -> list[dict[str, Any]]:
        return []

    def folders(self, course_id: str) -> list[dict[str, Any]]:
        return [{"id": "folder-1", "full_name": "course files/Week 1"}]

    def files(self, course_id: str) -> list[dict[str, Any]]:
        if self.files_forbidden:
            raise QuercusProviderError("provider_forbidden", "file listing forbidden")
        return self.file_values

    def file(self, file_id: str, reference_url: str | None = None) -> dict[str, Any]:
        self.file_metadata_count += 1
        return next(item for item in self.file_values if item["id"] == file_id)

    def modules(self, course_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "module-1",
                "name": "Getting Started",
                "position": 1,
                "items": [
                    {
                        "type": "Assignment",
                        "content_id": "assignment-1",
                        "title": "Project: One",
                        "position": 1,
                    },
                    {
                        "type": "Page",
                        "page_url": "instructions-regarding-problem-sets",
                        "html_url": "https://q.utoronto.ca/courses/course-1/modules/items/page-2",
                        "title": "Instructions Regarding Problem Sets",
                        "position": 2,
                    }
                ],
            }
        ]

    def open_file(self, file_id: str, download_url: str | None) -> FileResponse:
        self.open_count += 1
        assert file_id == "file-1"
        return FileResponse(b"notes")


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def act_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "act"
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", root)
    monkeypatch.setattr(quercus_service, "ACT_ROOT", root)
    return root


def connected_service(
    db: Session,
    provider: FakeQuercusProvider,
    *,
    queued: list[bool] | None = None,
) -> tuple[QuercusService, FakeSecretStore]:
    store = FakeSecretStore()
    service = QuercusService(
        db,
        secret_store=store,
        provider_factory=lambda _token: provider,
        queue_sync=(lambda: queued.append(True)) if queued is not None else None,
    )
    service.put_connection("personal-token")
    return service, store


def test_connection_selection_retention_identity_and_deletion(
    db_session: Session, act_root: Path
) -> None:
    queued: list[bool] = []
    provider = FakeQuercusProvider()
    service, store = connected_service(db_session, provider, queued=queued)

    selected = service.select_courses(["course-1"])

    assert queued == [True]
    assert selected[0].selected is True
    assert store.namespaces == {"fake-0001": "quercus"}
    course = db_session.get(QuercusCourse, "course-1")
    assert course is not None
    course_root = act_root / "knowledge" / "quercus" / course.local_path
    course_root.mkdir(parents=True)
    (course_root / "retained.txt").write_text("retained", encoding="utf-8")
    db_session.add(
        QuercusSyncResource(
            course_id=course.course_id,
            resource_type="page",
            resource_id="page-1",
            content_fingerprint="fingerprint",
            last_seen_generation=1,
        )
    )
    db_session.commit()

    with pytest.raises(QuercusError, match="different account"):
        QuercusService(
            db_session,
            secret_store=store,
            provider_factory=lambda _token: FakeQuercusProvider("user-2"),
        ).put_connection("replacement-token")

    service.delete_course("course-1")
    assert db_session.get(QuercusCourse, "course-1") is None
    assert db_session.scalar(
        select(QuercusSyncResource).where(QuercusSyncResource.course_id == "course-1")
    ) is None
    assert not course_root.exists()
    assert service.courses() == []
    assert db_session.get(QuercusCourseExclusion, ("user-1", "course-1")) is not None


def test_delete_untracked_course_excludes_it_from_future_course_lists(db_session: Session) -> None:
    provider = FakeQuercusProvider()
    service, _store = connected_service(db_session, provider)

    assert [course.course_id for course in service.courses()] == ["course-1"]

    service.delete_course("course-1")

    assert service.courses() == []
    assert db_session.get(QuercusCourse, "course-1") is None
    assert db_session.get(QuercusCourseExclusion, ("user-1", "course-1")) is not None
    with pytest.raises(QuercusError, match="not accessible"):
        service.select_courses(["course-1"])


def test_delete_fails_promptly_while_quercus_work_is_in_progress(db_session: Session) -> None:
    service, _store = connected_service(db_session, FakeQuercusProvider())
    assert quercus_service._SYNC_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(QuercusError, match="try deleting the course again shortly") as exc_info:
            service.delete_course("course-1")
    finally:
        quercus_service._SYNC_LOCK.release()

    assert exc_info.value.error_type == "operation_in_progress"
    assert db_session.get(QuercusCourseExclusion, ("user-1", "course-1")) is None


def test_daily_sync_is_a_successful_noop_without_selected_courses(
    db_session: Session, act_root: Path
) -> None:
    result = QuercusSyncService(
        db_session,
        quercus=QuercusService(db_session, secret_store=FakeSecretStore()),
    ).run()

    assert result == {
        "status": "succeeded",
        "course_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "error_type": None,
    }
    assert (act_root / "knowledge" / "quercus").is_dir()


def test_sync_is_incremental_sanitized_and_keeps_metadata_out_of_knowledge(
    db_session: Session, act_root: Path
) -> None:
    provider = FakeQuercusProvider()
    service, _store = connected_service(db_session, provider)
    service.select_courses(["course-1"])

    first = QuercusSyncService(db_session, quercus=service).run()
    course = db_session.get(QuercusCourse, "course-1")
    assert course is not None
    course_root = act_root / "knowledge" / "quercus" / course.local_path
    assignment = next((course_root / "assignments").glob("*.md"))
    first_mtime = assignment.stat().st_mtime_ns

    assert first["status"] == "succeeded"
    assert "Good work" in assignment.read_text(encoding="utf-8")
    assert "onclick" not in assignment.read_text(encoding="utf-8")
    assert 'href="../files/raw/Week 1/notes.txt"' in assignment.read_text(encoding="utf-8")
    assert "<script" not in (course_root / "README.md").read_text(encoding="utf-8")
    assert "<form" not in next((course_root / "pages").glob("*.md")).read_text(encoding="utf-8")
    assert (course_root / "files" / "raw" / "Week 1" / "notes.txt").read_bytes() == b"notes"
    assert (course_root / "files" / "processed").is_dir()
    assert provider.open_count == 1
    assert course.skipped_file_count == 2
    assert not list(course_root.rglob("*.json"))
    assert not list(course_root.rglob("*.tmp"))

    second = QuercusSyncService(db_session, quercus=service).run()

    assert second["status"] == "succeeded"
    assert provider.open_count == 1
    assert assignment.stat().st_mtime_ns == first_mtime
    skipped = db_session.scalars(
        select(QuercusSyncResource).where(QuercusSyncResource.download_state.like("skipped%"))
    ).all()
    assert {row.download_state for row in skipped} == {"skipped_too_large", "skipped_unknown_size"}


def test_sync_falls_back_to_accessible_module_pages_when_page_listing_is_unavailable(
    db_session: Session, act_root: Path
) -> None:
    provider = FakeQuercusProvider()
    provider.page_listing_not_found = True
    service, _store = connected_service(db_session, provider)
    service.select_courses(["course-1"])

    result = QuercusSyncService(db_session, quercus=service).run()

    course = db_session.get(QuercusCourse, "course-1")
    assert course is not None
    course_root = act_root / "knowledge" / "quercus" / course.local_path
    page = course_root / "pages" / "Instructions Regarding Problem Sets.md"
    module = course_root / "modules" / "01-Getting Started" / "README.md"

    assert result["status"] == "succeeded"
    assert course.last_error_type is None
    assert "Submit the suggested exercises by email." in page.read_text(encoding="utf-8")
    assert "../../pages/Instructions Regarding Problem Sets.md" in module.read_text(encoding="utf-8")


def test_forbidden_bulk_file_listing_falls_back_to_content_links_without_cleanup(
    db_session: Session, act_root: Path
) -> None:
    provider = FakeQuercusProvider()
    provider.files_forbidden = True
    service, _store = connected_service(db_session, provider)
    service.select_courses(["course-1"])
    course = db_session.get(QuercusCourse, "course-1")
    assert course is not None
    course_root = act_root / "knowledge" / "quercus" / course.local_path
    stale_file = course_root / "files" / "previous.txt"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_bytes(b"previous")
    db_session.add(
        QuercusSyncResource(
            course_id=course.course_id,
            resource_type="file",
            resource_id="previous-file",
            content_fingerprint="previous",
            relative_path="files/previous.txt",
            size_bytes=8,
            download_state="downloaded",
            last_seen_generation=0,
        )
    )
    db_session.commit()

    result = QuercusSyncService(db_session, quercus=service).run()

    assert result["status"] == "succeeded"
    assert provider.file_metadata_count == 1
    assert provider.open_count == 1
    assert (course_root / "files" / "raw" / "Week 1" / "notes.txt").read_bytes() == b"notes"
    assert (course_root / "files" / "raw" / "previous.txt").read_bytes() == b"previous"
    assignment = next((course_root / "assignments").glob("*.md"))
    assert 'href="../files/raw/Week 1/notes.txt"' in assignment.read_text(encoding="utf-8")


def test_complete_deletion_removes_stale_page_but_partial_listing_preserves_it(
    db_session: Session, act_root: Path
) -> None:
    provider = FakeQuercusProvider()
    service, _store = connected_service(db_session, provider)
    service.select_courses(["course-1"])
    sync = QuercusSyncService(db_session, quercus=service)
    assert sync.run()["status"] == "succeeded"
    course = db_session.get(QuercusCourse, "course-1")
    assert course is not None
    page = next((act_root / "knowledge" / "quercus" / course.local_path / "pages").glob("*.md"))

    provider.page_failure = True
    assert sync.run()["status"] == "partial"
    assert page.exists()

    provider.page_failure = False
    provider.page_values = []
    assert sync.run()["status"] == "succeeded"
    assert not page.exists()
    assert db_session.scalar(
        select(QuercusSyncResource).where(QuercusSyncResource.resource_type == "page")
    ) is None


def test_canvas_sync_and_processing_status_are_independent_and_delete_both_file_copies(
    db_session: Session, act_root: Path
) -> None:
    provider = FakeQuercusProvider()
    service, _store = connected_service(db_session, provider)
    service.select_courses(["course-1"])
    sync = QuercusSyncService(db_session, quercus=service)
    sync.processing.set_method(PROCESSING_MARKER)

    assert sync.run()["status"] == "succeeded"
    course = db_session.get(QuercusCourse, "course-1")
    assert course is not None
    course_root = act_root / "knowledge" / "quercus" / course.local_path
    raw = course_root / "files" / "raw" / "Week 1" / "notes.txt"
    processed = course_root / "files" / "processed" / "Week 1" / "notes.md"
    assert course.last_sync_status == "succeeded"
    assert course.last_processing_status == "failed"
    assert raw.is_file()
    assert processed.is_file()

    provider.file_values = []
    assert sync.run()["status"] == "succeeded"
    assert not raw.exists()
    assert not processed.exists()


def test_html_sanitizer_removes_active_content_and_unsafe_urls() -> None:
    value = sanitize_html(
        '<p onclick="run()"><a href="javascript:run()">bad</a>'
        '<a href="https://example.com">good</a></p><iframe src="https://bad.example">x</iframe>'
    )

    assert "onclick" not in value
    assert "javascript:" not in value
    assert "iframe" not in value
    assert 'href="https://example.com"' in value


def test_immediate_sync_requests_coalesce_while_one_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    started = Event()
    release = Event()
    completed = Event()
    calls: list[bool] = []

    class FakeSync:
        def __init__(self, _db: object) -> None:
            pass

        def run(self) -> dict[str, Any]:
            calls.append(True)
            if len(calls) == 1:
                started.set()
                assert release.wait(2)
            else:
                completed.set()
            return {"status": "succeeded"}

    class SessionContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(quercus_service, "QuercusSyncService", FakeSync)
    dispatcher = QuercusSyncDispatcher(lambda: SessionContext())  # type: ignore[arg-type]

    dispatcher.request()
    assert started.wait(2)
    dispatcher.request()
    dispatcher.request()
    dispatcher.request()
    release.set()

    assert completed.wait(2)
    assert len(calls) == 2
