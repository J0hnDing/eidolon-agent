import json
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
from app.schemas.manifest import SkillManifest
from app.services.github_provider import FakeGitHubProviderAdapter
from app.services.integration_service import IntegrationCaller, IntegrationError, IntegrationService
from app.services.secret_store import FakeSecretStore

SENTINEL = "EIDOLON_GITHUB_SENTINEL_7e9525f4"


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
    operations: list[str] | None = None,
    repositories: list[str] | None = None,
) -> dict:
    return {
        "provider": "github",
        "operations": operations or ["github.repository.get"],
        "resource_scope": {"repositories": repositories if repositories is not None else ["octo/demo"]},
        "reason": "Read approved repository data.",
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
            requested_permissions_json=manifest.permissions.model_dump(mode="json"),
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
