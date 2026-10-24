"""Native lifecycle round trips plus fixed-route and typed-contract checks."""

import pytest
from jsonschema import Draft202012Validator

from app.integrations.atlas_lifecycle import ATLAS_LIFECYCLE_OPERATIONS, ATLAS_LIFECYCLE_SPECS
from app.integrations.types import IntegrationEffect, RiskLevel
from app.services import atlas_provider as provider_module
from app.services.atlas_mutation_provider import AtlasMutationProvider
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.github_provider import IntegrationProviderError
from tests.test_atlas_native_read_contract import synthetic_atlas  # noqa: F401

SPECS = {spec.id: spec for spec in ATLAS_LIFECYCLE_SPECS}


def test_every_lifecycle_contract_has_valid_schema_example_and_appropriate_risk():
    assert set(SPECS) == ATLAS_LIFECYCLE_OPERATIONS
    for spec in SPECS.values():
        Draft202012Validator.check_schema(spec.input_schema)
        Draft202012Validator.check_schema(spec.output_schema)
        Draft202012Validator(spec.input_schema).validate(spec.presentation.usage_example["input"])
        assert (spec.risk == RiskLevel.HIGH) == (spec.id in {"atlas.trash.empty", "atlas.custom_fields.delete"})
        if spec.risk == RiskLevel.HIGH:
            assert spec.requires_invocation_approval and IntegrationEffect.DELETE in spec.effects


@pytest.mark.parametrize(("operation", "requested"), [
    ("atlas.records.trash", {"id": "r"}),
    ("atlas.records.restore", {"id": "r", "expected_revision": True}),
    ("atlas.records.restore_revision", {"id": "r", "revision": 0, "expected_revision": 2}),
    ("atlas.records.restore_revision", {"id": "r", "revision": 1, "expected_revision": 2, "path": "/api/export"}),
    ("atlas.links.restore_revision", {"id": "l", "revision": 1}),
    ("atlas.trash.empty", {"ids": ["r"]}),
    ("atlas.custom_fields.create", {"category": "person", "name": "x", "type": "shell"}),
    ("atlas.custom_fields.update", {"id": "f"}),
    ("atlas.custom_fields.delete", {"id": "../f"}),
    ("atlas.records.trash", {"id": "a%2fb", "expected_revision": 1}),
    ("atlas.links.delete", {"id": "a?b", "expected_revision": 1}),
    ("atlas.unsupported", {}),
])
def test_invalid_inputs_fail_before_transport(operation, requested):
    def no_request(*args, **kwargs):
        pytest.fail("Invalid input reached Atlas")

    with pytest.raises(IntegrationProviderError) as error:
        AtlasMutationProvider(no_request).execute(operation, requested)
    assert error.value.error_type == "invalid_input"


@pytest.mark.parametrize(("operation", "requested", "path", "method", "payload", "key", "result"), [
    ("atlas.records.trash", {"id": "r", "expected_revision": 2}, "/api/records/r", "DELETE", {"revision": 2},
     "record", {"id": "r", "trashed": True}),
    ("atlas.records.restore", {"id": "r", "expected_revision": 3}, "/api/records/r/restore", "POST", {"revision": 3},
     "record", {"id": "r", "trashed": False}),
    ("atlas.records.restore_revision", {"id": "r", "expected_revision": 3, "revision": 1,
                                        "confirm_currency_change": True},
     "/api/records/r/revisions/1/restore", "POST", {"revision": 3, "confirmCurrencyChange": True},
     "record", {"id": "r", "data": {"exact": 0.123456789123}}),
    ("atlas.links.delete", {"id": "l", "expected_revision": 4}, "/api/links/l", "DELETE", {"revision": 4},
     "result", {"deleted": True}),
    ("atlas.links.restore_revision", {"id": "l", "expected_revision": 4, "revision": 2},
     "/api/links/l/restore-revision", "POST", {"revision": 2, "expectedRevision": 4}, "link", {"id": "l"}),
    ("atlas.trash.empty", {}, "/api/trash", "DELETE", None, "result", {"deleted": 1}),
    ("atlas.custom_fields.list", {"category": "a&b"}, "/api/custom-fields?category=a%26b", "GET", None,
     "fields", [{"id": "f", "futureNativeField": {"unknown": True}}]),
    ("atlas.custom_fields.create", {"category": "person", "name": "x", "type": "text"},
     "/api/custom-fields", "POST", {"category": "person", "name": "x", "type": "text"}, "field", {"id": "f"}),
    ("atlas.custom_fields.update", {"id": "f", "name": "New name"},
     "/api/custom-fields/f", "PATCH", {"name": "New name"}, "field", {"id": "f"}),
    ("atlas.custom_fields.delete", {"id": "f"}, "/api/custom-fields/f", "DELETE", None,
     "result", {"deleted": True, "field": {"id": "f"}, "recordsUpdated": 1, "revisionsUpdated": 2}),
])
def test_fixed_routes_translate_revision_guards_and_preserve_native_output(
    operation, requested, path, method, payload, key, result,
):
    calls = []

    def request(actual_path, actual_payload, **options):
        calls.append((actual_path, actual_payload, options))
        return result

    output = AtlasMutationProvider(request).execute(operation, requested)
    assert output == {key: result}
    Draft202012Validator(SPECS[operation].output_schema).validate(output)
    assert calls == [(path, payload, {"method": method, "timeout": 15, "max_bytes": 32 * 1024 * 1024})]


@pytest.mark.parametrize(("exception", "kind"), [(TimeoutError("sensitive"), "provider_timeout"),
                                               (OSError("sensitive"), "provider_unavailable"),
                                               (ValueError("sensitive"), "provider_unavailable")])
def test_transport_failures_are_normalized(exception, kind):
    def request(*args, **kwargs):
        raise exception

    with pytest.raises(IntegrationProviderError) as error:
        AtlasMutationProvider(request).execute("atlas.trash.empty", {})
    assert error.value.error_type == kind
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(("operation", "result"), [
    ("atlas.custom_fields.list", [None]), ("atlas.custom_fields.create", {}),
    ("atlas.records.trash", []), ("atlas.trash.empty", None),
])
def test_invalid_native_responses_fail_explicitly(operation, result):
    example = SPECS[operation].presentation.usage_example["input"]
    with pytest.raises(IntegrationProviderError) as error:
        AtlasMutationProvider(lambda *args, **kwargs: result).execute(operation, example)
    assert error.value.error_type == "provider_unavailable"


def test_native_lifecycle_custom_fields_history_and_purge(synthetic_atlas, monkeypatch):  # noqa: F811
    # This module owns its synthetic in-memory server. Only records/fields created below are removed.
    monkeypatch.setattr(provider_module, "ATLAS_BASE_URL", synthetic_atlas["url"])
    adapter = UrllibAtlasProviderAdapter()
    provider = AtlasMutationProvider(adapter._request)

    def native(path, payload=None, method="GET"):
        return adapter._request(path, payload, timeout=10, max_bytes=32 * 1024 * 1024, method=method)

    def call(operation, requested):
        result = provider.execute(operation, requested)
        Draft202012Validator(SPECS[operation].output_schema).validate(result)
        return result

    field = call("atlas.custom_fields.create", {
        "category": "interest", "name": "Lifecycle test choice", "type": "singleChoice", "options": ["A", "B"],
    })["field"]
    assert field in call("atlas.custom_fields.list", {"category": "interest"})["fields"]
    field = call("atlas.custom_fields.update", {"id": field["id"], "name": "Lifecycle renamed"})["field"]
    record = native("/api/records", {"category": "interest", "title": "Lifecycle record",
                    "data": {"liking": 0.123456789}, "customFieldValues": {field["id"]: "A"}}, "POST")
    record_id = record["id"]
    original_revision = record["revision"]
    changed = native(f"/api/records/{record_id}", {"revision": original_revision, "title": "Edited title"}, "PATCH")
    restored = call("atlas.records.restore_revision", {
        "id": record_id, "revision": original_revision, "expected_revision": changed["revision"],
    })["record"]
    assert restored["title"] == record["title"]
    assert restored["data"]["liking"] == 0.123456789
    with pytest.raises(IntegrationProviderError) as stale:
        call("atlas.records.trash", {"id": record_id, "expected_revision": original_revision})
    assert stale.value.error_type == "stale_revision"
    trashed = call("atlas.records.trash", {"id": record_id, "expected_revision": restored["revision"]})["record"]
    assert trashed["trashed"] is True
    active = call("atlas.records.restore", {"id": record_id, "expected_revision": trashed["revision"]})["record"]
    assert active["trashed"] is False
    with pytest.raises(IntegrationProviderError):
        call("atlas.custom_fields.update", {"id": field["id"], "options": ["B"]})
    assert native(f"/api/records/{record_id}")["customFieldValues"][field["id"]] == "A"
    link = native("/api/links", {"sourceId": synthetic_atlas["ids"]["project"], "targetId": record_id,
                                 "type": "related", "notes": "Retained link context"}, "POST")
    assert call("atlas.links.delete", {"id": link["id"], "expected_revision": link["revision"]})["result"]["deleted"]
    deleted = native(f"/api/links/{link['id']}")
    assert deleted["deleted"] is True and deleted["revision"] == link["revision"] + 1
    with pytest.raises(IntegrationProviderError) as stale:
        call("atlas.links.restore_revision", {"id": link["id"], "revision": link["revision"],
                                               "expected_revision": link["revision"]})
    assert stale.value.error_type == "stale_revision"
    restored_link = call("atlas.links.restore_revision", {
        "id": link["id"], "revision": link["revision"], "expected_revision": deleted["revision"],
    })["link"]
    assert restored_link["deleted"] is False and restored_link["notes"] == "Retained link context"
    deletion = call("atlas.custom_fields.delete", {"id": field["id"]})["result"]
    assert deletion["deleted"] is True and deletion["recordsUpdated"] == 1 and deletion["revisionsUpdated"] >= 3
    assert field["id"] not in native(f"/api/records/{record_id}")["customFieldValues"]
    assert all(field["id"] not in revision["customFieldValues"]
               for revision in native(f"/api/records/{record_id}/revisions"))
    call("atlas.records.trash", {"id": record_id, "expected_revision": active["revision"]})
    trashed_records = native("/api/records?trashed=true")
    assert [item["id"] for item in trashed_records] == [record_id]
    assert call("atlas.trash.empty", {}) == {"result": {"deleted": 1}}
    with pytest.raises(IntegrationProviderError) as missing:
        native(f"/api/records/{record_id}")
    assert missing.value.error_type == "not_found"
    assert call("atlas.trash.empty", {}) == {"result": {"deleted": 0}}
