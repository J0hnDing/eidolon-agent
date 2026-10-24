"""States parity through the actual native in-memory Atlas HTTP server."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator

from app.integrations.atlas_read import ATLAS_READ_SPECS
from app.integrations.atlas_state_write import ATLAS_STATE_WRITE_SPECS
from app.services import atlas_provider as provider_module
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.atlas_state_write_provider import AtlasStateWriteProvider
from app.services.github_provider import IntegrationProviderError
from tests.test_atlas_native_read_contract import native_json, synthetic_atlas  # noqa: F401

SPECS = {spec.id: spec for spec in ATLAS_STATE_WRITE_SPECS}
EVIDENCE = {"attribution": "user_report", "source": "conversation:synthetic-feedback",
            "note": "The user reported satisfaction after a completed session."}


@pytest.fixture
def states_write(synthetic_atlas, monkeypatch):  # noqa: F811
    monkeypatch.setattr(provider_module, "ATLAS_BASE_URL", synthetic_atlas["url"])
    provider = AtlasStateWriteProvider(UrllibAtlasProviderAdapter()._request)

    def execute(name, payload):
        result = provider.execute(name, payload)
        Draft202012Validator(SPECS[name].output_schema).validate(result)
        return result

    return execute


def test_configuration_preserves_other_parameters_and_rejects_stale_revision(synthetic_atlas, states_write):  # noqa: F811
    before = native_json(synthetic_atlas, "/api/states/configuration")
    previous = before["states"]["physical_energy"]["recoveryRate"]
    updated = states_write("atlas.states.configuration.update", {
        "expected_revision": before["revision"],
        "states": {"physical_energy": {"recoveryRate": previous + 0.01}},
    })["configuration"]
    assert updated["revision"] == before["revision"] + 1
    expected = deepcopy(before["states"])
    expected["physical_energy"]["recoveryRate"] = previous + 0.01
    assert updated["states"] == expected
    with pytest.raises(IntegrationProviderError) as stale:
        states_write("atlas.states.configuration.update", {
            "expected_revision": before["revision"], "states": {"mental_energy": {"confidenceFloor": 0.1}},
        })
    assert stale.value.error_type == "stale_revision"
    assert native_json(synthetic_atlas, "/api/states/configuration") == updated


def test_events_retain_native_duplicate_conflict_and_one_time_semantics(synthetic_atlas, states_write):  # noqa: F811
    initialized = native_json(synthetic_atlas, "/api/states")["initialization"]["initializedAt"]
    payload = {"eventId": "eidolon-states-activity", "startedAt": initialized,
               "endedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "confidence": 0.6,
               "targets": {synthetic_atlas["ids"]["goal"]: {"intensity": 0.4, "evidence": EVIDENCE}}}
    first = states_write("atlas.states.events.create", payload)
    assert first["duplicate"] is False
    assert first["event"]["eventId"] == payload["eventId"]
    assert first["event"]["targetEffects"][0]["dose"] == 0.4
    duplicate = states_write("atlas.states.events.create", payload)
    assert duplicate == {**first, "duplicate": True}
    with pytest.raises(IntegrationProviderError) as conflict:
        states_write("atlas.states.events.create", {**payload, "confidence": 0.5})
    assert conflict.value.error_type == "event_id_conflict"
    with pytest.raises(IntegrationProviderError) as invalid:
        states_write("atlas.states.events.create", {**payload, "eventId": "invalid-duration",
                     "impacts": [{"state": "goal_progression", "intensity": 0.4, "durationHours": 1}]})
    assert invalid.value.error_type == "invalid_input"


def test_manual_observations_preserve_zero_and_history_with_idempotent_retries(synthetic_atlas, states_write):  # noqa: F811
    before = native_json(synthetic_atlas, "/api/states/events?limit=100")
    payload = {"eventId": "eidolon-zero-estimate", "states": {
        "mental_energy": {"value": 0, "confidence": 0},
        "physical_energy": {"value": 0.123456789, "confidence": 1},
    }}
    first = states_write("atlas.states.observations.create", payload)
    observations = {item["state"]: item for item in first["event"]["observations"]}
    assert observations["mental_energy"]["value"] == 0
    assert observations["mental_energy"]["confidence"] == 0
    assert observations["physical_energy"]["value"] == 0.123456789
    assert first["event"]["type"] == "observation"
    assert states_write("atlas.states.observations.create", payload) == {**first, "duplicate": True}
    with pytest.raises(IntegrationProviderError) as conflict:
        states_write("atlas.states.observations.create", {**payload, "states": {"mental_energy": {"value": 1, "confidence": 0}}})
    assert conflict.value.error_type == "event_id_conflict"
    after = native_json(synthetic_atlas, "/api/states/events?limit=100")
    assert after["total"] == before["total"] + 1
    assert {item["eventId"] for item in before["events"]} <= {item["eventId"] for item in after["events"]}


def test_native_configuration_and_duplicate_impact_invariants_are_atomic(synthetic_atlas, states_write):  # noqa: F811
    before = native_json(synthetic_atlas, "/api/states/configuration")
    with pytest.raises(IntegrationProviderError) as invalid:
        states_write("atlas.states.configuration.update", {
            "expected_revision": before["revision"], "states": {
                "mental_energy": {"responseRate": 0.2},
                "physical_activity": {"optimalLower": 0.9, "optimalUpper": 0.1},
            },
        })
    assert invalid.value.error_type == "invalid_input"
    assert native_json(synthetic_atlas, "/api/states/configuration") == before
    history = native_json(synthetic_atlas, "/api/states/events?limit=100")
    initialized = native_json(synthetic_atlas, "/api/states")["initialization"]["initializedAt"]
    with pytest.raises(IntegrationProviderError) as invalid:
        states_write("atlas.states.events.create", {
            "eventId": "duplicate-impact", "startedAt": initialized, "endedAt": initialized,
            "confidence": 0.5, "impacts": [{"state": "goal_progression", "intensity": 0.3},
                                         {"state": "goal_progression", "intensity": 0.6}],
        })
    assert invalid.value.error_type == "invalid_input"
    assert native_json(synthetic_atlas, "/api/states/events?limit=100") == history


def test_per_record_configuration_preserves_others_and_rejects_unknown_targets(synthetic_atlas, states_write):  # noqa: F811
    goal_id = synthetic_atlas["ids"]["goal"]
    before = native_json(synthetic_atlas, "/api/states/configuration")
    updated = states_write("atlas.states.configuration.update", {
        "expected_revision": before["revision"], "states": {}, "targets": {goal_id: {"decayRate": 0.015}},
    })["configuration"]
    assert updated["states"] == before["states"]
    assert updated["targets"][goal_id]["decayRate"] == 0.015
    assert updated["targets"][goal_id]["responseRate"] == before["targets"][goal_id]["responseRate"]
    assert {key: value for key, value in updated["targets"].items() if key != goal_id} == {
        key: value for key, value in before["targets"].items() if key != goal_id
    }
    for bad_target in ("missing-record", synthetic_atlas["ids"]["person"]):
        with pytest.raises(IntegrationProviderError) as invalid:
            states_write("atlas.states.configuration.update", {
                "expected_revision": updated["revision"], "targets": {bad_target: {"decayRate": 0.1}},
            })
        assert invalid.value.error_type == "invalid_input"
    assert native_json(synthetic_atlas, "/api/states/configuration") == updated


def test_target_observations_preserve_evidence_zero_and_derived_summaries(synthetic_atlas, states_write):  # noqa: F811
    ids = synthetic_atlas["ids"]
    before = native_json(synthetic_atlas, "/api/states")
    targets = {ids[key]: {"value": value, "confidence": confidence, "evidence": EVIDENCE}
               for key, value, confidence in (("goal", 0, 0), ("interest", 0.75, 0.8), ("relationship", 0.65, 0.9))}
    payload = {"eventId": "eidolon-per-record-feedback", "targets": targets}
    first = states_write("atlas.states.observations.create", payload)
    assert first["event"]["targets"] == targets
    assert first["event"]["observations"] == []
    assert states_write("atlas.states.observations.create", payload) == {**first, "duplicate": True}
    changed = deepcopy(payload)
    changed["targets"][ids["goal"]]["evidence"] = {**EVIDENCE, "note": "Changed justification"}
    with pytest.raises(IntegrationProviderError) as conflict:
        states_write("atlas.states.observations.create", changed)
    assert conflict.value.error_type == "event_id_conflict"
    current = native_json(synthetic_atlas, "/api/states")
    assert current["targets"][ids["goal"]]["value"] == 0
    assert current["targets"][ids["goal"]]["confidence"] == 0
    assert current["targets"][ids["interest"]]["evidence"] == EVIDENCE
    assert current["targets"][ids["goal"]]["lastProgressAt"] == before["targets"][ids["goal"]]["lastProgressAt"]
    assert current["targets"][ids["goal"]]["lastObservationAt"] == first["event"]["endedAt"]
    summaries = {item["key"]: item for item in current["states"]}
    assert len(summaries) == 7
    assert "interest_progression" not in summaries and "relationship_satisfaction" not in summaries
    for key, record_id in (("goal_progression", ids["goal"]), ("goal_progression", ids["interest"]),
                           ("close_connection", ids["relationship"])):
        assert summaries[key]["derived"] is True
        assert record_id in summaries[key]["componentRecordIds"]
    progression = summaries["goal_progression"]
    assert progression["value"] == pytest.approx(sum(current["targets"][record_id]["value"]
        for record_id in progression["componentRecordIds"]) / progression["componentCount"])
    configuration = native_json(synthetic_atlas, "/api/states/configuration")
    for key in ("goal_progression", "close_connection"):
        assert summaries[key]["parameters"] == configuration["states"][key]


def test_target_events_are_one_time_and_target_reads_preserve_native_response(synthetic_atlas, states_write):  # noqa: F811
    ids = synthetic_atlas["ids"]
    initialized = native_json(synthetic_atlas, "/api/states")["initialization"]["initializedAt"]
    ended_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    targets = {ids[key]: {"intensity": 0.4, "evidence": {**EVIDENCE, "attribution": "observed"}}
               for key in ("goal", "interest")}
    payload = {"eventId": "eidolon-per-record-activity", "startedAt": initialized, "endedAt": ended_at,
               "confidence": 0.7, "impacts": [], "targets": targets}
    first = states_write("atlas.states.events.create", payload)
    assert first["event"]["impacts"] == []
    assert first["event"]["targets"] == targets
    assert {effect["recordId"] for effect in first["event"]["targetEffects"]} == set(targets)
    assert all(effect["dose"] == 0.4 and effect["doseUnit"] == "reference_stimulus"
               for effect in first["event"]["targetEffects"])
    assert states_write("atlas.states.events.create", payload) == {**first, "duplicate": True}
    read_spec = next(spec for spec in ATLAS_READ_SPECS if spec.id == "atlas.states.get")
    adapter = UrllibAtlasProviderAdapter()
    response = adapter.execute(read_spec, {})["states"]
    assert response["targets"][ids["goal"]]["lastProgressAt"] == first["event"]["endedAt"]
    configuration = adapter.execute(read_spec, {"view": "configuration"})["states"]
    assert configuration == native_json(synthetic_atlas, "/api/states/configuration")
    assert read_spec.contract_version == 3
    assert all(spec.contract_version == 3 for spec in SPECS.values())


@pytest.mark.parametrize("key", ["goal_progression", "close_connection"])
def test_direct_derived_summary_reports_reject_without_persisting(synthetic_atlas, states_write, key):  # noqa: F811
    history = native_json(synthetic_atlas, "/api/states/events?limit=100")
    with pytest.raises(IntegrationProviderError) as invalid:
        states_write("atlas.states.observations.create", {
            "eventId": f"direct-{key}", "states": {key: {"value": 1, "confidence": 1}},
        })
    assert invalid.value.error_type == "invalid_input"
    initialized = native_json(synthetic_atlas, "/api/states")["initialization"]["initializedAt"]
    with pytest.raises(IntegrationProviderError) as invalid:
        states_write("atlas.states.events.create", {
            "eventId": f"direct-event-{key}", "startedAt": initialized, "endedAt": initialized,
            "confidence": 1, "impacts": [{"state": key, "intensity": 1}],
        })
    assert invalid.value.error_type == "invalid_input"
    assert native_json(synthetic_atlas, "/api/states/events?limit=100") == history


@pytest.mark.parametrize("name,payload", [
    ("atlas.states.configuration.update", {"expected_revision": 1, "states": {"unknown": {"recoveryRate": 0.2}}}),
    ("atlas.states.configuration.update", {"expected_revision": 1, "states": {"physical_energy": {"decayRate": 1}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "states": {"mental_energy": {"value": float("nan"), "confidence": 0}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "states": {"mental_energy": {"value": 0, "confidence": 2}}}),
    ("atlas.states.observations.create", {"eventId": " estimate ", "states": {"mental_energy": {"value": 0, "confidence": 1}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "states": {}}),
    ("atlas.states.configuration.update", {"expected_revision": 1}),
    ("atlas.states.configuration.update", {"expected_revision": 1, "states": {}, "targets": {}}),
    ("atlas.states.configuration.update", {"expected_revision": 1, "targets": {"goal": {"recoveryRate": 1}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "targets": {}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "targets": {"goal": {"value": 0, "confidence": 1}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "targets": {" goal ": {"value": 0, "confidence": 1, "evidence": EVIDENCE}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "targets": {"goal": {"value": 0, "confidence": 1, "evidence": {**EVIDENCE, "attribution": "certain"}}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "targets": {"goal": {"value": 0, "confidence": 1, "evidence": {**EVIDENCE, "note": " "}}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "targets": {"goal": {"value": 0, "confidence": 1, "evidence": {**EVIDENCE, "source": "x" * 2001}}}}),
    ("atlas.states.observations.create", {"eventId": "estimate", "states": {"interest_progression": {"value": 0.5, "confidence": 1}}}),
    ("atlas.states.events.create", {"eventId": "event", "startedAt": "2026-10-20T14:00:00Z", "endedAt": "2026-10-20T15:00:00Z", "confidence": 1, "impacts": [], "targets": {}}),
    ("atlas.states.events.create", {"eventId": "event", "startedAt": "2026-10-20T14:00:00Z", "endedAt": "2026-10-20T15:00:00Z", "confidence": 1, "targets": {"goal": {"intensity": 0.5, "durationHours": 1, "evidence": EVIDENCE}}}),
])
def test_invalid_direct_calls_never_reach_transport(name, payload):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid input reached Atlas transport")

    with pytest.raises(IntegrationProviderError) as invalid:
        AtlasStateWriteProvider(unexpected).execute(name, payload)
    assert invalid.value.error_type == "invalid_input"


@pytest.mark.parametrize("result", [None, [], {"event": {}}, {"event": {}, "duplicate": 0}])
def test_invalid_native_event_output_is_sanitized(result):
    with pytest.raises(IntegrationProviderError) as invalid:
        AtlasStateWriteProvider(lambda *args, **kwargs: result).execute(
            "atlas.states.observations.create", {"eventId": "estimate", "states": {"mental_energy": {"value": 0, "confidence": 0}}},
        )
    assert invalid.value.error_type == "provider_unavailable"
