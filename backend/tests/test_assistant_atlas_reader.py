import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.services.assistant_atlas_reader import initial_tasks, read_task
from app.services.integration_service import IntegrationError

AT = "2026-10-16T13:00:00+00:00"


class Atlas:
    def __init__(self):
        self.handlers = {}
        self.calls = []

    def execute_context(self, context, operation, arguments):
        assert context.principal_kind == "agent" and context.agent_id == "assistant"
        assert context.origin == "backend"
        assert context.agent_session_id == 17 and context.agent_turn_id == 29
        assert context.initiating_action == "assistant.atlas_initialization"
        self.calls.append((operation, dict(arguments)))
        return SimpleNamespace(output=self.handlers[operation](arguments))


@pytest.fixture
def atlas(monkeypatch):
    source = Atlas()
    monkeypatch.setattr("app.services.assistant_atlas_reader.build_default_integration_service", lambda db: source)
    return source


def read(task):
    # An opaque DB sentinel also proves this reader never persists source bodies.
    return read_task(object(), task, session_id=17, turn_id=29, at=AT)


def task(operation, **arguments):
    return {"operation": operation, "input": arguments}


def test_initial_tasks_cover_current_atlas_without_archives():
    tasks = initial_tasks(AT)
    assert tasks == [
        task("atlas.describe", section="overview"), task("atlas.describe", section="model"),
        task("atlas.describe", section="custom_fields"),
        task("atlas.records.search", include_data=False, trashed="false", limit=100),
        task("atlas.graph.get"), task("atlas.states.get", view="current"),
        task("atlas.states.get", view="configuration"), task("atlas.history.list", kind="states", limit=100),
        task("atlas.personality.get"), task("atlas.person.completeness.get"),
        task("atlas.knowledge.read", view="nodes", limit=100),
    ]


def test_records_search_pages_expand_full_record_reads(atlas):
    atlas.handlers["atlas.records.search"] = lambda args: {
        "items": [{"id": "person-2", "revision": 6}] if args.get("cursor")
        else [{"id": "person-1", "revision": 3}],
        "next_cursor": None if args.get("cursor") else "next-page", "total": 2,
    }
    first = read(task("atlas.records.search", include_data=False, trashed="false", limit=100))
    assert first["next_tasks"] == [task("atlas.records.get", id="person-1"),
                                  task("atlas.records.search", include_data=False, trashed="false", limit=100,
                                       cursor="next-page")]
    second = read(first["next_tasks"][1])
    assert second["next_tasks"] == [task("atlas.records.get", id="person-2")]


def test_complete_record_preserves_all_native_fields_and_discovers_links_and_attachments(atlas):
    record = {"id": "me", "category": "person", "revision": 9,
              "data": {"arbitrary_new_field": {"secret": "only in model input"}, "precise": 1.234567891},
              "provenance": {"source": "user", "confidence": 0.58},
              "links": [{"id": "out", "anything": "embedded"}], "backlinks": [{"id": "in"}]}
    atlas.handlers["atlas.records.get"] = lambda args: {"record": record}
    result = read(task("atlas.records.get", id="me"))
    assert result["data"] == {"record": record}
    assert result["next_tasks"] == [task("atlas.attachments.list", record_id="me"),
                                    task("atlas.links.get", id="out"), task("atlas.links.get", id="in")]
    assert result["continuation"] is None
    assert "only in model input" not in str(result["next_tasks"])


def test_knowledge_pages_fetch_individual_nodes_including_cross_connections(atlas):
    atlas.handlers["atlas.knowledge.read"] = lambda args: (
        {"data": {"node": {"id": 1, "body": "Full knowledge"},
                  "connections": [{"sourceId": 1, "targetId": 2, "label": "cross branch"}]},
         "next_cursor": None, "total": None} if args.get("view") == "node"
        else {"data": [{"id": 1}], "next_cursor": "next", "total": 2})
    page = read(task("atlas.knowledge.read", view="nodes", limit=100))
    assert page["next_tasks"] == [task("atlas.knowledge.read", view="node", node_id=1),
                                 task("atlas.knowledge.read", view="nodes", limit=100, cursor="next")]
    detail = read(page["next_tasks"][0])
    assert detail["data"]["data"]["node"]["body"] == "Full knowledge"
    assert detail["data"]["data"]["connections"][0]["label"] == "cross branch"
    assert detail["next_tasks"] == []


@pytest.mark.parametrize("operation,key,arguments", [
    ("atlas.describe", "description", {"section": "custom_fields"}),
    ("atlas.graph.get", "graph", {}),
    ("atlas.states.get", "states", {"view": "configuration"}),
    ("atlas.personality.get", "personality", {}),
    ("atlas.person.completeness.get", "completeness", {}),
    ("atlas.links.get", "link", {"id": "l"}),
])
def test_native_objects_preserve_unknown_nested_fields(atlas, operation, key, arguments):
    value = {key: {"future_native_field": [None, {"value": 1.234567891, "evidence": "full text"}]}}
    atlas.handlers[operation] = lambda args: value
    assert read(task(operation, **arguments))["data"] == value


def test_initialization_current_states_reads_the_native_current_view(atlas):
    atlas.handlers["atlas.states.get"] = lambda args: {"states": {"view": args["view"], "estimate": "current"}}
    current_task = next(item for item in initial_tasks(AT)
                        if item["operation"] == "atlas.states.get" and item["input"].get("view") == "current")
    assert current_task == task("atlas.states.get", view="current")
    result = read(current_task)
    assert atlas.calls == [("atlas.states.get", {"view": "current"})]
    assert result["data"]["states"] == {"view": "current", "estimate": "current"}


def test_obsolete_queued_current_states_timestamp_is_dropped(atlas):
    atlas.handlers["atlas.states.get"] = lambda args: {"states": {"view": args["view"], "estimate": "current"}}
    result = read(task("atlas.states.get", view="current", at="2026-10-17T20:06:22.763702+00:00"))
    assert atlas.calls == [("atlas.states.get", {"view": "current"})]
    assert result["data"]["states"]["estimate"] == "current"


def test_state_history_follows_all_pages(atlas):
    atlas.handlers["atlas.history.list"] = lambda args: {"items": [{"id": "event", "observation": "full"}],
                                                         "next_cursor": "page-2", "total": 101}
    history = read(task("atlas.history.list", kind="states", limit=100))
    assert history["data"]["items"][0]["observation"] == "full"
    assert history["next_tasks"] == [task("atlas.history.list", kind="states", limit=100, cursor="page-2")]


def test_json_slices_have_no_truncation_and_only_final_slice_discovers_children(atlas):
    output = {"record": {"id": "large", "category": "experience", "links": [], "backlinks": [],
                         "data": {"private": "🗺" * 51_000, "tail": "IMPORTANT FINAL VALUE"}}}
    atlas.handlers["atlas.records.get"] = lambda args: output
    current = task("atlas.records.get", id="large")
    pieces = []
    while current:
        result = read(current)
        data = result["data"]
        assert data["format"] == "json_slice"
        assert data["offset"] == sum(len(piece) for piece in pieces)
        pieces.append(data["content"])
        if result["continuation"]:
            assert result["next_tasks"] == []
            assert "🗺" not in str(result["continuation"])
        else:
            assert result["next_tasks"] == [task("atlas.attachments.list", record_id="large")]
        current = result["continuation"]
    text = "".join(pieces)
    assert json.loads(text) == output
    assert len(text) == data["total"]
    assert hashlib.sha256(text.encode()).hexdigest() == data["sha256"]


def test_changed_json_object_restarts_slice_at_zero(atlas):
    output = {"record": {"id": "x", "data": {"large": "a" * 30_000}}}
    atlas.handlers["atlas.records.get"] = lambda args: output
    first = read(task("atlas.records.get", id="x"))
    output["record"]["data"]["large"] = "b" * 30_000
    changed = read(first["continuation"])
    assert changed["data"]["offset"] == 0
    assert changed["data"]["sha256"] != first["data"]["sha256"]


def test_attachment_metadata_discovers_every_image_and_resume(atlas):
    atlas.handlers["atlas.attachments.list"] = lambda args: {
        "images": [{"id": "first"}, {"id": "second"}], "resume": {"filename": "CV.pdf"},
    }
    result = read(task("atlas.attachments.list", record_id="person"))
    assert result["next_tasks"] == [
        task("atlas.attachments.read", kind="image", id="first", format="base64"),
        task("atlas.attachments.read", kind="image", id="second", format="base64"),
        task("atlas.attachments.read", kind="resume", id="person", format="text", limit=24_000),
    ]


def test_resume_reads_full_text_and_pins_hash_across_native_pages(atlas):
    text = "Resume content " * 4_000 + "Tail qualification"
    digest = hashlib.sha256(b"original PDF bytes").hexdigest()

    def resume(arguments):
        offset = arguments.get("offset", 0)
        if offset:
            assert arguments["sha256"] == digest
        end = min(offset + arguments["limit"], len(text))
        return {"content": text[offset:end], "encoding": "text", "media_type": "application/pdf",
                "total": len(text), "next_offset": end if end < len(text) else None, "sha256": digest}

    atlas.handlers["atlas.attachments.read"] = resume
    queue = [task("atlas.attachments.read", kind="resume", id="p", format="text", limit=24_000)]
    contents, fragments = [], []
    while queue:
        result = read(queue.pop(0))
        if result["data"].get("format") == "json_slice":
            fragments.append(result["data"]["content"])
            if result["continuation"] is None:
                contents.append(json.loads("".join(fragments))["content"])
                fragments = []
        else:
            contents.append(result["data"]["content"])
        queue.extend([result["continuation"]] if result["continuation"] else result["next_tasks"])
    assert "".join(contents) == text
    assert all("Resume content" not in str(arguments) for _, arguments in atlas.calls)


def test_empty_resume_text_is_explicitly_unreadable(atlas):
    atlas.handlers["atlas.attachments.read"] = lambda args: {
        "content": "", "encoding": "text", "media_type": "application/pdf", "total": 0,
        "next_offset": None, "sha256": "a" * 64,
    }
    with pytest.raises(IntegrationError, match="no readable text"):
        read(task("atlas.attachments.read", kind="resume", id="p", format="text"))


def image_handler(raw, *, media_type="image/png"):
    digest = hashlib.sha256(raw).hexdigest()

    def output(arguments):
        offset = arguments.get("offset", 0)
        end = min(offset + 37_000, len(raw))
        return {"content": base64.b64encode(raw[offset:end]).decode(), "encoding": "base64",
                "media_type": media_type, "total": len(raw), "next_offset": end if end < len(raw) else None,
                "sha256": digest}

    return output


def test_images_are_real_image_content_with_verified_bytes_and_no_base64_in_data(atlas):
    raw = b"\x89PNG\r\n\x1a\n" + b"image bytes" * 22_000
    atlas.handlers["atlas.attachments.read"] = image_handler(raw)
    result = read(task("atlas.attachments.read", kind="image", id="i", format="base64"))
    assert result["next_tasks"] == [] and result["continuation"] is None
    assert base64.b64decode(result["_initialization_image"]["data"]) == raw
    assert result["_initialization_image"]["mime_type"] == "image/png"
    assert result["data"] == {"attachment": {"kind": "image", "id": "i", "sha256": hashlib.sha256(raw).hexdigest(),
                                             "media_type": "image/png", "size": len(raw)}}
    assert len(atlas.calls) > 1
    assert all(arguments["sha256"] == hashlib.sha256(raw).hexdigest()
               for _, arguments in atlas.calls[1:])


@pytest.mark.parametrize("problem", ["invalid_mime", "mime_mismatch", "hash", "changed", "missing_page", "size"])
def test_images_never_silently_skip_invalid_or_partial_content(atlas, problem):
    raw = (b"not an image" if problem == "mime_mismatch" else b"\x89PNG\r\n\x1a\n") + b"a" * 80_000
    handler = image_handler(raw, media_type="text/html" if problem == "invalid_mime" else "image/png")

    def output(args):
        result = handler(args)
        if problem == "hash":
            result["sha256"] = "b" * 64
        elif problem == "changed" and args.get("offset"):
            result["sha256"] = "b" * 64
        elif problem == "missing_page":
            result["next_offset"] = None
        elif problem == "size":
            result["total"] = 20 * 1024 * 1024 + 1
        return result

    atlas.handlers["atlas.attachments.read"] = output
    with pytest.raises(IntegrationError):
        read(task("atlas.attachments.read", kind="image", id="i", format="base64"))


def test_unavailable_and_stale_source_errors_propagate(atlas):
    def fail(args):
        raise IntegrationError("stale_revision", "Restart Atlas read")

    atlas.handlers["atlas.records.search"] = fail
    with pytest.raises(IntegrationError) as error:
        read(task("atlas.records.search", cursor="stale", limit=100))
    assert error.value.error_type == "stale_revision"


def test_repeating_cursor_is_not_treated_as_complete(atlas):
    atlas.handlers["atlas.history.list"] = lambda args: {"items": [], "next_cursor": "same", "total": 12}
    with pytest.raises(IntegrationError, match="did not advance"):
        read(task("atlas.history.list", kind="states", cursor="same", limit=100))
