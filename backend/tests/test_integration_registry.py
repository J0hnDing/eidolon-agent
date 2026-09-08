from __future__ import annotations

from dataclasses import replace

import pytest

from app.integrations import (
    DEFAULT_INTEGRATION_REGISTRY,
    IntegrationEffect,
    IntegrationOperationRegistry,
    ProviderSpec,
    ResourceSpec,
    RiskLevel,
)
from app.services.integration_registry import OPERATIONS

EXPECTED_OPERATION_IDS = {
    "atlas.experience.list",
    "atlas.goal.list",
    "atlas.interest.get",
    "atlas.interest.list",
    "atlas.knowledge.frontier.list",
    "atlas.knowledge.node.get",
    "atlas.knowledge.node.know",
    "atlas.knowledge.search",
    "atlas.person.get",
    "atlas.project.list",
    "atlas.relationship.list",
    "email.conversation.get",
    "email.read_and_mark_new",
    "email.read_new",
    "email.search",
    "email.send",
    "github.issue.list",
    "github.pull_request.list",
    "github.repository.file.read",
    "github.repository.get",
    "github.repository.tree.list",
    "github.repository.trending.list",
    "google_calendar.event.create",
    "google_calendar.event.delete",
    "google_calendar.event.get",
    "google_calendar.event.list",
    "google_calendar.event.update",
    "huggingface.get_paper",
    "huggingface.list_papers",
    "huggingface.search_papers",
    "notion.report.create",
    "notion.report.delete",
    "notion.report.get",
    "notion.report.list",
    "notion.todo.create",
    "notion.todo.delete",
    "notion.todo.list",
    "notion.todo.update",
    "telegram.notification.send",
}


def _provider() -> ProviderSpec:
    return ProviderSpec(id="github", display_name="GitHub")


def _operation():
    return OPERATIONS["github.repository.get"]


def test_registry_rejects_duplicate_provider() -> None:
    provider = _provider()
    with pytest.raises(ValueError, match="Duplicate integration provider ID"):
        IntegrationOperationRegistry((provider, provider), ())


def test_registry_rejects_duplicate_operation() -> None:
    operation = _operation()
    with pytest.raises(ValueError, match="Duplicate integration operation ID"):
        IntegrationOperationRegistry((_provider(),), (operation, operation))


def test_registry_rejects_missing_provider() -> None:
    with pytest.raises(ValueError, match="has no supporting provider"):
        IntegrationOperationRegistry((), (_operation(),))


def test_registry_rejects_invalid_effect() -> None:
    operation = replace(_operation(), effects=frozenset({"read"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid effect"):
        IntegrationOperationRegistry((_provider(),), (operation,))


def test_registry_rejects_invalid_risk() -> None:
    operation = replace(_operation(), risk="low")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid risk"):
        IntegrationOperationRegistry((_provider(),), (operation,))


@pytest.mark.parametrize(
    "resource",
    [
        ResourceSpec(type="", identity_fields=()),
        ResourceSpec(type="github.repository", identity_fields=("owner", "owner")),
        ResourceSpec(type="github.repository", identity_fields=("Owner",)),
    ],
)
def test_registry_rejects_invalid_resource(resource: ResourceSpec) -> None:
    with pytest.raises(ValueError, match="invalid resource"):
        IntegrationOperationRegistry((_provider(),), (replace(_operation(), resource=resource),))


def test_registry_rejects_invalid_json_schema() -> None:
    operation = replace(_operation(), input_schema={"type": "not-a-json-schema-type"})
    with pytest.raises(ValueError, match="invalid input schema"):
        IntegrationOperationRegistry((_provider(),), (operation,))


def test_default_registry_lookup_and_order_are_deterministic() -> None:
    operations = DEFAULT_INTEGRATION_REGISTRY.list()
    assert [operation.id for operation in operations] == sorted(EXPECTED_OPERATION_IDS)
    assert DEFAULT_INTEGRATION_REGISTRY.get("github.repository.get") is OPERATIONS["github.repository.get"]
    assert [provider.id for provider in DEFAULT_INTEGRATION_REGISTRY.providers()] == sorted(
        {
            "github",
            "atlas",
            "notion",
            "google_calendar",
            "gmail",
            "outlook",
            "telegram",
            "huggingface",
            "wecom",
        }
    )
    assert DEFAULT_INTEGRATION_REGISTRY.for_provider("wecom") == ()
    assert [operation.id for operation in DEFAULT_INTEGRATION_REGISTRY.for_provider("notion")] == sorted(
        operation_id for operation_id in EXPECTED_OPERATION_IDS if operation_id.startswith("notion.")
    )


def test_default_registry_preserves_all_operation_ids() -> None:
    assert len(OPERATIONS) == 39
    assert set(OPERATIONS) == EXPECTED_OPERATION_IDS


def test_effect_risk_and_compatibility_metadata_are_derived() -> None:
    read = OPERATIONS["github.repository.get"]
    delete = OPERATIONS["notion.todo.delete"]
    high = OPERATIONS["email.send"]

    assert read.effects == frozenset({IntegrationEffect.READ})
    assert read.read_only is True
    assert delete.effects == frozenset({IntegrationEffect.DELETE})
    assert delete.destructive is True
    assert delete.read_only is False
    assert high.risk is RiskLevel.HIGH
    assert high.requires_invocation_approval is True


def test_contract_identity_contains_security_relevant_fields() -> None:
    identity = DEFAULT_INTEGRATION_REGISTRY.contract_identity(_operation().id)
    assert identity == {
        "input_schema": _operation().input_schema,
        "output_schema": _operation().output_schema,
        "effects": ["read"],
        "resource": {"type": "github.repository", "identity_fields": ["owner", "repository"]},
        "risk": "low",
        "provider_selection": "single",
        "supported_providers": ["github"],
        "version": 1,
    }
