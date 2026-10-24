import pytest
from jsonschema import Draft202012Validator

from app.services import atlas_provider as provider_module
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.github_provider import IntegrationProviderError
from app.services.integration_registry import OPERATIONS
from tests.test_atlas_native_read_contract import synthetic_atlas  # noqa: F401


def test_native_learning_and_correction_with_provenance_and_optimistic_revision(synthetic_atlas, monkeypatch):  # noqa: F811
    monkeypatch.setattr(provider_module, "ATLAS_BASE_URL", synthetic_atlas["url"])
    adapter = UrllibAtlasProviderAdapter()

    def call(name, payload):
        operation = OPERATIONS[name]
        Draft202012Validator(operation.input_schema).validate(payload)
        result = adapter.execute(operation, payload)
        Draft202012Validator(operation.output_schema).validate(result)
        return result

    provenance = {"attribution": "inferred", "confidence": 0.4,
                  "evidence": [{"source": "Synthetic conversation", "excerpt": "May enjoy learning this"}]}
    learned = call("atlas.records.create", {"category": "interest", "title": "Synthetic astronomy",
        "data": {"liking": 70, "skillLevel": 10, "provenance": provenance}})["record"]
    corrected = call("atlas.records.update", {"id": learned["id"], "expected_revision": learned["revision"],
        "data": {"liking": 20, "skillLevel": 10, "provenance": {"attribution": "stated",
            "evidence": [{"source": "Synthetic user correction"}]}}})["record"]
    assert corrected["data"]["liking"] == 20
    assert corrected["data"]["provenance"]["attribution"] == "stated"
    with pytest.raises(IntegrationProviderError) as stale:
        call("atlas.records.update", {"id": learned["id"], "expected_revision": learned["revision"],
                                     "title": "Stale inference"})
    assert stale.value.error_type == "stale_revision"
    readback = call("atlas.records.get", {"id": learned["id"]})["record"]
    assert readback["title"] == corrected["title"]
    history = call("atlas.history.list", {"kind": "record", "id": learned["id"]})
    assert history["total"] == 2
    link = call("atlas.links.create", {"sourceId": synthetic_atlas["ids"]["project"],
        "targetId": learned["id"], "type": "related", "provenance": provenance})["link"]
    changed = call("atlas.links.update", {"id": link["id"], "expected_revision": link["revision"],
                                         "notes": "Corrected relation context"})["link"]
    assert changed["notes"] == "Corrected relation context"
    with pytest.raises(IntegrationProviderError):
        call("atlas.links.update", {"id": link["id"], "expected_revision": link["revision"], "notes": "Stale"})


@pytest.mark.parametrize("category", ["person", "personality", "end", "goal", "belief", "interest",
                                      "experience", "project", "resource", "relationship"])
def test_every_editable_record_category_round_trips_update_trash_and_restore(synthetic_atlas, monkeypatch, category):  # noqa: F811
    monkeypatch.setattr(provider_module, "ATLAS_BASE_URL", synthetic_atlas["url"])
    adapter = UrllibAtlasProviderAdapter()

    def call(name, requested):
        spec = OPERATIONS[name]
        Draft202012Validator(spec.input_schema).validate(requested)
        output = adapter.execute(spec, requested)
        Draft202012Validator(spec.output_schema).validate(output)
        return output

    identity = synthetic_atlas["ids"][category]
    before = call("atlas.records.get", {"id": identity})["record"]
    updated = call("atlas.records.update", {"id": identity, "expected_revision": before["revision"],
        "title": before["title"] + " corrected", "data": before["data"],
        "customFieldValues": before["customFieldValues"]})["record"]
    assert updated["data"] == before["data"]
    assert updated["customFieldValues"] == before["customFieldValues"]
    trashed = call("atlas.records.trash", {"id": identity, "expected_revision": updated["revision"]})["record"]
    assert trashed["trashed"] is True
    restored = call("atlas.records.restore_revision", {"id": identity, "revision": before["revision"],
        "expected_revision": trashed["revision"]})["record"]
    assert restored["trashed"] is False
    assert restored["title"] == before["title"]
    assert restored["data"] == before["data"]


def test_populated_cash_currency_change_requires_native_explicit_confirmation(synthetic_atlas, monkeypatch):  # noqa: F811
    monkeypatch.setattr(provider_module, "ATLAS_BASE_URL", synthetic_atlas["url"])
    adapter = UrllibAtlasProviderAdapter()
    data = {"resourceVersion": 1, "kind": "cash", "amount": 100.123456789, "currency": "CAD",
            "lastUpdated": "2026-10-20", "provenance": {"attribution": "stated"}}
    cash = adapter.execute(OPERATIONS["atlas.records.create"], {
        "category": "resource", "title": "Cash & money", "data": data})["record"]
    requested = {"id": cash["id"], "expected_revision": cash["revision"], "data": {**data, "currency": "USD"}}
    with pytest.raises(IntegrationProviderError) as unconfirmed:
        adapter.execute(OPERATIONS["atlas.records.update"], requested)
    assert unconfirmed.value.error_type == "invalid_input"
    updated = adapter.execute(OPERATIONS["atlas.records.update"], {**requested, "confirmCurrencyChange": True})["record"]
    assert updated["data"]["currency"] == "USD"
    assert updated["data"]["amount"] == data["amount"]
    with pytest.raises(IntegrationProviderError) as fixed:
        adapter.execute(OPERATIONS["atlas.records.trash"], {"id": updated["id"], "expected_revision": updated["revision"]})
    assert fixed.value.error_type == "invalid_input"
