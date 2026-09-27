from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ActSession, TelegramBotConnection, TelegramTopicSession
from app.services.act_telegram_file_service import (
    ActTelegramFileCapability,
    ActTelegramFileError,
)
from app.services.act_workspace_service import ActWorkspace
from app.services.secret_store import FakeSecretStore
from app.services.telegram_provider import FakeTelegramBotApi
from app.services.telegram_service import (
    TELEGRAM_ACT_ROLE,
    TelegramService,
    TelegramServiceError,
)

FILE_CONTENT = b"PRIVATE_WORKSPACE_FILE_SENTINEL_71a9"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _workspace(root: Path) -> ActWorkspace:
    memory = root / "memory"
    knowledge = root / "knowledge"
    quercus = knowledge / "quercus"
    workspace = root / "workspace"
    downloads = workspace / "downloads"
    for path in (memory, quercus, downloads):
        path.mkdir(parents=True, exist_ok=True)
    return ActWorkspace(
        root=root,
        memory=memory,
        knowledge=knowledge,
        quercus=quercus,
        workspace=workspace,
        downloads=downloads,
    )


def _connected_service(db: Session) -> tuple[TelegramService, FakeTelegramBotApi, int]:
    store = FakeSecretStore()
    reference = store.put("123:telegram-token", namespace="telegram")
    session = ActSession(agent_id="act", codex_thread_id="act-send-file")
    connection = TelegramBotConnection(
        role=TELEGRAM_ACT_ROLE,
        is_default=True,
        secret_store_id=store.implementation_id,
        secret_reference=reference,
        bot_id="1001",
        status="connected",
        paired_chat_id="42",
        paired_user_id="7",
        topics_enabled=True,
        allows_users_to_create_topics=True,
    )
    db.add_all([session, connection])
    db.flush()
    db.add(
        TelegramTopicSession(
            connection_id=connection.id,
            telegram_chat_id="42",
            message_thread_id=91,
            session_id=session.id,
            topic_name="Files",
        )
    )
    db.commit()
    api = FakeTelegramBotApi()
    return (
        TelegramService(
            db,
            role=TELEGRAM_ACT_ROLE,
            secret_store=store,
            api_factory=lambda _token: api,
        ),
        api,
        session.id,
    )


def test_send_workspace_file_uses_current_act_topic_and_returns_metadata_only(
    db: Session,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "act")
    source = workspace.downloads / "report.txt"
    source.write_bytes(FILE_CONTENT)
    telegram, api, session_id = _connected_service(db)

    result = ActTelegramFileCapability(
        db,
        workspace=workspace,
        telegram_service=telegram,
    ).send(session_id, "workspace/downloads/report.txt", "Requested report")

    assert result == {
        "status": "sent",
        "path": "workspace/downloads/report.txt",
        "filename": "report.txt",
        "bytes": len(FILE_CONTENT),
        "message_id": 1,
    }
    assert FILE_CONTENT.decode() not in repr(result)
    sent = api.sent_documents[0]
    assert sent["message_thread_id"] == 91
    assert sent["chat"] == {"id": 42}
    assert sent["caption"] == "Requested report"
    assert sent["content"] == FILE_CONTENT


def test_send_workspace_file_refuses_paths_outside_workspace_before_delivery(
    db: Session,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "act")
    outside = workspace.knowledge / "private.txt"
    outside.write_bytes(FILE_CONTENT)
    telegram, api, session_id = _connected_service(db)

    with pytest.raises(ActTelegramFileError) as exc_info:
        ActTelegramFileCapability(
            db,
            workspace=workspace,
            telegram_service=telegram,
        ).send(session_id, "knowledge/private.txt")

    assert exc_info.value.error_type == "path_outside_workspace"
    assert api.sent_documents == []


def test_send_workspace_file_requires_current_session_topic(
    db: Session,
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "act")
    (workspace.workspace / "result.txt").write_bytes(FILE_CONTENT)
    telegram, _api, _session_id = _connected_service(db)
    other = ActSession(agent_id="act", codex_thread_id="other-act-session")
    db.add(other)
    db.commit()

    with pytest.raises(TelegramServiceError, match="no paired Telegram topic") as exc_info:
        ActTelegramFileCapability(
            db,
            workspace=workspace,
            telegram_service=telegram,
        ).send(other.id, "workspace/result.txt")
    assert exc_info.value.error_type == "telegram_topic_unavailable"
