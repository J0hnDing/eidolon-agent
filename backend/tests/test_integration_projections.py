from __future__ import annotations

from types import SimpleNamespace

from app.services.function_catalog_service import FunctionCatalogService


def test_catalog_mcp_hints_and_approval_are_derived_from_canonical_specs(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.function_catalog_service.build_default_integration_service",
        lambda _db: SimpleNamespace(operation_available=lambda _operation_id: True),
    )
    monkeypatch.setattr(
        "app.services.function_catalog_service.InvocationApprovalService.approval_available",
        lambda _self: True,
    )
    monkeypatch.setattr(
        "app.services.function_catalog_service.codex_available",
        lambda: True,
    )

    entries = {
        entry["id"]: entry
        for entry in FunctionCatalogService(None, project_root=tmp_path)._fixed_entries()  # type: ignore[arg-type]
        if entry.get("category") == "integration"
    }

    assert entries["github.repository.get"]["mcp_read_only"] is True
    assert entries["github.repository.get"]["mcp_destructive"] is False
    assert entries["github.repository.get"]["mcp_open_world"] is True
    assert entries["notion.todo.delete"]["mcp_read_only"] is False
    assert entries["notion.todo.delete"]["mcp_destructive"] is True
    assert entries["google_calendar.event.delete"]["mcp_read_only"] is False
    assert entries["google_calendar.event.delete"]["mcp_destructive"] is True
    assert entries["google_calendar.event.update"]["mcp_destructive"] is False
    assert entries["email.send"]["mcp_read_only"] is False
    assert entries["email.send"]["requires_invocation_approval"] is True
    assert entries["email.send"]["invocation"]["effects"] == ["send"]

