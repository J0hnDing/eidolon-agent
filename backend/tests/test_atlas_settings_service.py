import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.db as db_module
import app.services.atlas_lifecycle_service as lifecycle_module
from app.db import Base
from app.models import ApprovalRequest, IntegrationAuthorization, IntegrationConnection, Skill
from app.services.atlas_lifecycle_service import AtlasHttpResult, AtlasLifecycleError, AtlasLifecycleService
from app.services.atlas_settings_service import AtlasSettingsError, AtlasSettingsService
from app.services.secret_store import FakeSecretStore

KEY_SENTINEL = "ATLAS_KEY_SENTINEL_82d9"
PASS_SENTINEL = "ATLAS_PASSPHRASE_SENTINEL_774b"


class FakeLifecycle:
    def __init__(self):
        self.directory = Path("C:/Atlas")
        self.ownership = "owned"
        self.startup_error = None
        self.auto_unlock_attempted = False
        self.validation_error: str | None = None
        self.unlock_error: str | None = None
        self.running = True
        self.locked = False
        self.unlock_calls = 0
        self.start_calls = 0
        self.stop_calls = 0

    def status(self):
        return {"running": self.running, "initialized": True, "locked": self.locked}

    def start(self):
        self.start_calls += 1
        self.running = True
        self.ownership = "owned"

    def stop(self):
        self.stop_calls += 1
        self.running = False
        self.ownership = "none"

    def validate_directory(self, directory):
        return directory.resolve()

    def save_directory(self, directory):
        self.directory = directory.resolve()

    def validate_api_key(self, _key):
        if self.validation_error:
            raise AtlasLifecycleError(self.validation_error, "Key rejected")
        return "connected"

    def unlock(self, _passphrase):
        self.unlock_calls += 1
        if self.unlock_error:
            raise AtlasLifecycleError(self.unlock_error, "Unlock rejected")
        self.locked = False


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def service(db, *, store=None, lifecycle=None):
    return AtlasSettingsService(db, secret_store=store or FakeSecretStore(), lifecycle=lifecycle or FakeLifecycle())


def test_api_key_and_passphrase_are_separate_and_never_returned(db: Session) -> None:
    store = FakeSecretStore()
    settings = service(db, store=store)
    status = settings.put_api_key(KEY_SENTINEL)
    status = settings.put_passphrase(PASS_SENTINEL)
    connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "atlas"))

    assert status.api_key_status == "connected"
    assert status.auto_unlock_configured is True
    assert connection.secret_reference != connection.passphrase_secret_reference
    assert store.namespaces[connection.secret_reference] == "atlas_api_key"
    assert store.namespaces[connection.passphrase_secret_reference] == "atlas_passphrase"
    serialized = status.model_dump_json()
    assert KEY_SENTINEL not in serialized
    assert PASS_SENTINEL not in serialized


def test_invalid_passphrase_replacement_preserves_previous_secret(db: Session) -> None:
    store = FakeSecretStore()
    lifecycle = FakeLifecycle()
    settings = service(db, store=store, lifecycle=lifecycle)
    settings.put_api_key(KEY_SENTINEL)
    settings.put_passphrase(PASS_SENTINEL)
    connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "atlas"))
    previous_reference = connection.passphrase_secret_reference
    lifecycle.unlock_error = "invalid_passphrase"

    with pytest.raises(AtlasSettingsError, match="Unlock rejected"):
        settings.put_passphrase("bad replacement")

    db.refresh(connection)
    assert connection.passphrase_secret_reference == previous_reference
    assert store.get(previous_reference, namespace="atlas_passphrase") == PASS_SENTINEL


def test_connection_removal_cleans_both_secrets(db: Session) -> None:
    store = FakeSecretStore()
    settings = service(db, store=store)
    settings.put_api_key(KEY_SENTINEL)
    settings.put_passphrase(PASS_SENTINEL)

    settings.remove_connection()

    assert db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "atlas")) is None
    assert store.values == {}


def test_external_unlock_requires_recognized_stored_key(db: Session) -> None:
    store = FakeSecretStore()
    lifecycle = FakeLifecycle()
    settings = service(db, store=store, lifecycle=lifecycle)
    settings.put_api_key(KEY_SENTINEL)
    lifecycle.ownership = "external"
    lifecycle.validation_error = "invalid_api_key"

    with pytest.raises(AtlasSettingsError, match="identity could not be verified"):
        settings.put_passphrase(PASS_SENTINEL)

    connection = db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "atlas"))
    assert connection.passphrase_secret_reference is None


def test_lifecycle_directory_config_is_atomic_and_defaults_to_sibling(tmp_path: Path) -> None:
    project = tmp_path / "Eidolon"
    project.mkdir()
    lifecycle = AtlasLifecycleService(project_root=project)
    assert lifecycle.directory == tmp_path / "Eidolon-Atlas"

    selected = tmp_path / "Custom-Atlas"
    selected.mkdir()
    lifecycle.save_directory(selected)

    assert json.loads(lifecycle.config_path.read_text(encoding="utf-8")) == {"directory": str(selected)}
    assert not lifecycle.config_path.with_suffix(".tmp").exists()


def test_locked_external_api_key_can_be_identified_without_unlocking() -> None:
    class Client:
        def request(self, method, path, *, body=None, api_key=None):
            assert method == "GET"
            assert path == "/api/agent/tools"
            assert api_key == KEY_SENTINEL
            return AtlasHttpResult(423, {"error": {"code": "LOCKED"}})

    lifecycle = AtlasLifecycleService(client=Client())
    assert lifecycle.validate_api_key(KEY_SENTINEL) == "locked_recognized"


def test_startup_auto_unlocks_once_and_status_does_not_defeat_manual_lock(db: Session) -> None:
    store = FakeSecretStore()
    lifecycle = FakeLifecycle()
    settings = service(db, store=store, lifecycle=lifecycle)
    settings.put_api_key(KEY_SENTINEL)
    settings.put_passphrase(PASS_SENTINEL)
    lifecycle.unlock_calls = 0
    lifecycle.locked = True

    settings.startup()

    assert lifecycle.start_calls == 1
    assert lifecycle.unlock_calls == 1
    assert lifecycle.auto_unlock_attempted is True
    lifecycle.locked = True

    status = settings.status()

    assert status.locked is True
    assert lifecycle.unlock_calls == 1


def test_directory_change_clears_secrets_and_invalidates_atlas_authorization(
    db: Session, tmp_path: Path
) -> None:
    store = FakeSecretStore()
    lifecycle = FakeLifecycle()
    settings = service(db, store=store, lifecycle=lifecycle)
    settings.put_api_key(KEY_SENTINEL)
    settings.put_passphrase(PASS_SENTINEL)
    skill = Skill(
        name="atlas_reader",
        description="Atlas reader",
        runtime="function",
        status="installed",
        risk_level="low",
        manifest_path="manifest.json",
        installed_path="skills/installed/atlas_reader/versions/v1",
        enabled=True,
    )
    db.add(skill)
    db.flush()
    request = ApprovalRequest(
        skill_id=skill.id,
        request_scope="runtime",
        request_type="integration_access",
        risk_level="low",
        requested_permissions_json={},
        reason="Atlas read",
        user_explanation="Atlas read",
        status="approved",
    )
    db.add(request)
    db.flush()
    authorization = IntegrationAuthorization(
        skill_id=skill.id,
        provider="atlas",
        contract_fingerprint="fingerprint",
        approval_request_id=request.id,
    )
    db.add(authorization)
    db.commit()
    selected = (tmp_path / "Other-Atlas").resolve()

    settings.save_directory_and_restart(selected)

    assert lifecycle.directory == selected
    assert lifecycle.stop_calls == 1
    assert db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider == "atlas")) is None
    assert store.values == {}
    db.refresh(authorization)
    assert authorization.invalidated_at is not None
    assert request.status == "superseded"


def test_lifecycle_attaches_to_external_process_without_owning_or_stopping_it(tmp_path: Path) -> None:
    class Client:
        def request(self, method, path, **_kwargs):
            assert (method, path) == ("GET", "/api/status")
            return AtlasHttpResult(200, {"initialized": True, "locked": True})

    lifecycle = AtlasLifecycleService(project_root=tmp_path, client=Client())
    lifecycle.start()
    assert lifecycle.ownership == "external"
    lifecycle.stop()
    assert lifecycle.ownership == "none"


def test_lifecycle_stops_only_the_process_it_started(tmp_path: Path, monkeypatch) -> None:
    class Client:
        calls = 0

        def request(self, method, path, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise AtlasLifecycleError("atlas_unavailable", "not ready")
            return AtlasHttpResult(200, {"initialized": True, "locked": True})

    class Process:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 3
            return 0

    process = Process()
    monkeypatch.setattr(lifecycle_module.subprocess, "Popen", lambda *args, **kwargs: process)
    lifecycle = AtlasLifecycleService(project_root=tmp_path, client=Client())
    monkeypatch.setattr(lifecycle, "_validated_launch", lambda _directory: (tmp_path, "node"))

    lifecycle.start()
    assert lifecycle.ownership == "owned"
    lifecycle.stop()

    assert process.terminated is True
    assert lifecycle.ownership == "none"


def test_local_schema_adds_nullable_atlas_passphrase_references(tmp_path: Path, monkeypatch) -> None:
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-atlas.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE integration_connections ("
                "id INTEGER PRIMARY KEY, provider VARCHAR(32), secret_store_id VARCHAR(64), "
                "secret_reference VARCHAR(256), status VARCHAR(32), account_login VARCHAR(128), "
                "account_id VARCHAR(128), last_validated_at DATETIME)"
            )
        )
    monkeypatch.setattr(db_module, "engine", legacy_engine)

    db_module.ensure_local_schema()

    columns = {column["name"] for column in inspect(legacy_engine).get_columns("integration_connections")}
    assert {"passphrase_secret_store_id", "passphrase_secret_reference"} <= columns
