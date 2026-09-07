from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import QuercusCourse, QuercusSyncResource
from app.services import act_workspace_service, quercus_processing_service
from app.services.quercus_processing_service import (
    FAILURE_PLACEHOLDER,
    INFERENCE_URL,
    PROCESS_TIMEOUT_SECONDS,
    PROCESSING_MARKER,
    LlamaCppServerManager,
    QuercusProcessingService,
)


class FakeServerManager:
    def __init__(self) -> None:
        self.start_count = 0
        self.close_count = 0

    def ensure_running(self, **_kwargs) -> str:  # noqa: ANN003
        self.start_count += 1
        return INFERENCE_URL

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def course(db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> QuercusCourse:
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path / "act")
    monkeypatch.setattr(
        quercus_processing_service,
        "QUERCUS_PROCESSING_ROOT",
        tmp_path / "runtime" / "backend" / "quercus-processing",
    )
    value = QuercusCourse(
        course_id="course-1",
        account_id="user-1",
        name="Course",
        selected=True,
        local_path="Course",
    )
    db_session.add(value)
    db_session.commit()
    return value


@pytest.fixture
def llama_cpp_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "llama-cpp"
    directory.mkdir()
    executable = directory / ("llama-server.exe" if os.name == "nt" else "llama-server")
    executable.write_bytes(b"test")
    return directory


def test_startup_migrates_raw_layout_and_rewrites_links_idempotently(
    db_session: Session, course: QuercusCourse, tmp_path: Path
) -> None:
    course_root = tmp_path / "act" / "knowledge" / "quercus" / "Course"
    old_file = course_root / "files" / "Week 1" / "notes.pdf"
    old_file.parent.mkdir(parents=True)
    old_file.write_bytes(b"pdf")
    (course_root / "README.md").write_text(
        "[Notes](files/Week 1/notes.pdf)\n", encoding="utf-8"
    )
    resource = QuercusSyncResource(
        course_id=course.course_id,
        resource_type="file",
        resource_id="file-1",
        content_fingerprint="v1",
        relative_path="files/Week 1/notes.pdf",
        download_state="downloaded",
        last_seen_generation=1,
    )
    db_session.add(resource)
    db_session.commit()

    service = QuercusProcessingService(db_session, inference_probe=lambda: True)
    service.initialize()
    service.initialize()

    assert resource.relative_path == "files/raw/Week 1/notes.pdf"
    assert (course_root / "files" / "raw" / "Week 1" / "notes.pdf").read_bytes() == b"pdf"
    assert (course_root / "files" / "processed").is_dir()
    readme = (course_root / "README.md").read_text(encoding="utf-8")
    assert "files/raw/Week 1/notes.pdf" in readme
    assert not list(course_root.rglob("*.json"))


def test_marker_uses_fixed_command_environment_and_reprocesses_changed_source(
    db_session: Session,
    course: QuercusCourse,
    tmp_path: Path,
    llama_cpp_directory: Path,
) -> None:
    course_root = tmp_path / "act" / "knowledge" / "quercus" / "Course"
    raw = course_root / "files" / "raw" / "Week 1" / "notes.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"first")
    resource = QuercusSyncResource(
        course_id=course.course_id,
        resource_type="file",
        resource_id="file-1",
        content_fingerprint="v1",
        relative_path="files/raw/Week 1/notes.pdf",
        download_state="downloaded",
        last_seen_generation=1,
    )
    db_session.add(resource)
    db_session.commit()
    calls: list[tuple[list[str], dict[str, str], int, bool]] = []

    def runner(arguments, **kwargs):  # noqa: ANN001, ANN202
        calls.append((arguments, kwargs["env"], kwargs["timeout"], kwargs["shell"]))
        output = Path(arguments[-1]) / "notes"
        output.mkdir()
        (output / "notes.md").write_text(f"converted {len(calls)}", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    service = QuercusProcessingService(
        db_session,
        executable_resolver=lambda _name: "C:/marker/marker_single.exe",
        runner=runner,
        inference_probe=lambda: True,
        server_manager=(server_manager := FakeServerManager()),
    )
    service.configure(PROCESSING_MARKER, str(llama_cpp_directory))
    assert service.run()["status"] == "succeeded"

    processed = course_root / "files" / "processed" / "Week 1" / "notes.md"
    assert processed.read_text(encoding="utf-8") == "converted 1"
    arguments, environment, timeout, shell = calls[0]
    assert arguments == [
        "C:/marker/marker_single.exe",
        str(raw),
        "--mode",
        "balanced",
        "--output_format",
        "markdown",
        "--disable_image_extraction",
        "--output_dir",
        arguments[-1],
    ]
    assert environment["SURYA_INFERENCE_BACKEND"] == "llamacpp"
    assert environment["SURYA_INFERENCE_URL"] == INFERENCE_URL
    assert environment["SURYA_INFERENCE_PARALLEL"] == "1"
    assert environment["LLAMA_CPP_BINARY"] == str(
        llama_cpp_directory / ("llama-server.exe" if os.name == "nt" else "llama-server")
    )
    assert environment["PATH"].split(os.pathsep)[0] == str(llama_cpp_directory)
    assert timeout == PROCESS_TIMEOUT_SECONDS
    assert shell is False
    assert server_manager.start_count == 1
    assert server_manager.close_count == 1
    temporary_root = tmp_path / "runtime" / "backend" / "quercus-processing"
    assert not temporary_root.exists()

    assert service.run()["status"] == "succeeded"
    assert len(calls) == 1
    assert server_manager.close_count == 2
    resource.content_fingerprint = "v2"
    raw.write_bytes(b"second")
    db_session.commit()
    assert service.run()["status"] == "succeeded"
    assert processed.read_text(encoding="utf-8") == "converted 2"
    assert len(calls) == 2
    assert server_manager.start_count == 2
    assert server_manager.close_count == 3


def test_startup_cleans_current_and_legacy_processing_directories(
    db_session: Session, course: QuercusCourse, tmp_path: Path
) -> None:
    current_root = tmp_path / "runtime" / "backend" / "quercus-processing"
    legacy_root = tmp_path / "act" / "workspace" / "quercus-processing"
    for root in (current_root, legacy_root):
        (root / "marker-stale").mkdir(parents=True)
        (root / "llama-start-stale").mkdir()

    QuercusProcessingService(db_session).initialize()

    assert not current_root.exists()
    assert not legacy_root.exists()


def test_startup_preserves_unrecognized_processing_entries(
    db_session: Session, course: QuercusCourse, tmp_path: Path
) -> None:
    current_root = tmp_path / "runtime" / "backend" / "quercus-processing"
    unrelated = current_root / "do-not-delete"
    unrelated.mkdir(parents=True)

    QuercusProcessingService(db_session).initialize()

    assert unrelated.is_dir()


def test_backend_owned_inference_server_uses_keep_alive_environment(
    llama_cpp_directory: Path,
) -> None:
    llama_server = llama_cpp_directory / (
        "llama-server.exe" if os.name == "nt" else "llama-server"
    )

    environment = LlamaCppServerManager._server_environment(llama_server)

    assert environment["SURYA_INFERENCE_BACKEND"] == "llamacpp"
    assert environment["SURYA_INFERENCE_PARALLEL"] == "1"
    assert environment["SURYA_INFERENCE_KEEP_ALIVE"] == "1"
    assert environment["SURYA_INFERENCE_PORT"] == "8081"
    assert "SURYA_INFERENCE_URL" not in environment
    assert environment["LLAMA_CPP_BINARY"] == str(llama_server)


def test_failures_publish_exact_placeholder_without_automatic_retry(
    db_session: Session, course: QuercusCourse, tmp_path: Path
) -> None:
    course_root = tmp_path / "act" / "knowledge" / "quercus" / "Course"
    raw = course_root / "files" / "raw" / "archive.zip"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"zip")
    resource = QuercusSyncResource(
        course_id=course.course_id,
        resource_type="file",
        resource_id="file-1",
        content_fingerprint="v1",
        relative_path="files/raw/archive.zip",
        download_state="downloaded",
        last_seen_generation=1,
    )
    db_session.add(resource)
    db_session.commit()
    service = QuercusProcessingService(
        db_session,
        executable_resolver=lambda _name: None,
        inference_probe=lambda: False,
    )
    service.set_method(PROCESSING_MARKER)

    result = service.run()
    placeholder = course_root / "files" / "processed" / "archive.md"
    assert result["status"] == "failed"
    assert placeholder.read_text(encoding="utf-8") == FAILURE_PLACEHOLDER
    assert resource.processing_state == "failed"
    assert resource.processing_error_type == "unsupported_type"
    assert course.last_processing_status == "failed"
    assert course.failed_processing_count == 1

    service.run()
    assert placeholder.read_text(encoding="utf-8") == FAILURE_PLACEHOLDER


def test_failed_files_require_explicit_requeue_while_pending_files_resume(
    db_session: Session,
    course: QuercusCourse,
    tmp_path: Path,
    llama_cpp_directory: Path,
) -> None:
    course_root = tmp_path / "act" / "knowledge" / "quercus" / "Course"
    raw_root = course_root / "files" / "raw"
    processed_root = course_root / "files" / "processed"
    raw_root.mkdir(parents=True)
    processed_root.mkdir(parents=True)
    (raw_root / "failed.pdf").write_bytes(b"failed")
    (raw_root / "pending.pdf").write_bytes(b"pending")
    failed_output = processed_root / "failed.md"
    failed_output.write_text(FAILURE_PLACEHOLDER, encoding="utf-8")
    failed = QuercusSyncResource(
        course_id=course.course_id,
        resource_type="file",
        resource_id="failed-file",
        content_fingerprint="failed-v1",
        relative_path="files/raw/failed.pdf",
        processed_relative_path="files/processed/failed.md",
        processed_source_fingerprint="failed-v1",
        processing_state="failed",
        processing_error_type="marker_failed",
        download_state="downloaded",
        last_seen_generation=1,
    )
    pending = QuercusSyncResource(
        course_id=course.course_id,
        resource_type="file",
        resource_id="pending-file",
        content_fingerprint="pending-v1",
        relative_path="files/raw/pending.pdf",
        processing_state="pending",
        processing_error_type="interrupted",
        download_state="downloaded",
        last_seen_generation=1,
    )
    db_session.add_all([failed, pending])
    db_session.commit()
    calls: list[str] = []

    def runner(arguments, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(Path(arguments[1]).name)
        output = Path(arguments[-1]) / Path(arguments[1]).stem
        output.mkdir()
        (output / f"{output.name}.md").write_text("converted", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    service = QuercusProcessingService(
        db_session,
        executable_resolver=lambda _name: "marker_single",
        runner=runner,
        server_manager=FakeServerManager(),
    )
    service.configure(PROCESSING_MARKER, str(llama_cpp_directory))

    assert service.run()["status"] == "partial"
    assert calls == ["pending.pdf"]
    assert failed.processing_state == "failed"
    assert pending.processing_state == "succeeded"

    assert service.requeue_failed() == 1
    assert failed.processing_state == "pending"
    assert course.last_processing_status == "pending"
    assert service.run()["status"] == "succeeded"
    assert calls == ["pending.pdf", "failed.pdf"]
    assert failed.processing_state == "succeeded"
    assert failed_output.read_text(encoding="utf-8") == "converted"


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [
        ("missing_executable", "marker_unavailable"),
        ("missing_llama_cpp", "llama_cpp_unavailable"),
        ("nonzero", "marker_failed"),
        ("timeout", "timeout"),
        ("invalid_output", "invalid_output"),
    ],
)
def test_supported_file_failure_modes_are_retryable(
    db_session: Session,
    course: QuercusCourse,
    tmp_path: Path,
    llama_cpp_directory: Path,
    failure_mode: str,
    expected_error: str,
) -> None:
    course_root = tmp_path / "act" / "knowledge" / "quercus" / "Course"
    raw = course_root / "files" / "raw" / "notes.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"pdf")
    resource = QuercusSyncResource(
        course_id=course.course_id,
        resource_type="file",
        resource_id="file-1",
        content_fingerprint="v1",
        relative_path="files/raw/notes.pdf",
        download_state="downloaded",
        last_seen_generation=1,
    )
    db_session.add(resource)
    db_session.commit()

    def runner(_arguments, **_kwargs):  # noqa: ANN001, ANN202
        if failure_mode == "timeout":
            raise subprocess.TimeoutExpired("marker_single", PROCESS_TIMEOUT_SECONDS)
        return SimpleNamespace(returncode=1 if failure_mode == "nonzero" else 0)

    service = QuercusProcessingService(
        db_session,
        executable_resolver=lambda _name: None if failure_mode == "missing_executable" else "marker_single",
        runner=runner,
        inference_probe=lambda: False,
        server_manager=FakeServerManager(),
    )
    service.configure(PROCESSING_MARKER, str(llama_cpp_directory))
    if failure_mode == "missing_llama_cpp":
        (llama_cpp_directory / ("llama-server.exe" if os.name == "nt" else "llama-server")).unlink()

    service.run()

    assert resource.processing_state == "failed"
    assert resource.processing_error_type == expected_error
    assert (course_root / "files" / "processed" / "notes.md").read_text(
        encoding="utf-8"
    ) == FAILURE_PLACEHOLDER


def test_disabling_retains_output_without_managed_agent_guidance(
    db_session: Session, course: QuercusCourse, tmp_path: Path
) -> None:
    processed = (
        tmp_path
        / "act"
        / "knowledge"
        / "quercus"
        / "Course"
        / "files"
        / "processed"
        / "notes.md"
    )
    processed.parent.mkdir(parents=True)
    processed.write_text("existing", encoding="utf-8")
    service = QuercusProcessingService(db_session, inference_probe=lambda: True)
    service.set_method("none")

    assert processed.read_text(encoding="utf-8") == "existing"
    assert not (tmp_path / "act" / "AGENTS.md").exists()
    assert service.status()["status"] == "disabled"


def test_configuration_validates_and_reports_llama_cpp_installation(
    db_session: Session, llama_cpp_directory: Path, tmp_path: Path
) -> None:
    service = QuercusProcessingService(db_session, inference_probe=lambda: False)

    with pytest.raises(ValueError, match="absolute path"):
        service.configure(PROCESSING_MARKER, "relative/path")
    with pytest.raises(ValueError, match="contain llama-server.exe"):
        service.configure(PROCESSING_MARKER, str(tmp_path / "missing"))

    service.configure(PROCESSING_MARKER, str(llama_cpp_directory))

    status = service.status()
    assert status["llama_cpp_directory"] == str(llama_cpp_directory.resolve())
    assert status["llama_cpp_available"] is True
    assert status["inference_available"] is False
    assert status["inference_url"] == INFERENCE_URL
