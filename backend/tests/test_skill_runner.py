import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Skill, SkillRun
from app.services.skill_runner import SkillRunner, _final_run_outcome, _process_exit_error


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def create_skill_record(db: Session, skill_dir: Path) -> Skill:
    skill = Skill(
        name=f"skill_{len(list(skill_dir.parent.iterdir()))}",
        description="Test skill",
        status="installed",
        risk_level="low",
        manifest_path=str(skill_dir / "manifest.json"),
        installed_path=str(skill_dir),
        enabled=True,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def write_skill(
    skill_dir: Path,
    manifest_overrides: dict[str, Any] | None = None,
    skill_source: str | None = None,
    test_source: str = "def test_skill_passes():\n    assert True\n",
) -> None:
    skill_dir.mkdir()
    (skill_dir / "tests").mkdir()
    manifest = {
        "name": "demo_skill",
        "description": "A trusted local demo skill.",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "risk_level": "low",
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "codex",
        "enabled": False,
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)

    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "skill.py").write_text(
        skill_source
        or (
            "import json, sys\n"
            "payload = json.loads(sys.stdin.read() or '{}')\n"
            "print(json.dumps({'ok': True, 'input': payload}))\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "tests" / "test_skill.py").write_text(test_source, encoding="utf-8")


def run_skill(db: Session, skill_dir: Path, timeout_seconds: int = 5):
    skill = create_skill_record(db, skill_dir)
    return SkillRunner(db, timeout_seconds=timeout_seconds).run(
        skill_id=skill.id,
        skill_dir=skill_dir,
        input_json={"topic": "local"},
    )


def test_valid_manifest_passing_tests_and_valid_json_succeeds(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill_dir = tmp_path / "valid_skill"
    write_skill(skill_dir)

    run = run_skill(db_session, skill_dir)

    assert run.status == "succeeded"
    assert run.exit_code == 0
    assert run.output_json == {"ok": True, "input": {"topic": "local"}}
    assert run.started_at is not None
    assert run.ended_at is not None


def test_partial_output_is_reported_as_partial_with_failure_detail(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill_dir = tmp_path / "partial_skill"
    write_skill(
        skill_dir,
        skill_source=(
            "import json\n"
            "print(json.dumps({'status': 'partial', 'failures': ["
            "{'message': 'Codex CLI exited with code 1'}, "
            "{'message': 'Analysis unavailable'}]}))\n"
        ),
    )

    run = run_skill(db_session, skill_dir)

    assert run.status == "partial"
    assert run.exit_code == 0
    assert run.error_message == (
        "Skill reported partial output: Codex CLI exited with code 1; Analysis unavailable"
    )


def test_failed_output_is_reported_as_failed_even_with_zero_exit_code(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill_dir = tmp_path / "reported_failure_skill"
    write_skill(
        skill_dir,
        skill_source=(
            "import json\n"
            "print(json.dumps({'status': 'failed', 'failures': [{'message': 'Upstream request failed'}]}))\n"
        ),
    )

    run = run_skill(db_session, skill_dir)

    assert run.status == "failed"
    assert run.exit_code == 0
    assert run.error_message == "Skill reported failed output: Upstream request failed"


def test_failed_runtime_codex_invocation_prevents_succeeded_run_status() -> None:
    run = SkillRun(
        skill_id=1,
        status="running",
        codex_invocations_json=[{"status": "failed", "error_message": "CLI failed"}],
    )

    status, error_message = _final_run_outcome(run, {"status": "complete", "failures": []})

    assert status == "partial"
    assert error_message == "Skill completed after one or more runtime Codex calls failed"


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (126, "Skill could not be executed (exit code 126)"),
        (127, "Skill command was not found (exit code 127)"),
        (130, "Skill was interrupted by SIGINT (exit code 130)"),
        (137, "Skill was forcibly killed by SIGKILL (exit code 137)"),
        (139, "Skill crashed with a segmentation fault (exit code 139)"),
        (143, "Skill was terminated by SIGTERM (exit code 143)"),
        (9, "Skill exited with code 9"),
    ],
)
def test_process_exit_error_reports_specific_exit_code(returncode: int, expected: str) -> None:
    assert _process_exit_error(returncode) == expected


def test_invalid_manifest_blocks_run(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "invalid_manifest"
    write_skill(skill_dir, manifest_overrides={"name": "Invalid Name"})

    run = run_skill(db_session, skill_dir)

    assert run.status == "blocked"
    assert run.error_message is not None
    assert "name" in run.error_message
    assert run.exit_code is None


def test_unsupported_permissions_block_run(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "unsupported_permissions"
    write_skill(
        skill_dir,
        manifest_overrides={
            "risk_level": "medium",
            "permissions": {
                "network": [],
                "filesystem_read": ["./data"],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            }
        },
    )

    run = run_skill(db_session, skill_dir)

    assert run.status == "blocked"
    assert run.error_message == "filesystem read permissions are limited to the skill's own ./cache directory"
    assert run.exit_code is None


def test_own_cache_read_permission_is_supported(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "cache_read_skill"
    write_skill(
        skill_dir,
        manifest_overrides={
            "permissions": {
                "network": [],
                "filesystem_read": ["./cache"],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            }
        },
        skill_source=(
            "import json, sys\n"
            "from pathlib import Path\n"
            "cache = Path('cache')\n"
            "cache.mkdir(exist_ok=True)\n"
            "value = (cache / 'state.txt').read_text(encoding='utf-8') if (cache / 'state.txt').exists() else 'empty'\n"
            "print(json.dumps({'cache': value, 'input': json.loads(sys.stdin.read() or '{}')}))\n"
        ),
    )

    run = run_skill(db_session, skill_dir)

    assert run.status == "succeeded"
    assert run.output_json["cache"] == "empty"


def test_failing_skill_tests_block_entrypoint(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "failing_tests"
    write_skill(skill_dir, test_source="def test_skill_fails():\n    assert False\n")

    run = run_skill(db_session, skill_dir)

    assert run.status == "blocked"
    assert run.error_message == "Skill tests failed; entrypoint was not run"
    assert run.exit_code != 0


def test_invalid_json_stdout_fails_run(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "invalid_json"
    write_skill(skill_dir, skill_source="print('not json')\n")

    run = run_skill(db_session, skill_dir)

    assert run.status == "failed"
    assert run.error_message is not None
    assert "not valid JSON" in run.error_message
    assert run.stdout == "not json\n"


def test_skill_timeout_fails_run(tmp_path: Path, db_session: Session) -> None:
    skill_dir = tmp_path / "timeout_skill"
    write_skill(
        skill_dir,
        skill_source="import time\n"
        "time.sleep(10)\n"
        "print('{\"ok\": true}')\n",
    )

    run = run_skill(db_session, skill_dir, timeout_seconds=3)

    assert run.status == "failed"
    assert run.error_message == "Skill timed out after 3 seconds"
