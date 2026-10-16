"""Real HTTP compatibility with a synthetic in-memory current sibling Atlas."""

import base64
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import pytest
from jsonschema import Draft202012Validator

from app.services import atlas_provider as provider_module
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.integration_registry import OPERATIONS


@pytest.fixture(scope="module")
def synthetic_atlas(tmp_path_factory):
    sibling = Path(__file__).resolve().parents[2].parent / "Eidolon-Atlas"
    node = shutil.which("node")
    if node is None or not (sibling / "src/server.js").is_file():
        pytest.skip("Current sibling Atlas checkout and Node.js 24+ are required")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True, timeout=10)
    if int(version.stdout.strip().lstrip("v").split(".")[0]) < 24:
        pytest.skip("Node.js 24+ is required for Atlas node:sqlite")
    folder = tmp_path_factory.mktemp("atlas-native-read")
    stdout_path = folder / "stdout.jsonl"
    stderr_path = folder / "stderr.log"
    fixture = Path(__file__).parent / "fixtures/atlas_read_contract.mjs"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [node, str(fixture), str(sibling)], stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 20
            seed = None
            while time.monotonic() < deadline:
                lines = stdout_path.read_text(encoding="utf-8").splitlines()
                if lines:
                    seed = json.loads(lines[0])
                    break
                if process.poll() is not None:
                    pytest.fail(f"Synthetic Atlas failed to start: {stderr_path.read_text(encoding='utf-8')}")
                time.sleep(0.05)
            if seed is None:
                pytest.fail(f"Synthetic Atlas startup timed out: {stderr_path.read_text(encoding='utf-8')}")
            yield {**seed, "url": f"http://127.0.0.1:{seed['port']}"}
        finally:
            if process.stdin is not None:
                if process.poll() is None:
                    process.stdin.write(b"close\n")
                    process.stdin.flush()
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def native_read(synthetic_atlas, monkeypatch):
    monkeypatch.setattr(provider_module, "ATLAS_BASE_URL", synthetic_atlas["url"])
    adapter = UrllibAtlasProviderAdapter()

    def execute(operation_id, requested=None):
        requested = requested or {}
        spec = OPERATIONS[operation_id]
        Draft202012Validator(spec.input_schema).validate(requested)
        output = adapter.execute(spec, requested)
        Draft202012Validator(spec.output_schema).validate(output)
        return output

    return execute


def native_json(seed, path):
    with urlopen(seed["url"] + path, timeout=10) as response:
        return json.load(response)


def test_native_complete_person_health_custom_fields_and_same_completeness(synthetic_atlas, native_read):
    ids = synthetic_atlas["ids"]
    full = native_read("atlas.records.get", {"id": ids["person"]})["record"]
    assert full == native_json(synthetic_atlas, f"/api/records/{ids['person']}")
    assert full["data"]["physicalHealth"]["measurements"][0]["value"] == 70.123456789
    assert full["data"]["languageProficiencies"] == {"English": "native"}
    assert full["data"]["passportNumber"] == "SYNTHETIC-ID-NOT-A-REAL-PASSPORT"
    assert full["customFieldValues"][ids["field"]] == 0.12345678912345
    completeness = native_json(synthetic_atlas, "/api/person/completeness")
    assert native_read("atlas.person.completeness.get")["completeness"] == completeness
    legacy = native_read("atlas.person.get")
    assert legacy["completeness"] == completeness
    assert "passportNumber" not in legacy["personal_info"]["data"]
    assert legacy["personal_info"]["data"]["physicalHealth"] == full["data"]["physicalHealth"]
    assert completeness["definition"] == "atlas-overview-baseline-v1"
    assert {"profile", "personality", "goal", "belief"} <= {area["id"] for area in completeness["areas"]}


def test_native_current_interests_goal_blueprints_and_motivational_graph(synthetic_atlas, native_read):
    ids = synthetic_atlas["ids"]
    interest = native_read("atlas.interest.list")["interests"][0]
    assert native_read("atlas.interest.get") == native_read("atlas.interest.list")
    assert interest["id"] == ids["interest"]
    assert interest["liking"] == 85.123456789
    assert interest["skill_level"] == 44.5
    goals = {goal["id"]: goal for goal in native_read("atlas.goal.list")["goals"]}
    assert goals[ids["goal"]]["progress"] == 31.375
    assert goals[ids["emptyGoal"]]["progress"] is None
    assert goals[ids["goal"]]["blueprint"]["edges"] == [{"sourceId": "draft", "targetId": "check"}]
    graph = native_read("atlas.graph.get", {"goal_id": ids["goal"]})["graph"]
    assert graph == native_json(synthetic_atlas, f"/api/goals/{ids['goal']}/graph")
    assert {ids["goal"], ids["end"], ids["belief"]} <= {node["id"] for node in graph["nodes"]}
    assert graph["edges"][0]["degreePercent"] == 66.6666667


def test_native_discovery_and_every_record_category_are_readable(synthetic_atlas, native_read):
    overview = native_read("atlas.describe")["description"]
    assert "browserOpenapi" in overview["schemas"]
    reference = native_read("atlas.describe", {"section": "api_reference", "path": "/api/records"})["description"]
    assert "/api/records" in reference["browserOpenapi"]["paths"]
    assert native_read("atlas.describe", {"section": "model"})["description"]["endTaxonomy"]
    fields = native_read("atlas.describe", {"section": "custom_fields", "category": "person"})["description"]["custom_fields"]
    assert fields[0]["id"] == synthetic_atlas["ids"]["field"]
    found = []
    cursor = None
    while True:
        page = native_read("atlas.records.search", {"include_data": True, "limit": 3, **({"cursor": cursor} if cursor else {})})
        found.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {"person", "personality", "end", "goal", "belief", "interest", "experience", "project", "resource", "relationship"} <= {record["category"] for record in found}
    resource = next(record for record in found if record["id"] == synthetic_atlas["ids"]["resource"])
    assert resource["data"]["quantity"] == 1.234567891


@pytest.mark.parametrize("category,key", [("experience", "experiences"), ("project", "projects"), ("relationship", "relationships")])
def test_native_legacy_lists_retain_complete_record_data(synthetic_atlas, native_read, category, key):
    record_id = synthetic_atlas["ids"][category]
    result = native_read(f"atlas.{category}.list")[key]
    item = next(row for row in result if row["id"] == record_id)
    full = native_read("atlas.records.get", {"id": record_id})["record"]
    assert item["data"] == full["data"]
    assert item["revision"] == full["revision"]
    assert item["custom_field_values"] == full["customFieldValues"]


def test_native_links_and_complete_record_and_link_history(synthetic_atlas, native_read):
    ids = synthetic_atlas["ids"]
    assert native_read("atlas.links.get", {"id": ids["link"]})["link"] == native_json(synthetic_atlas, f"/api/links/{ids['link']}")
    for kind, record_id, route in (("record", ids["person"], "records"), ("link", ids["link"], "links")):
        expected = native_json(synthetic_atlas, f"/api/{route}/{record_id}/revisions")
        first = native_read("atlas.history.list", {"kind": kind, "id": record_id, "limit": 1})
        assert first["total"] == len(expected) == 2
        second = native_read("atlas.history.list", {"kind": kind, "id": record_id, "limit": 1, "cursor": first["next_cursor"]})
        assert first["items"] + second["items"] == expected
        assert second["next_cursor"] is None


def test_native_states_personality_and_knowledge(synthetic_atlas, native_read):
    ids = synthetic_atlas["ids"]
    at = synthetic_atlas["at"]
    states = native_read("atlas.states.get", {"at": at})["states"]
    assert states == native_json(synthetic_atlas, f"/api/states?at={at}")
    configuration = native_read("atlas.states.get", {"view": "configuration"})["states"]
    assert configuration == native_json(synthetic_atlas, "/api/states/configuration")
    history = native_read("atlas.history.list", {"kind": "states"})
    assert history["items"][0]["eventId"] == "synthetic-manual-estimate"
    assert history["items"] == native_json(synthetic_atlas, "/api/states/events")["events"]
    personality = native_read("atlas.personality.get")["personality"]
    assert personality == native_json(synthetic_atlas, "/api/personality")
    spectrum = next(row for row in personality["record"]["data"]["spectra"] if row["key"] == "sociability")
    assert spectrum["score"] == 12.3456789
    assert spectrum["confidence"] == 0.7654321
    node = native_read("atlas.knowledge.read", {"view": "node", "node_id": ids["knowledgeRoot"]})["data"]
    assert node == native_json(synthetic_atlas, f"/api/knowledge/nodes/{ids['knowledgeRoot']}")
    assert node["node"]["terms"][0]["definition"] == "Synthetic data"
    tree = native_read("atlas.knowledge.read", {"view": "tree"})["data"]
    assert tree == native_json(synthetic_atlas, "/api/knowledge/tree")
    assert native_read("atlas.knowledge.search", {"keywords": "Synthetic contract"})["nodes"][0]["node_id"] == ids["knowledgeRoot"]
    assert native_read("atlas.knowledge.node.get", {"node_id": ids["knowledgeRoot"]})["node"]["explanation"] == "A synthetic explanation"
    assert ids["knowledgeChild"] in {node["node_id"] for node in native_read("atlas.knowledge.frontier.list")["nodes"]}
    nodes = []
    cursor = None
    while True:
        page = native_read("atlas.knowledge.read", {"limit": 7, **({"cursor": cursor} if cursor else {})})
        nodes.extend(page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert nodes == native_json(synthetic_atlas, "/api/knowledge/nodes")["nodes"]


@pytest.mark.parametrize("kind", ["image", "resume"])
def test_native_attachment_bytes_paging_and_resume_text(synthetic_atlas, native_read, kind):
    ids = synthetic_atlas["ids"]
    attachment_id = ids["image"] if kind == "image" else ids["person"]
    listed = native_read("atlas.attachments.list", {"record_id": ids["experience"] if kind == "image" else ids["person"]})
    if kind == "image":
        assert listed["images"][0]["id"] == ids["image"]
    else:
        assert listed["resume"]["filename"] == "synthetic.pdf"
    expected = base64.b64decode(synthetic_atlas[kind])
    actual = bytearray()
    offset = 0
    digest = hashlib.sha256(expected).hexdigest()
    while True:
        chunk = native_read("atlas.attachments.read", {"kind": kind, "id": attachment_id,
                            "offset": offset, "limit": 29, "sha256": digest})
        actual.extend(base64.b64decode(chunk["content"]))
        offset = chunk["next_offset"]
        if offset is None:
            break
    assert actual == expected
    if kind == "resume":
        text = native_read("atlas.attachments.read", {"kind": "resume", "id": attachment_id, "format": "text"})
        assert text["content"] == "Synthetic resume text for native contract"


def test_native_goal_write_roundtrip_uses_current_schema(synthetic_atlas, native_read):
    created = native_read("atlas.goal.create", {"title": "Synthetic written goal", "success_conditions": "Read roundtrip",
                          "pursuit_status": "active", "commitment": 12.345, "target_date": "2026-12",
                          "blueprint": {"nodes": [{"id": "part", "title": "Part", "progress": 12.75}], "edges": []}})
    goal = created["goal"]
    assert goal["progress"] == 12.75
    assert goal["commitment"] == 12.345
    updated = native_read("atlas.goal.update", {"id": goal["id"], "expected_revision": goal["revision"],
                          "target_date": None, "success_conditions": "Updated synthetic condition"})["goal"]
    assert updated["revision"] == goal["revision"] + 1
    assert updated["target_date"] is None
    assert updated["success_conditions"] == "Updated synthetic condition"
    assert native_read("atlas.records.get", {"id": goal["id"]})["record"]["data"] == updated["data"]
