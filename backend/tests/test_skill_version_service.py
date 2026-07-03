import json
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import ApprovalRequest, Skill, SkillVersion
from app.services.agent_workflow_service import AgentWorkflowService
from app.services.codex_service import CodexService, FakeCodexAdapter
from app.services.permission_service import PermissionService
from app.services.skill_version_service import SkillVersionError, SkillVersionService


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


def write_installed_skill(project_root: Path, name: str = "versioned_skill") -> Path:
    skill_dir = project_root / "skills" / "installed" / name
    tests_dir = skill_dir / "tests"
    tests_dir.mkdir(parents=True)
    manifest = {
        "name": name,
        "description": "Versioned skill",
        "skill_type": "automation",
        "interface_type": "chat",
        "entrypoint": "skill.py",
        "instructions_path": None,
        "input_schema": None,
        "output_schema": None,
        "tool_ui_schema": None,
        "dependencies": [],
        "risk_level": "low",
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "test",
        "enabled": True,
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "README.md").write_text("# Active version\n", encoding="utf-8")
    (skill_dir / "skill.py").write_text(
        "import json\nimport sys\npayload = json.loads(sys.stdin.read() or '{}')\nprint(json.dumps({'ok': True, 'input': payload}))\n",
        encoding="utf-8",
    )
    (tests_dir / "test_skill.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return skill_dir


def create_installed_skill(db: Session, project_root: Path, name: str = "versioned_skill") -> Skill:
    skill_dir = write_installed_skill(project_root, name)
    skill = Skill(
        name=name,
        description="Versioned skill",
        skill_type="automation",
        interface_type="chat",
        status="installed",
        risk_level="low",
        manifest_path=skill_dir.relative_to(project_root).as_posix() + "/manifest.json",
        installed_path=skill_dir.relative_to(project_root).as_posix(),
        enabled=True,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


def approve_runtime(db: Session, skill: Skill, project_root: Path) -> None:
    permission_service = PermissionService(db, project_root=project_root)
    request = permission_service.create_runtime_request(skill)
    permission_service.approve_request(request)


def test_creating_version_from_active_copy_does_not_modify_active(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = SkillVersionService(db_session, project_root=tmp_path)

    active = service.ensure_active_version(skill)
    draft = service.create_draft_from_active(skill, "Improve README")

    active_readme = (tmp_path / active.folder_path / "README.md").read_text(encoding="utf-8")
    draft_readme = (tmp_path / draft.folder_path / "README.md").read_text(encoding="utf-8")

    assert active.version == "v1"
    assert draft.version == "v2"
    assert active_readme == "# Active version\n"
    assert draft_readme == "# Active version\n"
    assert skill.installed_path == active.folder_path


def test_max_three_versions_enforced(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = SkillVersionService(db_session, project_root=tmp_path)
    service.ensure_active_version(skill)
    service.create_draft_from_active(skill, "v2")
    service.create_draft_from_active(skill, "v3")

    with pytest.raises(SkillVersionError, match="Maximum 3 versions"):
        service.create_draft_from_active(skill, "v4")


def test_activating_new_version_switches_active_pointer(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = SkillVersionService(db_session, project_root=tmp_path)
    active = service.ensure_active_version(skill)
    draft = service.create_draft_from_active(skill, "Ready update")
    validation = service.validate_version(draft)

    activated = service.activate_version(skill, draft)

    db_session.refresh(active)
    assert validation.ok is True
    assert activated.active_version_id == draft.id
    assert activated.installed_path == draft.folder_path
    assert draft.status == "active"
    assert active.status == "archived"


def test_discarding_draft_does_not_affect_active_version(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = SkillVersionService(db_session, project_root=tmp_path)
    active = service.ensure_active_version(skill)
    draft = service.create_draft_from_active(skill, "Discard me")
    draft_folder = tmp_path / draft.folder_path

    service.discard_version(skill, draft)

    db_session.refresh(skill)
    assert skill.active_version_id == active.id
    assert (tmp_path / active.folder_path).exists()
    assert not draft_folder.exists()
    assert draft.status == "discarded"


def test_unchanged_permissions_skip_runtime_reapproval(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = SkillVersionService(db_session, project_root=tmp_path)
    service.ensure_active_version(skill)
    draft = service.create_draft_from_active(skill, "No permission change")
    service.validate_version(draft)

    request = service.create_runtime_request_if_needed(skill, draft)

    assert request is None


def test_changed_permissions_require_approval_before_activation(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = SkillVersionService(db_session, project_root=tmp_path)
    service.ensure_active_version(skill)
    draft = service.create_draft_from_active(skill, "Add network")
    manifest_path = tmp_path / draft.folder_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["permissions"]["network"] = ["example.com"]
    manifest["risk_level"] = "medium"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    service.validate_version(draft)

    with pytest.raises(SkillVersionError, match="Runtime permission approval"):
        service.activate_version(skill, draft)

    request = db_session.query(ApprovalRequest).filter(ApprovalRequest.skill_id == skill.id).one()
    assert request.status == "pending"
    assert request.requested_network_domains_json == ["example.com"]


def test_approved_changed_permissions_allow_activation(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = SkillVersionService(db_session, project_root=tmp_path)
    service.ensure_active_version(skill)
    draft = service.create_draft_from_active(skill, "Add network")
    manifest_path = tmp_path / draft.folder_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["permissions"]["network"] = ["example.com"]
    manifest["risk_level"] = "medium"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    service.validate_version(draft)
    request = service.create_runtime_request_if_needed(skill, draft)
    assert request is not None
    PermissionService(db_session, project_root=tmp_path).approve_request(request)

    activated = service.activate_version(skill, draft)

    assert activated.active_version_id == draft.id


def test_pm_blocks_unrealistic_suggestion(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)

    class BlockingProductManagerAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict):
            if plan.get("codex_task") == "product_manager_update_review":
                payload = {
                    "decision": "ask_user_for_input",
                    "summary": "This suggestion is too broad or unrealistic for a bounded skill update.",
                    "blueprint": {
                        "goal": "Do not build this update.",
                        "skill_name": skill.name,
                        "skill_type": skill.skill_type,
                        "interface_type": skill.interface_type,
                        "suggestion": plan["suggestion"],
                        "milestones": [],
                    },
                }
                return subprocess.CompletedProcess(args=["fake"], returncode=0, stdout=json.dumps(payload), stderr="")
            return super().generate(prompt, output_dir, plan)

    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=BlockingProductManagerAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    )

    agent_run = service.create_update_run(skill, "Make it sentient and guarantee perfect results")

    assert agent_run.status == "blocked"
    assert agent_run.error_message is None
    assert "too broad or unrealistic" in (agent_run.summary or "")


def test_pm_can_propose_better_solution_for_broad_request(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)

    class BetterSolutionProductManagerAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict):
            if plan.get("codex_task") == "product_manager_update_review":
                payload = {
                    "decision": "ask_user_for_input",
                    "summary": "A better next project is a small, testable behavior change with clear input and output.",
                    "blueprint": {
                        "goal": "Ask for a narrower update.",
                        "skill_name": skill.name,
                        "skill_type": skill.skill_type,
                        "interface_type": skill.interface_type,
                        "suggestion": plan["suggestion"],
                        "milestones": [],
                    },
                }
                return subprocess.CompletedProcess(args=["fake"], returncode=0, stdout=json.dumps(payload), stderr="")
            return super().generate(prompt, output_dir, plan)

    agent_run = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=BetterSolutionProductManagerAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    ).create_update_run(skill, "do everything")

    assert agent_run.status == "blocked"
    assert agent_run.error_message is None
    assert "better next project" in (agent_run.summary or "")


def test_update_workflow_creates_blueprint_and_builder_edits_only_new_version(
    tmp_path: Path,
    db_session: Session,
) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    service = AgentWorkflowService(db_session, project_root=tmp_path)

    agent_run = service.create_update_run(skill, "Add a clearer README explanation")
    versions = SkillVersionService(db_session, project_root=tmp_path).list_versions(skill)
    draft = next(version for version in versions if version.version == "v2")

    active = db_session.get(SkillVersion, skill.active_version_id)
    assert active is not None
    assert agent_run.blueprint_json is not None
    assert agent_run.status == "succeeded"
    assert "Proposed Update" in (tmp_path / draft.folder_path / "README.md").read_text(encoding="utf-8")
    assert "Proposed Update" not in (tmp_path / active.folder_path / "README.md").read_text(encoding="utf-8")


def test_update_product_manager_review_uses_codex_adapter(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)

    class RecordingAdapter(FakeCodexAdapter):
        def __init__(self) -> None:
            self.tasks: list[str] = []

        def generate(self, prompt: str, output_dir: Path, plan: dict):
            task = plan.get("codex_task")
            if task:
                self.tasks.append(task)
            return super().generate(prompt, output_dir, plan)

    adapter = RecordingAdapter()
    AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=adapter, project_root=tmp_path),
        project_root=tmp_path,
    ).create_update_run(skill, "Add a clearer README explanation")

    assert "product_manager_update_review" in adapter.tasks
    assert "product_manager_summary" in adapter.tasks


def test_update_request_permission_waits_and_resumes_after_approval(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)

    class PermissionFirstAdapter(FakeCodexAdapter):
        def generate(self, prompt: str, output_dir: Path, plan: dict):
            if plan.get("codex_task") == "product_manager_update_review":
                payload = {
                    "decision": "request_permission",
                    "summary": "This update is valid, but build-time internet research needs approval first.",
                    "blueprint": {
                        "goal": "Add public documentation lookup support.",
                        "skill_name": skill.name,
                        "skill_type": skill.skill_type,
                        "interface_type": skill.interface_type,
                        "suggestion": plan["suggestion"],
                        "requested_network_domains": ["docs.python.org"],
                        "requested_dependencies": [],
                        "requested_permissions": {
                            "network": ["docs.python.org"],
                            "filesystem_read": [],
                            "filesystem_write": [],
                            "secrets": [],
                            "shell": False,
                        },
                        "milestones": [
                            {
                                "name": "update_version",
                                "summary": "Create the approved draft update.",
                                "acceptance_criteria": ["draft version tests pass"],
                            }
                        ],
                    },
                }
                return subprocess.CompletedProcess(args=["fake"], returncode=0, stdout=json.dumps(payload), stderr="")
            return super().generate(prompt, output_dir, plan)

    service = AgentWorkflowService(
        db_session,
        codex_service=CodexService(db_session, adapter=PermissionFirstAdapter(), project_root=tmp_path),
        project_root=tmp_path,
    )

    agent_run = service.create_update_run(skill, "Let the update look at Python docs online")
    request = db_session.query(ApprovalRequest).filter_by(
        skill_id=skill.id,
        request_scope="build_time",
        request_type="update",
    ).one()

    assert agent_run.status == "waiting_for_approval"
    assert agent_run.error_message is None
    assert request.status == "pending"
    assert request.requested_network_domains_json == ["docs.python.org"]

    PermissionService(db_session, project_root=tmp_path).approve_request(request)
    service.resume_run(agent_run)

    db_session.refresh(agent_run)
    versions = SkillVersionService(db_session, project_root=tmp_path).list_versions(skill)
    assert agent_run.status in {"succeeded", "waiting_for_approval"}
    assert any(version.version == "v2" for version in versions)


def test_old_version_remains_runnable_after_failed_update(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)
    approve_runtime(db_session, skill, tmp_path)
    version_service = SkillVersionService(db_session, project_root=tmp_path)
    active = version_service.ensure_active_version(skill)
    draft = version_service.create_draft_from_active(skill, "Broken update")
    (tmp_path / draft.folder_path / "tests" / "test_skill.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    validation = version_service.validate_version(draft)
    decision = PermissionService(db_session, project_root=tmp_path).can_run(skill)

    assert validation.ok is False
    assert skill.active_version_id == active.id
    assert decision.allowed is True


def test_new_version_does_not_auto_activate_or_run(tmp_path: Path, db_session: Session) -> None:
    skill = create_installed_skill(db_session, tmp_path)

    AgentWorkflowService(db_session, project_root=tmp_path).create_update_run(skill, "Add README details")

    db_session.refresh(skill)
    runs = []
    versions = SkillVersionService(db_session, project_root=tmp_path).list_versions(skill)
    assert skill.active_version_id == next(version.id for version in versions if version.version == "v1")
    assert any(version.status == "proposed_update" for version in versions)
    assert runs == []
