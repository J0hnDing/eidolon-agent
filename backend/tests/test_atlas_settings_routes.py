from pathlib import Path
from types import SimpleNamespace

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
            passphrase_configured=False,
        )


def test_restart_route_accepts_only_empty_object_and_returns_sanitized_status(monkeypatch) -> None:
    fake = FakeSettings()
    db = object()
    refreshed = []
    monkeypatch.setattr(atlas_settings, "build_default_atlas_settings_service", lambda _db: fake)
    monkeypatch.setattr(
        atlas_settings,
        "FunctionCatalogService",
        lambda value: SimpleNamespace(refresh=lambda: refreshed.append(value)),
    )
    response = atlas_settings.restart_atlas(db)

    assert response.process_ownership == "owned"
    assert fake.restarts == 1
    assert refreshed == [db]


def test_api_key_routes_are_absent() -> None:
    paths = {route.path for route in atlas_settings.router.routes}
    assert "/settings/integrations/atlas/api-key" not in paths
