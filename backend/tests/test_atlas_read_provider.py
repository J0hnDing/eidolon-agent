import base64
import copy
import hashlib
import io
import json
import zipfile
from urllib.error import HTTPError, URLError

import pytest
from jsonschema import Draft202012Validator

from app.integrations.atlas_read import ATLAS_READ_SPECS
from app.services import atlas_read_provider as read_module
from app.services.atlas_provider import FakeAtlasProviderAdapter, UrllibAtlasProviderAdapter
from app.services.github_provider import IntegrationProviderError

SPECS = {spec.id: spec for spec in ATLAS_READ_SPECS}


@pytest.fixture
def native(monkeypatch):
    adapter = UrllibAtlasProviderAdapter()
    routes = {}
    calls = []

    def request(path, body, **options):
        calls.append((path, body, options))
        assert body is None
        assert options == {"method": "GET", "timeout": 15, "max_bytes": 32 * 1024 * 1024}
        assert path in routes, f"Unexpected native request: {path}"
        payload = routes[path]
        return payload() if callable(payload) else copy.deepcopy(payload)

    monkeypatch.setattr(adapter, "_request", request)

    def execute(operation_id, requested=None):
        output = adapter.execute(SPECS[operation_id], requested or {})
        Draft202012Validator(SPECS[operation_id].output_schema).validate(output)
        return output

    return routes, calls, execute


def record(record_id="person-1", **extra):
    return {"id": record_id, "category": "person", "title": "Example Person", "revision": 7,
            "trashed": False, "createdAt": "2026-10-01T00:00:00.000Z", "updatedAt": "2026-10-15T01:02:03.000Z",
            "data": {"pronouns": "they/them", "passportNumber": "explicitly-requested-fixture",
                     "physicalHealth": {"measurements": [{"value": 70.123456789}]},
                     "provenance": {"attribution": "stated", "evidence": []}},
            "customFieldValues": {"score": 0.123456789123456, "flag": False, "unknown": None},
            "links": [{"id": "outgoing", "targetId": "other"}], "backlinks": [{"id": "incoming"}], **extra}


def test_describe_discovers_native_schemas_catalogs_and_custom_fields(native):
    routes, calls, execute = native
    schemas = {"RecordCategory": {"enum": ["person", "interest", "end", "resource"]},
               "PersonData": {"properties": {"physicalHealth": {"type": "object"}}}}
    reference = {"browserOpenapi": {"paths": {"/api/records": {"get": {"operationId": "list_records"}},
                                               "/api/person/completeness": {"get": {}}},
                                      "components": {"schemas": schemas}},
                 "knowledgeOpenapi": {"paths": {"/api/knowledge/nodes": {"get": {}}}, "components": {}}}
    routes["/api/settings/api-reference"] = reference
    routes["/api/model/catalog"] = {"selfQuestions": [{"id": "self_concept"}], "resourceCurrencies": ["CAD"]}
    fields = [{"id": "custom", "category": "person", "fieldType": "number", "archived": False}]
    routes["/api/custom-fields?category=person"] = fields
    overview = execute("atlas.describe")["description"]
    assert overview["schemas"]["browserOpenapi"]["schemas"] == schemas
    assert {item["operation"] for item in overview["read_operations"]} == set(SPECS)
    assert "sensitive Person" in overview["guidance"]
    assert execute("atlas.describe", {"section": "api_reference"}) == {"description": reference}
    selected = execute("atlas.describe", {"section": "api_reference", "path": "/api/person/completeness"})
    assert set(selected["description"]) == {"browserOpenapi"}
    assert selected["description"]["browserOpenapi"]["components"]["schemas"] == schemas
    assert list(selected["description"]["browserOpenapi"]["paths"]) == ["/api/person/completeness"]
    assert execute("atlas.describe", {"section": "model"})["description"] == routes["/api/model/catalog"]
    assert execute("atlas.describe", {"section": "custom_fields", "category": "person"}) == {
        "description": {"custom_fields": fields},
    }
    assert len(calls) == 5


def test_full_record_read_keeps_native_precision_unknowns_custom_fields_and_links(native):
    routes, _, execute = native
    full = record()
    routes["/api/records/person-1"] = full
    assert execute("atlas.records.get", {"id": "person-1"}) == {"record": full}


def test_compact_search_pages_without_loading_person_data_and_binds_cursor_to_scope(native):
    routes, _, execute = native
    items = [record(f"person-{index}") for index in range(3)]
    routes["/api/records?category=person&q=A%26B"] = items
    requested = {"category": "person", "query": "A&B", "limit": 2}
    first = execute("atlas.records.search", requested)
    assert first["total"] == 3
    assert [item["id"] for item in first["items"]] == ["person-0", "person-1"]
    assert set(first["items"][0]) == {"id", "category", "title", "revision", "trashed", "createdAt", "updatedAt"}
    assert "explicitly-requested-fixture" not in json.dumps(first)
    assert first["next_cursor"]
    second = execute("atlas.records.search", {**requested, "cursor": first["next_cursor"]})
    assert [item["id"] for item in second["items"]] == ["person-2"]
    assert second["next_cursor"] is None
    routes["/api/records?category=person&q=other"] = items
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.records.search", {**requested, "query": "other", "cursor": first["next_cursor"]})
    assert error.value.error_type == "stale_revision"
    routes["/api/records?category=person&q=A%26B"][0]["revision"] += 1
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.records.search", {**requested, "cursor": first["next_cursor"]})
    assert error.value.error_type == "stale_revision"


def test_complete_search_preserves_every_field_and_binds_include_data(native):
    routes, _, execute = native
    items = [record("a"), record("b")]
    routes["/api/records?trashed=all"] = items
    requested = {"trashed": "all", "limit": 1, "include_data": True}
    first = execute("atlas.records.search", requested)
    assert first["items"] == items[:1]
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.records.search", {**requested, "include_data": False, "cursor": first["next_cursor"]})
    assert error.value.error_type == "stale_revision"
    routes["/api/records?trashed=all"][0]["customFieldValues"]["score"] = 0.2
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.records.search", {**requested, "cursor": first["next_cursor"]})
    assert error.value.error_type == "stale_revision"


@pytest.mark.parametrize("cursor", ["!not-base64!", "bm90LWpzb24=", "e30=", "W10=", "WzEsIC0xXQ==",
                                    "WzEsIHRydWVd", "WzEsIDk5OV0="])
def test_invalid_cursors_are_normalized(native, cursor):
    routes, _, execute = native
    routes["/api/records"] = [record()]
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.records.search", {"cursor": cursor})
    assert error.value.error_type == "invalid_input"


def test_page_byte_bound_returns_cursor_and_never_silently_truncates(native, monkeypatch):
    routes, _, execute = native
    items = [{"id": str(index), "data": "x" * 70} for index in range(4)]
    routes["/api/records"] = items
    monkeypatch.setattr(read_module, "_MAX_PAGE_BYTES", 110)
    collected = []
    cursor = None
    while True:
        page = execute("atlas.records.search", {"include_data": True, **({"cursor": cursor} if cursor else {})})
        collected.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert collected == items
    monkeypatch.setattr(read_module, "_MAX_PAGE_BYTES", 20)
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.records.search", {"include_data": True})
    assert error.value.error_type == "response_too_large"


@pytest.mark.parametrize(("operation", "requested", "path", "key", "payload"), [
    ("atlas.links.get", {"id": "link-1"}, "/api/links/link-1", "link", {"id": "link-1", "degreePercent": 0.123456789}),
    ("atlas.person.completeness.get", {}, "/api/person/completeness", "completeness",
     {"complete": 27, "missing": 73, "areas": [{"id": "profile", "missingFields": ["Pronouns"]}]}),
    ("atlas.states.get", {}, "/api/states", "states", {"estimates": [{"value": 0.123456789, "confidence": 0}]}),
    ("atlas.states.get", {"view": "configuration"}, "/api/states/configuration", "states",
     {"revision": 3, "parameters": {"decay": 0.012345678912345}}),
    ("atlas.states.get", {"at": "2026-10-15T01:02:03+00:00"}, "/api/states?at=2026-10-15T01%3A02%3A03%2B00%3A00",
     "states", {"estimates": []}),
    ("atlas.personality.get", {}, "/api/personality", "personality",
     {"spectra": [{"key": "sociability", "score": None, "confidence": 0}],
      "record": {"data": {"valenceImport": {"parameter": 0.123456789123}}}}),
    ("atlas.graph.get", {}, "/api/goal-graph", "graph", {"nodes": [], "edges": []}),
    ("atlas.graph.get", {"goal_id": "goal-1"}, "/api/goals/goal-1/graph", "graph",
     {"nodes": [{"id": "goal-1", "connectionStatus": "missing_end"}], "edges": []}),
])
def test_fixed_native_get_routes_keep_complete_payloads(native, operation, requested, path, key, payload):
    routes, calls, execute = native
    routes[path] = payload
    assert execute(operation, requested) == {key: payload}
    assert [call[0] for call in calls] == [path]


@pytest.mark.parametrize(("kind", "path"), [("record", "/api/records/record-1/revisions"),
                                             ("link", "/api/links/record-1/revisions")])
def test_history_pages_complete_record_and_link_revisions(native, kind, path):
    routes, _, execute = native
    revisions = [{"id": "record-1", "revision": 2, "data": {"exact": 0.123456789}},
                 {"id": "record-1", "revision": 1, "data": {"old": True}}]
    routes[path] = revisions
    first = execute("atlas.history.list", {"kind": kind, "id": "record-1", "limit": 1})
    second = execute("atlas.history.list", {"kind": kind, "id": "record-1", "cursor": first["next_cursor"]})
    assert first["items"] + second["items"] == revisions
    assert second["next_cursor"] is None


def test_states_history_follows_actual_native_event_pages_then_returns_bounded_pages(native):
    routes, calls, execute = native
    events = [{"eventId": str(index), "type": "observation", "observations": {"mental_energy": .123456789}}
              for index in range(103)]
    for limit, offset in ((1, 0), (100, 0), (25, 100)):
        routes[f"/api/states/events?limit={limit}&offset={offset}"] = {
            "events": events[offset:offset + limit], "total": 103, "limit": limit, "offset": offset,
        }
    first = execute("atlas.history.list", {"kind": "states", "limit": 100})
    second = execute("atlas.history.list", {"kind": "states", "cursor": first["next_cursor"]})
    assert first["items"] + second["items"] == events
    assert first["total"] == 103
    assert second["next_cursor"] is None
    assert [call[0] for call in calls] == ["/api/states/events?limit=1&offset=0",
                                          "/api/states/events?limit=100&offset=0",
                                          "/api/states/events?limit=1&offset=0",
                                          "/api/states/events?limit=25&offset=100"]


def test_states_history_rejects_intervening_native_writes(native):
    routes, _, execute = native
    routes["/api/states/events?limit=1&offset=0"] = {
        "events": [{"eventId": "0"}], "total": 101, "limit": 1, "offset": 0,
    }
    routes["/api/states/events?limit=25&offset=0"] = {
        "events": [{"eventId": "new"}] + [{"eventId": str(index)} for index in range(24)],
        "total": 102, "limit": 25, "offset": 0,
    }
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.history.list", {"kind": "states"})
    assert error.value.error_type == "stale_revision"


def test_states_cursor_detects_intervening_append_without_refetching_full_history(native):
    routes, _, execute = native
    first = {"events": [{"eventId": "newest"}], "total": 20, "limit": 1, "offset": 0}
    routes["/api/states/events?limit=1&offset=0"] = first
    result = execute("atlas.history.list", {"kind": "states", "limit": 1})
    assert result["total"] == 20
    first["total"] = 21
    first["events"] = [{"eventId": "new-append"}]
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.history.list", {"kind": "states", "cursor": result["next_cursor"]})
    assert error.value.error_type == "stale_revision"


def test_knowledge_native_envelopes_preserve_connections_and_recursive_tree(native):
    routes, _, execute = native
    nodes = [{"id": 1, "name": "Subject", "terms": [{"id": "term", "definition": "Definition"}]},
             {"id": 2, "name": "Child", "parentId": 1}]
    routes["/api/knowledge/nodes"] = {"nodes": nodes}
    detail = {"node": {**nodes[0], "childCount": 1,
                       "connections": [{"connectionId": 7, "id": 2, "name": "Child"}]}}
    routes["/api/knowledge/nodes/1"] = detail
    tree = {"branches": [{"id": 1, "name": "Subject", "children": [nodes[1]]}]}
    routes["/api/knowledge/tree"] = tree
    page = execute("atlas.knowledge.read", {"limit": 1})
    final = execute("atlas.knowledge.read", {"cursor": page["next_cursor"]})
    assert page["data"] + final["data"] == nodes
    assert page["total"] == 2
    assert execute("atlas.knowledge.read", {"view": "node", "node_id": 1}) == {
        "data": detail, "next_cursor": None, "total": None,
    }
    assert execute("atlas.knowledge.read", {"view": "tree"}) == {
        "data": tree, "next_cursor": None, "total": None,
    }


@pytest.mark.parametrize(("category", "expected"), [
    ("person", {"images": [], "resume": {"recordId": "r", "filename": "resume.pdf", "byteLength": 31}}),
    ("experience", {"images": [{"id": "image-1", "recordId": "r", "mimeType": "image/png"}], "resume": None}),
    ("goal", {"images": [], "resume": None}),
])
def test_attachment_metadata_uses_only_applicable_native_routes(native, category, expected):
    routes, calls, execute = native
    routes["/api/records/r"] = {"id": "r", "category": category}
    routes["/api/records/r/images"] = expected["images"]
    routes["/api/records/r/resume"] = expected["resume"]
    assert execute("atlas.attachments.list", {"record_id": "r"}) == expected
    assert len(calls) == (1 if category == "goal" else 2)
    assert all(not call[0].endswith("/content") for call in calls)


def test_attachment_bytes_page_exactly_and_sha256_detects_replacement(native, monkeypatch):
    _, calls, execute = native
    raw = b"\x89PNG\x00\xfffixture-image"
    binary_calls = []

    def binary(path):
        binary_calls.append(path)
        return raw, "image/png"

    monkeypatch.setattr(read_module, "_binary", binary)
    first = execute("atlas.attachments.read", {"kind": "image", "id": "image-1", "limit": 5})
    final = execute("atlas.attachments.read", {"kind": "image", "id": "image-1", "offset": first["next_offset"],
                                                "sha256": first["sha256"]})
    assert base64.b64decode(first["content"]) + base64.b64decode(final["content"]) == raw
    assert first["total"] == len(raw)
    assert first["sha256"] == hashlib.sha256(raw).hexdigest()
    assert final["next_offset"] is None
    assert binary_calls == ["/api/images/image-1/content"] * 2
    assert calls == []
    raw = b"replacement"
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.attachments.read", {"kind": "image", "id": "image-1", "sha256": first["sha256"]})
    assert error.value.error_type == "stale_revision"


def test_docx_resume_text_chunks_unicode_without_silent_loss(native, monkeypatch):
    _, _, execute = native
    text = "A résumé π\nA second paragraph"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         '<w:body><w:p><w:r><w:t>A résumé π</w:t></w:r></w:p>'
                         '<w:p><w:r><w:t>A second paragraph</w:t></w:r></w:p></w:body></w:document>')
    raw = buffer.getvalue()
    paths = []

    def binary(path):
        paths.append(path)
        return raw, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    monkeypatch.setattr(read_module, "_binary", binary)
    chunks = []
    offset = 0
    while True:
        result = execute("atlas.attachments.read", {"kind": "resume", "id": "person-1", "format": "text",
                                                     "offset": offset, "limit": 7})
        assert result["encoding"] == "text"
        assert result["total"] == len(text)
        chunks.append(result["content"])
        offset = result["next_offset"]
        if offset is None:
            break
    assert "".join(chunks) == text
    assert paths == ["/api/records/person-1/resume/content"] * len(chunks)


def test_pdf_resume_pages_extracted_text_and_rejects_extraction_truncation(native, monkeypatch):
    _, _, execute = native
    monkeypatch.setattr(read_module, "_binary", lambda path: (b"fixture-pdf", "application/pdf"))
    monkeypatch.setattr(read_module, "extract_pdf_text", lambda raw, limit: "page one\npage two")
    result = execute("atlas.attachments.read", {"kind": "resume", "id": "person-1", "format": "text", "limit": 8})
    assert result["content"] == "page one"
    assert result["next_offset"] == 8
    monkeypatch.setattr(read_module, "extract_pdf_text", lambda raw, limit: "x" * limit)
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.attachments.read", {"kind": "resume", "id": "person-1", "format": "text"})
    assert error.value.error_type == "response_too_large"


@pytest.mark.parametrize(("operation", "requested"), [
    ("atlas.records.get", {"id": ".."}), ("atlas.records.get", {"id": "."}),
    ("atlas.records.get", {"id": "../../api/setup"}),
    ("atlas.records.get", {"id": "https://example.test"}),
    ("atlas.records.get", {"id": "C:\\private\\data"}),
    ("atlas.records.get", {"id": "%2fapi%2fexport"}),
    ("atlas.links.get", {"id": "link?operation=delete"}),
    ("atlas.records.get", {"id": "r", "path": "/api/setup"}),
    ("atlas.records.search", {"limit": 0}), ("atlas.records.search", {"limit": 101}),
    ("atlas.records.search", {"method": "POST"}), ("atlas.describe", {"url": "https://example.test"}),
    ("atlas.describe", {"path": "/api/records"}), ("atlas.describe", {"section": "model", "category": "person"}),
    ("atlas.history.list", {"kind": "record"}), ("atlas.history.list", {"kind": "states", "id": "r"}),
    ("atlas.states.get", {"view": "configuration", "at": "2026-10-15T00:00:00Z"}),
    ("atlas.knowledge.read", {"view": "node"}), ("atlas.knowledge.read", {"node_id": 1}),
    ("atlas.knowledge.read", {"view": "tree", "cursor": "anything"}),
    ("atlas.attachments.read", {"kind": "image", "id": "i", "format": "text"}),
    ("atlas.attachments.read", {"kind": "resume", "id": "p", "offset": -1}),
    ("atlas.attachments.read", {"kind": "resume", "id": "p", "limit": 131073}),
])
def test_invalid_inputs_cannot_select_paths_urls_writes_or_conflicting_modes(native, operation, requested):
    _, calls, execute = native
    with pytest.raises(IntegrationProviderError) as error:
        execute(operation, requested)
    assert error.value.error_type == "invalid_input"
    assert calls == []


def test_unknown_operation_does_not_issue_transport_request():
    calls = []
    provider = read_module.AtlasReadProvider(lambda *args, **kwargs: calls.append(args))
    for operation in ("atlas.http", "atlas.records.delete", "atlas.setup", "atlas.unlock", "atlas.export"):
        with pytest.raises(IntegrationProviderError) as error:
            provider.execute(operation, {})
        assert error.value.error_type == "invalid_input"
    assert calls == []


@pytest.mark.parametrize(("operation", "requested", "path", "malformed"), [
    ("atlas.records.get", {"id": "r"}, "/api/records/r", []),
    ("atlas.records.search", {}, "/api/records", {"items": []}),
    ("atlas.records.search", {}, "/api/records", ["invalid record"]),
    ("atlas.knowledge.read", {}, "/api/knowledge/nodes", {"nodes": None}),
    ("atlas.history.list", {"kind": "record", "id": "r"}, "/api/records/r/revisions", [1]),
])
def test_malformed_native_shapes_fail_explicitly(native, operation, requested, path, malformed):
    routes, _, execute = native
    routes[path] = malformed
    with pytest.raises(IntegrationProviderError) as error:
        execute(operation, requested)
    assert error.value.error_type == "provider_unavailable"


def test_nonexistent_discovery_path_cannot_turn_into_arbitrary_native_request(native):
    routes, calls, execute = native
    routes["/api/settings/api-reference"] = {"browserOpenapi": {"paths": {}}}
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.describe", {"section": "api_reference", "path": "http://example.test/api/setup"})
    assert error.value.error_type == "not_found"
    assert [call[0] for call in calls] == ["/api/settings/api-reference"]


def test_attachment_offset_beyond_end_is_rejected_and_end_offset_is_explicitly_empty(native, monkeypatch):
    _, _, execute = native
    monkeypatch.setattr(read_module, "_binary", lambda path: (b"abc", "application/pdf"))
    with pytest.raises(IntegrationProviderError) as error:
        execute("atlas.attachments.read", {"kind": "resume", "id": "p", "offset": 4})
    assert error.value.error_type == "invalid_input"
    result = execute("atlas.attachments.read", {"kind": "resume", "id": "p", "offset": 3})
    assert result["content"] == ""
    assert result["total"] == 3
    assert result["next_offset"] is None


@pytest.mark.parametrize("spec", ATLAS_READ_SPECS, ids=lambda spec: spec.id)
def test_all_fake_read_outputs_and_examples_satisfy_contracts(spec):
    Draft202012Validator.check_schema(spec.input_schema)
    Draft202012Validator.check_schema(spec.output_schema)
    requested = spec.presentation.usage_example["input"]
    Draft202012Validator(spec.input_schema).validate(requested)
    output = FakeAtlasProviderAdapter().execute(spec, requested)
    Draft202012Validator(spec.output_schema).validate(output)


class BinaryResponse:
    status = 200

    def __init__(self, raw, content_type="image/png"):
        self.raw = raw
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def read(self, limit):
        return self.raw[:limit]

    def close(self):
        self.closed = True


def test_binary_transport_allows_the_native_twenty_mebibyte_image_contract(monkeypatch):
    response = BinaryResponse(b"x" * (12 * 1024 * 1024))
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append((request.full_url, request.get_method(), timeout))
            return response

    monkeypatch.setattr(read_module, "build_opener", lambda *handlers: Opener())
    raw, media_type = read_module._binary("/api/images/image-1/content")
    assert len(raw) == 12 * 1024 * 1024
    assert media_type == "image/png"
    assert requests == [("http://127.0.0.1:4817/api/images/image-1/content", "GET", 15)]
    assert response.closed


@pytest.mark.parametrize(("failure", "expected"), [
    (HTTPError("http://127.0.0.1", 423, "locked", {}, None), "atlas_locked"),
    (HTTPError("http://127.0.0.1", 404, "missing", {}, None), "not_found"),
    (HTTPError("http://127.0.0.1", 302, "redirect", {}, None), "provider_unavailable"),
    (TimeoutError(), "provider_timeout"), (URLError("unreachable"), "provider_unavailable"),
])
def test_binary_transport_normalizes_failures(monkeypatch, failure, expected):
    class Opener:
        def open(self, request, timeout):
            raise failure

    monkeypatch.setattr(read_module, "build_opener", lambda *handlers: Opener())
    with pytest.raises(IntegrationProviderError) as error:
        read_module._binary("/api/images/image-1/content")
    assert error.value.error_type == expected
