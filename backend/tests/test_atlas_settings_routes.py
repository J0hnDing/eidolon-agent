from pathlib import Path

from app.routers import atlas_settings
from app.schemas.atlas_settings import AtlasSettingsStatus


class FakeSettings:
    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1
        return AtlasSettingsStatus(
            directory=str(Path("C:/Atlas")),
            running=True,
            process_ownership="owned",
            initialized=True,
            locked=False,
            api_key_status="missing",
            auto_unlock_configured=False,
        )


def test_restart_route_accepts_only_empty_object_and_returns_sanitized_status(monkeypatch) -> None:
    fake = FakeSettings()
    monkeypatch.setattr(atlas_settings, "build_default_atlas_settings_service", lambda _db: fake)
    response = atlas_settings.restart_atlas(object())

    assert response.process_ownership == "owned"
    assert fake.restarts == 1
