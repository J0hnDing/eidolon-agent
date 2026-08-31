import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    ApprovalRequest,
    IntegrationAuditRecord,
    IntegrationAuthorization,
    IntegrationConnection,
    Skill,
    SkillVersion,
)
from app.schemas.manifest import SkillManifest, manifest_permission_requests
from app.services.atlas_provider import FakeAtlasProviderAdapter
from app.services.github_provider import FakeGitHubProviderAdapter
from app.services.integration_service import IntegrationCaller, IntegrationError, IntegrationService
from app.services.permission_service import PermissionService
from app.services.report_service import FakeReportProvider
from app.services.secret_store import FakeSecretStore
from app.services.todo_service import FakeTodoProvider

SENTINEL = "EIDOLON_GITHUB_SENTINEL_7e9525f4"
ATLAS_SENTINEL = "ATLAS_KEY_SENTINEL_4221"
NOTION_SENTINEL = "EIDOLON_NOTION_SENTINEL_90b7d"


@pytest.fixture
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def integration_requirement(
    *,
    provider: str = "github",
    operations: list[str] | None = None,
    repositories: list[str] | None = None,
) -> dict:
    return {
        "provider": provider,
        "operations": operations or (["github.repository.get"] if provider == "github" else ["notion.todo.list"]),
        "resource_scope": {
            "repositories": repositories if repositories is not None else (["octo/demo"] if provider == "github" else [])
        },
    }


def create_installed_skill(
    db: Session,
    root: Path,
    *,
    runtime: str = "function",
    requirement: dict | None = None,
) -> tuple[Skill, SkillManifest]:
    requirement = requirement or integration_requirement()
    skill_dir = root / "skills" / "installed" / "github_reader" / "versions" / "v1"
    (skill_dir / "tests").mkdir(parents=True)
    entrypoint = "app:app" if runtime == "web_app" else "skill.py"
    (skill_dir / ("app.py" if runtime == "web_app" else "skill.py")).write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n" if runtime == "web_app" else "print('{}')\n",
        encoding="utf-8",
    )
    manifest_json = {
        "manifest_version": 1,
        "name": "github_reader",
        "description": "Read approved GitHub data.",
        "runtime": runtime,
        "entrypoint": entrypoint,
        "input_schema": {"type": "object"} if runtime == "function" else None,
        "output_schema": {"type": "object"} if runtime == "function" else None,
        "function_requirements": [],
        "integration_requirements": [requirement],
        "dependencies": [],
        "permissions": {
            "network": [],
            "filesystem_read": [],
            "filesystem_write": [],
            "secrets": [],
            "shell": False,
        },
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest_json), encoding="utf-8")
    manifest = SkillManifest.model_validate(manifest_json)
    skill = Skill(
        name="github_reader",
        description=manifest.description,
        runtime=runtime,
        status="installed",
        risk_level="low",
        manifest_path="skills/installed/github_reader/versions/v1/manifest.json",
        input_schema_json=manifest.input_schema,
        output_schema_json=manifest.output_schema,
        integration_requirements_json=[
            item.model_dump(mode="json") for item in manifest.integration_requirements
        ],
        installed_path="skills/installed/github_reader/versions/v1",
        enabled=True,
    )
    db.add(skill)
    db.flush()
    version = SkillVersion(
        skill_id=skill.id,
        version="v1",
        status="active",
        folder_path=skill.installed_path,
        manifest_json=manifest.model_dump(mode="json"),
        code_snapshot_path=skill.installed_path,
        permission_fingerprint="runtime-approved",
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
            requested_permissions_json=manifest_permission_requests(manifest.permissions),
            reason="Runtime permissions approved.",
            user_explanation="Runtime permissions approved.",
            status="approved",
        )
    )
    db.commit()
    return skill, manifest


def authorize(
    service: IntegrationService,
    skill: Skill,
    manifest: SkillManifest,
) -> IntegrationAuthorization:
    authorization = service.ensure_authorization_requests(
        skill,
        manifest,
        version_id=skill.active_version_id,
    )[0]
    authorization.approval_request.status = "approved"
    service.db.commit()
    return authorization


def test_runtime_bundle_approval_resolves_base_and_integration_together(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, _ = create_installed_skill(db, tmp_path)
    permission_service = PermissionService(db, project_root=tmp_path)
    request = permission_service.create_runtime_request(skill)
    assert request.status == "approved"
    assert request.reason_json["integration_requirements"][0]["authorization_state"] == "pending"

    approved = permission_service.approve_runtime_bundle(skill)

    assert approved.status == "approved"
    assert approved.reason_json["integration_requirements"][0]["authorization_state"] == "approved"
    integration_requests = db.scalars(
        select(ApprovalRequest).where(ApprovalRequest.request_type == "integration_access")
    ).all()
    assert [item.status for item in integration_requests] == ["approved"]


def test_runtime_bundle_denial_resolves_every_pending_component(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, _ = create_installed_skill(db, tmp_path)
    permission_service = PermissionService(db, project_root=tmp_path)
    permission_service.create_runtime_request(skill)

    denied = permission_service.deny_runtime_bundle(skill)

    assert denied.status == "approved"
    assert denied.reason_json["integration_requirements"][0]["authorization_state"] == "denied"


def connected_service(
    db: Session,
    root: Path,
    *,
    store: FakeSecretStore | None = None,
    provider: FakeGitHubProviderAdapter | None = None,
) -> IntegrationService:
    service = IntegrationService(
        db,
        project_root=root,
        secret_store=store or FakeSecretStore(),
        github=provider or FakeGitHubProviderAdapter(),
    )
    service.put_github_connection(SENTINEL)
    return service


def test_add_replace_inspect_remove_and_failed_replacement_preserves_prior(
    db: Session,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    provider = FakeGitHubProviderAdapter(login="first", account_id="1")
    service = IntegrationService(db, project_root=tmp_path, secret_store=store, github=provider)

    status = service.put_github_connection(SENTINEL)
    connection = db.scalar(select(IntegrationConnection))
    assert status.connected is True
    assert status.account_login == "first"
    assert connection is not None
    assert connection.secret_reference != SENTINEL
    assert SENTINEL not in json.dumps(status.model_dump(mode="json"), default=str)

    old_reference = connection.secret_reference
    provider.error_type = "invalid_credential"
    with pytest.raises(IntegrationError, match="invalid or revoked"):
        service.put_github_connection("invalid-replacement")
    db.refresh(connection)
    assert connection.secret_reference == old_reference
    assert store.get(old_reference) == SENTINEL

    provider.error_type = None
    provider.login = "replacement"
    service.put_github_connection("replacement-value")
    db.refresh(connection)
    assert connection.account_login == "replacement"
    assert connection.secret_reference != old_reference
    assert old_reference not in store.values

    replacement_reference = connection.secret_reference
    store.fail_put = True
    with pytest.raises(IntegrationError) as store_failure:
        service.put_github_connection("store-failed-replacement")
    assert store_failure.value.error_type == "connection_unavailable"
    db.refresh(connection)
    assert connection.secret_reference == replacement_reference
    store.fail_put = False

    service.remove_github_connection()
    assert db.scalar(select(IntegrationConnection)) is None
    assert service.connection_status().status == "disconnected"


def test_secret_store_failures_fail_closed(db: Session, tmp_path: Path) -> None:
    unavailable = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=None,
        github=FakeGitHubProviderAdapter(),
    )
    with pytest.raises(IntegrationError) as exc_info:
        unavailable.put_github_connection(SENTINEL)
    assert exc_info.value.error_type == "connection_unavailable"

    store = FakeSecretStore(fail_put=True)
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        github=FakeGitHubProviderAdapter(),
    )
    with pytest.raises(IntegrationError) as exc_info:
        service.put_github_connection(SENTINEL)
    assert exc_info.value.error_type == "connection_unavailable"
    assert db.scalar(select(IntegrationConnection)) is None


def test_account_identity_change_invalidates_authorization(db: Session, tmp_path: Path) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    provider = FakeGitHubProviderAdapter(login="first", account_id="1")
    service = connected_service(db, tmp_path, provider=provider)
    authorization = authorize(service, skill, manifest)

    provider.login = "second"
    provider.account_id = "2"
    service.put_github_connection("replacement")
    db.refresh(authorization)
    assert authorization.invalidated_at is not None
    assert authorization.approval_request.status == "superseded"


def test_notion_connection_is_atomic_secret_free_and_coexists_with_github_and_atlas_rows(
    db: Session,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    notion = FakeTodoProvider()
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        github=FakeGitHubProviderAdapter(),
        notion_provider_factory=lambda _token, _source: notion,
        notion_report_provider_factory=lambda _token, _source: FakeReportProvider(),
    )
    service.put_github_connection(SENTINEL)
    db.add(
        IntegrationConnection(
            provider="atlas",
            secret_store_id=store.implementation_id,
            secret_reference="atlas-native",
            credential_kind="native",
            status="connected",
            account_login="local",
            account_id="local-atlas",
            last_validated_at=db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "github")).last_validated_at,
        )
    )
    db.commit()

    status = service.put_notion_connection(NOTION_SENTINEL, "source-id", "report-source-id")

    assert status.connected is True
    assert status.bot_name == "Fake Notion bot"
    assert status.workspace_name == "Fake workspace"
    assert status.data_source_id == "source-id"
    assert status.report_data_source_id == "report-source-id"
    connections = db.scalars(select(IntegrationConnection).order_by(IntegrationConnection.provider)).all()
    assert [item.provider for item in connections] == ["atlas", "github", "notion"]
    notion_row = next(item for item in connections if item.provider == "notion")
    assert notion_row.secret_reference != NOTION_SENTINEL
    assert store.namespaces[notion_row.secret_reference] == "notion"
    for table_name in [
        row[0]
        for row in db.connection().exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]:
        assert NOTION_SENTINEL not in repr(
            db.connection().exec_driver_sql(f'SELECT * FROM "{table_name}"').fetchall()
        )

    old_reference = notion_row.secret_reference

    class RejectedProvider(FakeTodoProvider):
        def validate_connection(self):
            from app.services.github_provider import IntegrationProviderError

            raise IntegrationProviderError("invalid_credential", "sentinel must not escape")

    service.notion_provider_factory = lambda _token, _source: RejectedProvider()
    with pytest.raises(IntegrationError) as rejected:
        service.put_notion_connection("failed-replacement", "other-source", "other-report-source")
    assert rejected.value.error_type == "invalid_credential"
    db.refresh(notion_row)
    assert notion_row.secret_reference == old_reference
    assert notion_row.configured_resource_id == "source-id"

    service.notion_provider_factory = lambda _token, _source: notion
    service.remove_notion_connection()
    assert db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "notion")) is None
    assert old_reference not in store.values


def test_notion_identity_change_invalidates_authorization(
    db: Session,
    tmp_path: Path,
) -> None:
    requirement = integration_requirement(provider="notion", operations=["notion.todo.list"])
    skill, manifest = create_installed_skill(db, tmp_path, requirement=requirement)
    store = FakeSecretStore()
    notion = FakeTodoProvider(bot_id="first-bot")
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        notion_provider_factory=lambda _token, _source: notion,
        notion_report_provider_factory=lambda _token, _source: FakeReportProvider(bot_id=notion.bot_id),
    )
    service.put_notion_connection(NOTION_SENTINEL, "source-id", "report-source-id")
    authorization = authorize(service, skill, manifest)
    assert authorization.approval_request.risk_level == "low"

    notion.bot_id = "second-bot"
    service.put_notion_connection(
        "replacement",
        "source-id",
        "report-source-id",
    )

    db.refresh(authorization)
    assert authorization.invalidated_at is not None
    assert authorization.approval_request.status == "superseded"


def test_notion_data_source_changes_preserve_authorization(
    db: Session,
    tmp_path: Path,
) -> None:
    requirement = integration_requirement(provider="notion", operations=["notion.todo.list"])
    skill, manifest = create_installed_skill(db, tmp_path, requirement=requirement)
    store = FakeSecretStore()
    notion = FakeTodoProvider(bot_id="shared-bot")
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        notion_provider_factory=lambda _token, _source: notion,
        notion_report_provider_factory=lambda _token, _source: FakeReportProvider(bot_id=notion.bot_id),
    )
    service.put_notion_connection(NOTION_SENTINEL, "source-id", "report-source-id")
    authorization = authorize(service, skill, manifest)

    service.put_notion_data_sources("other-source", "other-report-source")
    service.remove_notion_data_sources()
    assert service.operation_available("notion.todo.list") is False
    service.put_notion_data_sources("restored-source", "restored-report-source")

    db.refresh(authorization)
    assert authorization.invalidated_at is None
    assert authorization.approval_request.status == "approved"
    assert service.operation_available("notion.todo.list") is True


def test_notion_credential_and_data_sources_are_saved_and_removed_separately(
    db: Session,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    notion = FakeTodoProvider(bot_id="shared-bot")
    reports = FakeReportProvider(bot_id="shared-bot")
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        notion_provider_factory=lambda _token, _source: notion,
        notion_report_provider_factory=lambda _token, _source: reports,
    )

    connected = service.put_notion_credential(NOTION_SENTINEL)
    assert connected.connected is True
    assert connected.data_source_id is None
    assert connected.report_data_source_id is None
    assert notion.calls == [("validate_identity", {})]

    configured = service.put_notion_data_sources("todo-source", "report-source")
    assert configured.data_source_id == "todo-source"
    assert configured.report_data_source_id == "report-source"
    reference = service._connection("notion").secret_reference  # noqa: SLF001

    replaced = service.put_notion_credential("replacement-token")
    assert replaced.data_source_id == "todo-source"
    assert replaced.report_data_source_id == "report-source"
    assert service._connection("notion").secret_reference != reference  # noqa: SLF001

    cleared = service.remove_notion_data_sources()
    assert cleared.connected is True
    assert cleared.data_source_id is None
    assert cleared.report_data_source_id is None
    assert service.operation_available("notion.todo.list") is False
    assert service.operation_available("notion.report.list") is False


def test_failed_separate_data_source_validation_preserves_both_existing_ids(
    db: Session,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        notion_provider_factory=lambda _token, _source: FakeTodoProvider(),
        notion_report_provider_factory=lambda _token, _source: FakeReportProvider(),
    )
    service.put_notion_credential(NOTION_SENTINEL)
    service.put_notion_data_sources("todo-source", "report-source")

    class RejectedReportProvider(FakeReportProvider):
        def validate_connection(self):
            from app.services.github_provider import IntegrationProviderError

            raise IntegrationProviderError("not_found", "missing report source")

    service.notion_report_provider_factory = lambda _token, _source: RejectedReportProvider()
    with pytest.raises(IntegrationError) as rejected:
        service.put_notion_data_sources("other-todo", "other-report")

    assert rejected.value.error_type == "not_found"
    message = str(rejected.value)
    assert "Reports data source" in message
    assert "Copy its data source ID" in message
    assert "share the original database" in message
    assert "todo" not in message.lower()
    status = service.notion_connection_status()
    assert status.data_source_id == "todo-source"
    assert status.report_data_source_id == "report-source"


def test_notion_todo_operations_use_existing_authorization_audit_and_fake_provider(
    db: Session,
    tmp_path: Path,
) -> None:
    requirement = integration_requirement(
        provider="notion",
        operations=["notion.todo.list", "notion.todo.create", "notion.todo.update", "notion.todo.delete"],
    )
    skill, manifest = create_installed_skill(db, tmp_path, requirement=requirement)
    store = FakeSecretStore()
    notion = FakeTodoProvider()
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        notion_provider_factory=lambda _token, _source: notion,
        notion_report_provider_factory=lambda _token, _source: FakeReportProvider(),
    )
    service.put_notion_connection(NOTION_SENTINEL, "source-id", "report-source-id")
    authorization = authorize(service, skill, manifest)
    assert authorization.approval_request.risk_level == "medium"
    caller = IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function")

    created = service.invoke(caller, "notion.todo.create", {"title": "Title only"})
    updated = service.invoke(
        caller,
        "notion.todo.update",
        {"id": created["id"], "done": True, "notes": None},
    )
    listed = service.invoke(caller, "notion.todo.list", {"page_size": 1})
    removed = service.invoke(caller, "notion.todo.delete", {"id": created["id"]})

    assert updated["notes"] is None
    assert updated["done"] is True
    assert listed["todos"][0]["done"] is True
    assert listed["todos"][0]["id"] == created["id"]
    assert removed == {"id": created["id"], "removed": True}
    audits = db.scalars(select(IntegrationAuditRecord).order_by(IntegrationAuditRecord.id)).all()
    assert [audit.operation_id for audit in audits] == [
        "notion.todo.create",
        "notion.todo.update",
        "notion.todo.list",
        "notion.todo.delete",
    ]
    assert audits[1].resource == f"notion-page:{created['id']}"
    assert NOTION_SENTINEL not in repr([(audit.resource, audit.error_type) for audit in audits])


def test_notion_report_operations_use_separate_contained_source_and_raw_blocks(
    db: Session,
    tmp_path: Path,
) -> None:
    requirement = integration_requirement(
        provider="notion",
        operations=[
            "notion.report.list",
            "notion.report.get",
            "notion.report.create",
            "notion.report.delete",
        ],
    )
    skill, manifest = create_installed_skill(db, tmp_path, requirement=requirement)
    store = FakeSecretStore()
    reports = FakeReportProvider()
    report_sources: list[str] = []

    def report_factory(_token: str, source: str) -> FakeReportProvider:
        report_sources.append(source)
        return reports

    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        notion_provider_factory=lambda _token, _source: FakeTodoProvider(),
        notion_report_provider_factory=report_factory,
    )
    service.put_notion_connection(NOTION_SENTINEL, "todo-source", "report-source")
    authorization = authorize(service, skill, manifest)
    assert authorization.approval_request.risk_level == "medium"
    caller = IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function")
    children = [{"type": "divider", "divider": {}}]

    created = service.invoke(
        caller,
        "notion.report.create",
        {"name": "Weekly report", "select": "GitHub Projects", "children": children},
    )
    listed = service.invoke(caller, "notion.report.list", {"page_size": 10})
    fetched = service.invoke(caller, "notion.report.get", {"id": created["id"]})
    removed = service.invoke(caller, "notion.report.delete", {"id": created["id"]})

    assert listed["reports"] == [created]
    assert fetched["report"] == created
    assert fetched["blocks"] == children
    assert removed == {"id": created["id"], "removed": True}
    assert report_sources and set(report_sources) == {"report-source"}
    audits = db.scalars(select(IntegrationAuditRecord).order_by(IntegrationAuditRecord.id)).all()
    assert [audit.operation_id for audit in audits] == [
        "notion.report.create",
        "notion.report.list",
        "notion.report.get",
        "notion.report.delete",
    ]
    assert audits[2].resource == f"notion-page:{created['id']}"


def test_legacy_todo_only_notion_connection_leaves_report_operations_unavailable(
    db: Session,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    service = IntegrationService(db, project_root=tmp_path, secret_store=store)
    reference = store.put(NOTION_SENTINEL, namespace="notion")
    db.add(
        IntegrationConnection(
            provider="notion",
            secret_store_id=store.implementation_id,
            secret_reference=reference,
            status="connected",
            account_login="Notion bot",
            account_id="bot-id",
            configured_resource_id="todo-source",
            configured_report_resource_id=None,
            last_validated_at=datetime.now(UTC),
        )
    )
    db.commit()

    status = service.notion_connection_status()

    assert status.connected is True
    assert status.data_source_id == "todo-source"
    assert status.report_data_source_id is None
    assert service.operation_available("notion.todo.list") is True
    assert service.operation_available("notion.report.list") is False

    skill, manifest = create_installed_skill(
        db,
        tmp_path,
        requirement=integration_requirement(
            provider="notion",
            operations=["notion.report.create"],
        ),
    )
    authorization = service.ensure_authorization_requests(skill, manifest)[0]
    assert authorization.approval_request.reason_json["connection_available"] is False


def test_declared_approved_call_is_normalized_audited_and_secret_free(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    service = connected_service(db, tmp_path)
    authorize(service, skill, manifest)

    output = service.invoke(
        IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
        "github.repository.get",
        {"owner": "octo", "repository": "demo"},
    )

    assert output["full_name"] == "octo/demo"
    audit = db.scalar(select(IntegrationAuditRecord))
    assert audit is not None
    assert audit.resource == "octo/demo"
    assert audit.status == "succeeded"
    assert audit.error_type is None
    assert SENTINEL not in json.dumps(
        {
            "resource": audit.resource,
            "operation": audit.operation_id,
            "status": audit.status,
            "error": audit.error_type,
        }
    )
    table_names = [
        row[0]
        for row in db.connection().exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table_name in table_names:
        rows = db.connection().exec_driver_sql(f'SELECT * FROM "{table_name}"').fetchall()
        assert SENTINEL not in repr(rows)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text(encoding="utf-8", errors="ignore")


def test_missing_authorization_connection_and_secret_are_normalized(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    store = FakeSecretStore()
    service = connected_service(db, tmp_path, store=store)
    caller = IntegrationCaller(
        skill_id=skill.id,
        version_id=skill.active_version_id,
        runtime="function",
    )

    with pytest.raises(IntegrationError) as missing_authorization:
        service.invoke(
            caller,
            "github.repository.get",
            {"owner": "octo", "repository": "demo"},
        )
    assert missing_authorization.value.error_type == "authorization_missing_or_stale"

    authorize(service, skill, manifest)
    service.remove_github_connection()
    with pytest.raises(IntegrationError) as missing_connection:
        service.invoke(
            caller,
            "github.repository.get",
            {"owner": "octo", "repository": "demo"},
        )
    assert missing_connection.value.error_type == "connection_unavailable"

    service.put_github_connection(SENTINEL)
    store.fail_get = True
    with pytest.raises(IntegrationError) as unavailable_secret:
        service.invoke(
            caller,
            "github.repository.get",
            {"owner": "octo", "repository": "demo"},
        )
    assert unavailable_secret.value.error_type == "connection_unavailable"
    audits = db.scalars(select(IntegrationAuditRecord).order_by(IntegrationAuditRecord.id)).all()
    assert [audit.error_type for audit in audits] == [
        "authorization_missing_or_stale",
        "connection_unavailable",
        "connection_unavailable",
    ]


@pytest.mark.parametrize(
    ("operation", "input_json", "expected"),
    [
        ("github.repository.file.read", {"owner": "octo", "repository": "demo", "path": "README.md"}, "operation_undeclared"),
        ("github.repository.get", {"owner": "other", "repository": "repo"}, "repository_outside_scope"),
        ("github.repository.get", {"owner": "octo"}, "invalid_input"),
        (
            "github.repository.get",
            {"owner": "octo", "repository": "demo", "credential": SENTINEL},
            "invalid_input",
        ),
    ],
)
def test_invocation_denials_are_normalized_and_safely_audited(
    db: Session,
    tmp_path: Path,
    operation: str,
    input_json: dict,
    expected: str,
) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    service = connected_service(db, tmp_path)
    authorize(service, skill, manifest)

    with pytest.raises(IntegrationError) as exc_info:
        service.invoke(
            IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
            operation,
            input_json,
        )
    assert exc_info.value.error_type == expected
    audit = db.scalar(select(IntegrationAuditRecord))
    assert audit is not None
    assert audit.status == "failed"
    assert audit.error_type == expected
    assert SENTINEL not in str(exc_info.value)


def test_trending_rejects_caller_query_construction(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, manifest = create_installed_skill(
        db,
        tmp_path,
        requirement=integration_requirement(
            operations=["github.repository.trending.list"],
            repositories=[],
        ),
    )
    service = connected_service(db, tmp_path)
    authorize(service, skill, manifest)

    with pytest.raises(IntegrationError) as exc_info:
        service.invoke(
            IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
            "github.repository.trending.list",
            {"language": "python stars:>1000"},
        )
    assert exc_info.value.error_type == "invalid_input"


@pytest.mark.parametrize("error_type", ["rate_limited", "provider_timeout", "provider_forbidden", "response_too_large"])
def test_provider_errors_are_normalized_and_safely_audited(
    db: Session,
    tmp_path: Path,
    error_type: str,
) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    provider = FakeGitHubProviderAdapter()
    service = connected_service(db, tmp_path, provider=provider)
    authorize(service, skill, manifest)
    provider.error_type = error_type

    with pytest.raises(IntegrationError) as exc_info:
        service.invoke(
            IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
            "github.repository.get",
            {"owner": "octo", "repository": "demo"},
        )
    assert exc_info.value.error_type == error_type
    audit = db.scalar(select(IntegrationAuditRecord))
    assert audit is not None
    assert audit.status == "failed"
    assert audit.error_type == error_type
    assert SENTINEL not in str(exc_info.value)


def test_removed_connection_blocks_calls_but_retains_authorization_and_audit(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    service = connected_service(db, tmp_path)
    authorization = authorize(service, skill, manifest)
    service.invoke(
        IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
        "github.repository.get",
        {"owner": "octo", "repository": "demo"},
    )
    service.remove_github_connection()

    with pytest.raises(IntegrationError) as exc_info:
        service.invoke(
            IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
            "github.repository.get",
            {"owner": "octo", "repository": "demo"},
        )
    assert exc_info.value.error_type == "connection_unavailable"
    assert db.get(IntegrationAuthorization, authorization.id) is not None
    assert db.scalar(select(IntegrationAuditRecord)) is not None


def test_unchanged_contract_reuses_authorization_but_expansion_requires_reapproval(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    service = connected_service(db, tmp_path)
    first = authorize(service, skill, manifest)

    reused = service.ensure_authorization_requests(skill, manifest)[0]
    assert reused.id == first.id
    expanded = SkillManifest.model_validate(
        {
            **manifest.model_dump(mode="json"),
            "integration_requirements": [
                integration_requirement(repositories=["octo/demo", "octo/other"])
            ],
        }
    )
    next_authorization = service.ensure_authorization_requests(skill, expanded)[0]
    assert next_authorization.id != first.id
    assert next_authorization.approval_request.status == "pending"
    db.refresh(first)
    assert first.invalidated_at is None
    assert service.authorization_state(skill, manifest.integration_requirements[0]) == "approved"
    assert service.authorization_state(skill, expanded.integration_requirements[0]) == "pending"


def test_disabled_and_stale_version_are_rejected_before_provider_call(
    db: Session,
    tmp_path: Path,
) -> None:
    skill, manifest = create_installed_skill(db, tmp_path)
    provider = FakeGitHubProviderAdapter()
    service = connected_service(db, tmp_path, provider=provider)
    authorize(service, skill, manifest)

    skill.enabled = False
    db.commit()
    with pytest.raises(IntegrationError) as disabled:
        service.invoke(
            IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
            "github.repository.get",
            {"owner": "octo", "repository": "demo"},
        )
    assert disabled.value.error_type == "authorization_missing_or_stale"

    skill.enabled = True
    db.commit()
    with pytest.raises(IntegrationError) as stale:
        service.invoke(
            IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id + 100, runtime="function"),
            "github.repository.get",
            {"owner": "octo", "repository": "demo"},
        )
    assert stale.value.error_type == "authorization_missing_or_stale"
    assert provider.calls == []


def test_direct_user_integration_path_preserves_provider_boundaries_without_skill_scope(
    db: Session,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    provider = FakeGitHubProviderAdapter()
    service = connected_service(db, tmp_path, store=store, provider=provider)

    output = service.invoke_direct(
        "github.repository.get",
        {"owner": "outside", "repository": "token-allowed"},
    )

    assert output["full_name"] == "outside/token-allowed"
    assert provider.calls == [
        ("github.repository.get", {"owner": "outside", "repository": "token-allowed"})
    ]
    store.fail_get = True
    with pytest.raises(IntegrationError) as unavailable:
        service.invoke_direct(
            "github.repository.get",
            {"owner": "outside", "repository": "token-allowed"},
        )
    assert unavailable.value.error_type == "connection_unavailable"

    with pytest.raises(IntegrationError) as invalid:
        service.invoke_direct("github.repository.get", {"owner": "invalid owner", "repository": "repo"})
    assert invalid.value.error_type == "invalid_input"
    assert len(provider.calls) == 1


def test_direct_user_notion_and_atlas_calls_reuse_configured_containment(
    db: Session,
    tmp_path: Path,
) -> None:
    store = FakeSecretStore()
    notion = FakeTodoProvider()
    configured_sources: list[str] = []

    def notion_factory(_token: str, source: str) -> FakeTodoProvider:
        configured_sources.append(source)
        return notion

    atlas = FakeAtlasProviderAdapter()
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=store,
        github=FakeGitHubProviderAdapter(),
        atlas=atlas,
        notion_provider_factory=notion_factory,
        notion_report_provider_factory=lambda _token, _source: FakeReportProvider(),
    )
    service.put_notion_connection(
        NOTION_SENTINEL,
        "only-configured-source",
        "only-configured-report-source",
    )
    service.provider_connected = lambda provider: provider == "atlas"  # type: ignore[method-assign]

    notion_output = service.invoke_direct("notion.todo.list", {"page_size": 10})
    atlas_output = service.invoke_direct("atlas.person.get", {})

    assert notion_output == {"todos": [], "has_more": False, "next_cursor": None}
    assert configured_sources[-1] == "only-configured-source"
    assert atlas_output == {"personal_info": None}
    assert atlas.calls == [("atlas.person.get", {})]


def test_atlas_know_uses_bounded_codex_and_audits_only_node_id(db: Session, tmp_path: Path) -> None:
    requirement = {
        "provider": "atlas",
        "operations": ["atlas.knowledge.node.know"],
        "resource_scope": {},
    }
    skill, manifest = create_installed_skill(db, tmp_path, requirement=requirement)
    class Codex:
        prompts: list[str] = []

        def generate(self, prompt, output_dir, plan):
            self.prompts.append(prompt)
            return subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "explanation": "A bounded explanation.",
                        "terms": [{"id": "term", "label": "Term", "definition": "Definition."}],
                        "children": ["Immediate child"],
                    }
                ),
                "",
            )

    codex = Codex()
    atlas = FakeAtlasProviderAdapter()
    service = IntegrationService(
        db,
        project_root=tmp_path,
        secret_store=None,
        atlas=atlas,
        codex_adapter=codex,
    )
    service.provider_connected = lambda provider: provider == "atlas"  # type: ignore[method-assign]
    authorize(service, skill, manifest)

    result = service.invoke(
        IntegrationCaller(skill_id=skill.id, version_id=skill.active_version_id, runtime="function"),
        "atlas.knowledge.node.know",
        {"node_id": 42},
    )

    assert result["node"]["status"] == "known"
    assert ATLAS_SENTINEL not in codex.prompts[0]
    audit = db.scalar(select(IntegrationAuditRecord))
    assert audit.resource == "node:42"
    assert audit.operation_id == "atlas.knowledge.node.know"
    assert "A bounded explanation" not in repr(audit)
