import asyncio
import json
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.main import enforce_web_app_gateway_origin
from app.models import (
    ApprovalRequest,
    Skill,
    SkillOperationLock,
    SkillRun,
    SkillVersion,
    WebAppAuditRecord,
    WebAppInstance,
    WebAppSession,
)
from app.routers import web_apps as web_apps_router
from app.schemas.manifest import SkillManifest, manifest_permission_requests
from app.schemas.skill_codex import SkillCodexRequest
from app.services.skill_version_service import SkillVersionService
from app.services.web_app_runtime_service import (
    LaunchResult,
    PersistentWebAppLauncher,
    WebAppRuntimeConfig,
    WebAppRuntimeError,
    WebAppRuntimeService,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


class FakeLauncher:
    def __init__(self) -> None:
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.capability_token = ""

    def start(self, **kwargs) -> LaunchResult:
        self.starts.append(kwargs["instance_id"])
        self.capability_token = kwargs["capability_token"]
        return LaunchResult(runner_mode="docker", upstream_url="http://127.0.0.1:54321", container_id="container")

    def stop(self, instance: WebAppInstance) -> str:
        self.stops.append(instance.id)
        return "runtime stopped"

    def logs(self, instance: WebAppInstance) -> str:
        return "runtime log"


def config(tmp_path: Path, **overrides) -> WebAppRuntimeConfig:
    values = {
        "mode": "docker",
        "docker_image": "test-image",
        "memory_limit": "128m",
        "cpu_limit": "0.5",
        "runtime_root": tmp_path / "runtime" / "web_apps",
        "readiness_timeout_seconds": 1,
        "idle_timeout_seconds": 900,
        "session_ttl_seconds": 3600,
        "backend_origin": "http://localhost:8000",
    }
    values.update(overrides)
    return WebAppRuntimeConfig(**values)


def create_web_app(db: Session, project_root: Path, *, enabled: bool = True) -> Skill:
    skill_dir = project_root / "skills" / "installed" / "dashboard" / "versions" / "v1"
    (skill_dir / "tests").mkdir(parents=True)
    manifest = {
        "manifest_version": 1,
        "name": "dashboard",
        "description": "A self-rendered dashboard.",
        "runtime": "web_app",
        "entrypoint": "app:app",
        "instructions_path": None,
        "input_schema": None,
        "output_schema": None,
        "dependencies": [],
        "permissions": {
            "network": [],
            "filesystem_read": ["./cache"],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
            "codex": {"call_response": True, "internet_access": False},
        },
        "schedule": None,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "app.py").write_text("async def app(scope, receive, send):\n    pass\n", encoding="utf-8")
    (skill_dir / "tests" / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    skill = Skill(
        name="dashboard",
        description="A self-rendered dashboard.",
        runtime="web_app",
        status="installed",
        risk_level="low",
        manifest_path="skills/installed/dashboard/versions/v1/manifest.json",
        installed_path="skills/installed/dashboard/versions/v1",
        enabled=enabled,
    )
    db.add(skill)
    db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version="v1",
        status="active",
        folder_path="skills/installed/dashboard/versions/v1",
        manifest_json=manifest,
        code_snapshot_path="skills/installed/dashboard/versions/v1",
        permission_fingerprint="fingerprint",
        validation_status="passed",
        test_status="passed",
    )
    db.add(version)
    db.flush()
    skill.active_version_id = version.id
    db.add(
        ApprovalRequest(
            skill_id=skill.id,
            request_scope="runtime",
            request_type="install",
            risk_level="low",
            requested_permissions_json=manifest_permission_requests(
                SkillManifest.model_validate(manifest).permissions
            ),
            requested_dependencies_json=[],
            requested_network_domains_json=[],
            requested_filesystem_json={"filesystem_read": ["./cache"], "filesystem_write": ["./cache"]},
            reason_json={},
            reason="Approved runtime",
            user_explanation="Approved runtime",
            status="approved",
        )
    )
    db.commit()
    db.refresh(skill)
    return skill


def test_open_reuses_version_pinned_instance_but_creates_isolated_sessions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )

    first = service.open_session(skill)
    second = service.open_session(skill)

    assert first.instance.id == second.instance.id
    assert first.session.id != second.session.id
    assert first.session.gateway_host != second.session.gateway_host
    assert first.embed_url.endswith(".web-app.localhost:8000/")
    persisted_session = db_session.get(WebAppSession, first.session.id)
    assert persisted_session is not None
    assert persisted_session.gateway_host != first.session.gateway_host
    assert first.session.gateway_host.startswith(persisted_session.gateway_host.split(".", 1)[0] + "-")
    assert first.containment.iframe_sandbox == "allow-scripts allow-forms allow-same-origin allow-modals"
    assert launcher.starts == [first.instance.id]
    assert db_session.scalar(select(SkillOperationLock).where(SkillOperationLock.skill_id == skill.id)) is None
    assert db_session.scalars(select(SkillRun).where(SkillRun.skill_id == skill.id)).all() == []
    assert service.instance_for_capability(launcher.capability_token)[0].id == first.instance.id


def test_open_requires_enabled_web_app_with_runtime_approval(tmp_path: Path, db_session: Session) -> None:
    skill = create_web_app(db_session, tmp_path, enabled=False)
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=FakeLauncher(),
        health_checker=lambda _url, _timeout: True,
    )

    with pytest.raises(WebAppRuntimeError, match="disabled"):
        service.open_session(skill)


def test_open_requires_separate_runtime_permission_approval(tmp_path: Path, db_session: Session) -> None:
    skill = create_web_app(db_session, tmp_path)
    db_session.query(ApprovalRequest).filter(ApprovalRequest.skill_id == skill.id).delete()
    db_session.commit()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=FakeLauncher(),
        health_checker=lambda _url, _timeout: True,
    )

    with pytest.raises(WebAppRuntimeError, match="not been reviewed"):
        service.open_session(skill)


def test_stop_closes_sessions_without_holding_an_idle_operation_lock(tmp_path: Path, db_session: Session) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    opened = service.open_session(skill)

    service.stop_skill_instances(skill, "Skill disabled")

    instance = db_session.get(WebAppInstance, opened.instance.id)
    session = db_session.get(WebAppSession, opened.session.id)
    assert instance is not None
    assert session is not None
    assert instance.status == "stopped"
    assert session.status == "closed"
    assert launcher.stops == [opened.instance.id]


def test_idle_recovery_and_shutdown_cleanup_stop_instances_and_sessions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path, idle_timeout_seconds=0),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )

    idle = service.open_session(skill)
    assert service.stop_idle_instances() == 1
    assert db_session.get(WebAppInstance, idle.instance.id).status == "stopped"
    assert db_session.get(WebAppSession, idle.session.id).status == "closed"

    stale = service.open_session(skill)
    assert service.recover_stale_instances() == 1
    assert db_session.get(WebAppInstance, stale.instance.id).status == "stopped"

    shutdown = service.open_session(skill)
    assert service.shutdown_all() == 1
    assert db_session.get(WebAppInstance, shutdown.instance.id).status == "stopped"


def test_delete_removes_runtime_records_after_stopping_resources(tmp_path: Path, db_session: Session) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    opened = service.open_session(skill)

    service.delete_skill_runtime_records(skill)

    assert launcher.stops == [opened.instance.id]
    assert db_session.scalars(select(WebAppInstance).where(WebAppInstance.skill_id == skill.id)).all() == []
    assert db_session.scalars(select(WebAppSession).where(WebAppSession.skill_id == skill.id)).all() == []
    assert db_session.scalars(select(WebAppAuditRecord)).all() == []


def test_creating_update_or_repair_draft_does_not_stop_active_instance(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    runtime = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    opened = runtime.open_session(skill)

    draft = SkillVersionService(db_session, project_root=tmp_path).create_draft_from_active(
        skill,
        "Repair draft",
        created_by="repair_agent",
    )

    assert draft.status == "draft"
    assert db_session.get(WebAppInstance, opened.instance.id).status == "healthy"
    assert launcher.stops == []


def test_readiness_failure_persists_failed_instance_and_diagnostics(tmp_path: Path, db_session: Session) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: False,
    )

    with pytest.raises(WebAppRuntimeError, match="did not become ready"):
        service.open_session(skill)

    instance = db_session.scalar(select(WebAppInstance).where(WebAppInstance.skill_id == skill.id))
    assert instance is not None
    assert instance.status == "failed"
    assert "did not become ready" in (instance.error_message or "")
    assert instance.logs == "runtime stopped"


def test_open_replaces_a_persisted_healthy_instance_that_no_longer_responds(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    first = service.open_session(skill)
    health_results = iter((False, False, True))
    service.health_checker = lambda _url, _timeout: next(health_results)

    second = service.open_session(skill)

    first_instance = db_session.get(WebAppInstance, first.instance.id)
    assert first_instance is not None
    assert first_instance.status == "failed"
    assert second.instance.id != first.instance.id
    assert launcher.starts == [first.instance.id, second.instance.id]


def test_open_replaces_an_unhealthy_instance_and_closes_its_sessions(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    first = service.open_session(skill)
    first_instance = db_session.get(WebAppInstance, first.instance.id)
    assert first_instance is not None

    service.mark_unhealthy(first_instance, "Gateway failure")
    second = service.open_session(skill)

    db_session.refresh(first_instance)
    first_session = db_session.get(WebAppSession, first.session.id)
    assert first_instance.status == "failed"
    assert first_session is not None
    assert first_session.status == "closed"
    assert second.instance.id != first.instance.id
    assert launcher.stops == [first.instance.id]


def test_activating_another_version_invalidates_running_instance(tmp_path: Path, db_session: Session) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    runtime = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    opened = runtime.open_session(skill)
    running_instance = db_session.get(WebAppInstance, opened.instance.id)
    assert running_instance is not None
    running_instance.runner_mode = "local_dev"
    running_instance.container_id = None
    db_session.commit()
    active = db_session.get(SkillVersion, skill.active_version_id)
    assert active is not None
    v1_dir = tmp_path / active.folder_path
    v2_dir = v1_dir.parent / "v2"
    shutil.copytree(v1_dir, v2_dir)
    candidate = SkillVersion(
        skill_id=skill.id,
        version="v2",
        status="proposed_update",
        folder_path="skills/installed/dashboard/versions/v2",
        manifest_json=active.manifest_json,
        code_snapshot_path="skills/installed/dashboard/versions/v2",
        permission_fingerprint=active.permission_fingerprint,
        validation_status="passed",
        test_status="passed",
    )
    db_session.add(candidate)
    db_session.commit()
    db_session.refresh(candidate)

    SkillVersionService(db_session, project_root=tmp_path).activate_version(skill, candidate)

    db_session.refresh(running_instance)
    db_session.refresh(skill)
    assert running_instance.status == "stopped"
    assert skill.active_version_id == candidate.id


class FakeImageManager:
    def ensure_image(self):
        return None


def test_docker_web_app_ingress_does_not_grant_internet_egress(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def docker_runner(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(command, 1, "", "missing")
        if command[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:2] == ["docker", "port"]:
            return subprocess.CompletedProcess(command, 0, "127.0.0.1:49152\n", "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    launcher = PersistentWebAppLauncher(
        config(tmp_path),
        docker_runner=docker_runner,
        docker_available_checker=lambda: True,
        image_manager=FakeImageManager(),
    )
    skill_dir = tmp_path / "skill"
    cache_dir = tmp_path / "cache"
    skill_dir.mkdir()
    cache_dir.mkdir()
    from app.services.manifest_validator import validate_manifest

    manifest = validate_manifest(
        {
            "manifest_version": 1,
            "name": "app",
            "description": "App",
            "runtime": "web_app",
            "entrypoint": "app:app",
            "permissions": {
                "network": [],
                "filesystem_read": [],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
        }
    )

    result = launcher.start(
        instance_id="instance",
        skill_id=1,
        skill_dir=skill_dir,
        cache_dir=cache_dir,
        manifest=manifest,
        capability_token="capability",
    )

    run_commands = [command for command in commands if command[:2] == ["docker", "run"]]
    assert len(run_commands) == 2
    app_command, relay_command = run_commands
    assert app_command[app_command.index("--network") + 1] == "personal-agent-web-app-internal"
    assert "api.github.com:127.0.0.1" in app_command
    assert "github.com:127.0.0.1" in app_command
    assert "api.notion.com:127.0.0.1" in app_command
    assert "www.googleapis.com:127.0.0.1" in app_command
    assert "oauth2.googleapis.com:127.0.0.1" in app_command
    assert "gmail.googleapis.com:127.0.0.1" in app_command
    assert "api.telegram.org:127.0.0.1" in app_command
    assert "127.0.0.1::8000" not in app_command
    assert "--read-only" in app_command
    assert app_command[app_command.index("--cap-drop") + 1] == "ALL"
    assert f"{skill_dir.resolve()}:/package:ro" in app_command
    assert f"{cache_dir.resolve()}:/skill/cache:rw" in app_command
    assert "PYTHONPATH=/package:/package/.deps:/runtime" in app_command
    assert "PERSONAL_AGENT_SKILL_CACHE_DIR=/skill/cache" in app_command
    assert "PERSONAL_AGENT_BACKEND_URL=http://personal-agent-web-relay-instance:8001" in app_command
    assert relay_command[relay_command.index("--network") + 1] == "bridge"
    assert "127.0.0.1::8000" in relay_command
    assert "/runtime/web_runtime_relay.py" in relay_command
    assert [
        "docker",
        "network",
        "connect",
        "personal-agent-web-app-internal",
        "container-id",
    ] in commands
    assert result.upstream_url == "http://127.0.0.1:49152"
    assert result.relay_container_id == "container-id"

    commands.clear()
    networked_manifest = manifest.model_copy(
        update={
            "permissions": manifest.permissions.model_copy(update={"network": ["example.com"]}),
        }
    )
    launcher.start(
        instance_id="networked",
        skill_id=1,
        skill_dir=skill_dir,
        cache_dir=cache_dir,
        manifest=networked_manifest,
        capability_token="capability",
    )

    networked_app_command = next(command for command in commands if command[:2] == ["docker", "run"])
    assert networked_app_command[networked_app_command.index("--network") + 1] == (
        "personal-agent-web-app-internal"
    )
    assert "PERSONAL_AGENT_BACKEND_URL=http://personal-agent-web-relay-networked:8001" in networked_app_command
    assert networked_app_command[networked_app_command.index("--dns") + 1] == "1.1.1.1"
    assert [
        "docker",
        "network",
        "connect",
        "--gw-priority",
        "1",
        "bridge",
        "container-id",
    ] in commands


def test_local_development_fallback_uses_controlled_cache_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import web_app_runtime_service as runtime_module
    from app.services.manifest_validator import validate_manifest

    captured = {}

    class FakeProcess:
        pid = 2468

    def fake_popen(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return FakeProcess()

    monkeypatch.setattr(runtime_module.subprocess, "Popen", fake_popen)
    skill_dir = tmp_path / "installed" / "v1"
    cache_dir = tmp_path / "runtime" / "skill_cache" / "skill_1" / "cache"
    skill_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    manifest = validate_manifest(
        {
            "name": "app",
            "description": "App",
            "runtime": "web_app",
            "entrypoint": "app:app",
            "permissions": {
                "network": [],
                "filesystem_read": ["./cache"],
                "filesystem_write": ["./cache"],
                "secrets": [],
                "shell": False,
            },
        }
    )
    launcher = PersistentWebAppLauncher(config(tmp_path, mode="local"))

    result = launcher.start(
        instance_id="local-instance",
        skill_id=1,
        skill_dir=skill_dir,
        cache_dir=cache_dir,
        manifest=manifest,
        capability_token="capability",
    )

    assert result.runner_mode == "local_dev"
    assert captured["cwd"] == cache_dir.parent
    assert str(skill_dir.resolve()) in captured["env"]["PYTHONPATH"]
    assert captured["env"]["PERSONAL_AGENT_SKILL_CACHE_DIR"] == str(cache_dir.resolve())
    runtime_module._LOCAL_PROCESSES.pop("local-instance", None)


def test_gateway_applies_containment_headers_and_strips_upstream_privilege(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    opened = service.open_session(skill)

    class FakeUpstream:
        status = 200
        headers = {"Content-Type": "text/html", "Set-Cookie": "unsafe=1; Domain=.web-app.localhost"}

        def read(self, _limit: int) -> bytes:
            return b"<html><body>owned by skill</body></html>"

    class FakeOpener:
        def open(self, _request, timeout: int):
            assert timeout == 10
            return FakeUpstream()

    monkeypatch.setattr(web_apps_router, "build_opener", lambda *_args: FakeOpener())
    monkeypatch.setattr(web_apps_router, "WebAppRuntimeService", lambda _db: service)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"host", opened.session.gateway_host.encode("ascii"))],
        "client": ("127.0.0.1", 12345),
        "server": (opened.session.gateway_host, 8000),
    }
    response = asyncio.run(web_apps_router.proxy_web_app("", Request(scope, receive), db_session))

    assert response.status_code == 200
    assert response.body == b"<html><body>owned by skill</body></html>"
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["permissions-policy"].find("camera=()") >= 0
    assert "set-cookie" not in response.headers
    audit = db_session.scalars(
        select(WebAppAuditRecord).where(WebAppAuditRecord.operation == "gateway_request")
    ).all()
    assert audit[-1].request_json == {"method": "GET", "path": "/"}


def test_gateway_origin_cannot_route_to_trusted_backend_apis() -> None:
    captured_paths = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def call_next(request: Request):
        captured_paths.append(request.scope["path"])
        return Response(status_code=204)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [(b"host", b"forged.web-app.localhost")],
        "client": ("127.0.0.1", 12345),
        "server": ("forged.web-app.localhost", 80),
    }
    response = asyncio.run(enforce_web_app_gateway_origin(Request(scope, receive), call_next))

    assert response.status_code == 204
    assert captured_paths == ["/__web_app_gateway/health"]

    trusted_scope = {**scope, "path": "/__web_app_gateway/health", "raw_path": b"/__web_app_gateway/health"}
    trusted_scope["headers"] = [(b"host", b"localhost:8000")]
    response = asyncio.run(enforce_web_app_gateway_origin(Request(trusted_scope, receive), call_next))
    assert response.status_code == 404


def test_instance_capability_rejects_forged_tokens_and_records_bounded_codex_audit(
    tmp_path: Path,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = create_web_app(db_session, tmp_path)
    launcher = FakeLauncher()
    service = WebAppRuntimeService(
        db_session,
        project_root=tmp_path,
        config=config(tmp_path),
        launcher=launcher,
        health_checker=lambda _url, _timeout: True,
    )
    service.open_session(skill)
    monkeypatch.setattr(web_apps_router, "WebAppRuntimeService", lambda _db: service)

    with pytest.raises(HTTPException) as exc_info:
        web_apps_router.web_app_codex_capability(
            SkillCodexRequest(prompt="forged"),
            "Bearer not-the-instance-token",
            db_session,
        )
    assert exc_info.value.status_code == 401

    response = web_apps_router.web_app_codex_capability(
        SkillCodexRequest(prompt="Summarize this", context={"item": "demo"}),
        f"Bearer {launcher.capability_token}",
        db_session,
    )
    assert response.response == "Deterministic Codex response."
    audit = db_session.scalars(
        select(WebAppAuditRecord).where(WebAppAuditRecord.operation == "codex_call")
    ).all()
    assert audit[-1].request_json == {"prompt_characters": 14, "internet_access": False}
    assert "prompt" not in audit[-1].request_json
