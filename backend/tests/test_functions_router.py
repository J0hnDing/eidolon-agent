from types import SimpleNamespace

from app.routers import functions


def test_function_catalog_route_passes_refresh_to_projection(monkeypatch) -> None:
    calls = []

    def fake_service(_db):
        return SimpleNamespace(
            list_entries=lambda **kwargs: calls.append(kwargs) or [],
        )

    monkeypatch.setattr(functions, "FunctionCatalogService", fake_service)

    assert functions.list_function_catalog(refresh=True, db=object()) == []
    assert calls == [{"refresh": True, "include_runtime_state": True}]
