import json

import pytest
from jsonschema import Draft202012Validator

from app.services import atlas_provider as provider_module
from app.services.atlas_provider import UrllibAtlasProviderAdapter
from app.services.github_provider import IntegrationProviderError
from app.services.integration_registry import OPERATIONS


def record(record_id, category, title, data, **extra):
    return {
        "id": record_id,
        "category": category,
        "title": title,
        "data": data,
        "createdAt": extra.pop("createdAt", "2026-01-01T00:00:00.000Z"),
        **extra,
    }


def test_native_person_projection_excludes_sensitive_fields() -> None:
    adapter = UrllibAtlasProviderAdapter()
    payload = [
        record(
            "person-1",
            "person",
            "Jane Doe",
            {
                "preferredName": "Jane",
                "emails": ["jane@example.test"],
                "summary": "Builder",
                "passportNumber": "SECRET-PASSPORT",
                "nationalId": "SECRET-ID",
            },
        )
    ]
    adapter._request = lambda *args, **kwargs: payload  # type: ignore[method-assign]  # noqa: SLF001

    result = adapter.execute(OPERATIONS["atlas.person.get"], {})

    assert result["personal_info"]["name"] == "Jane Doe"
    assert result["personal_info"]["emails"] == ["jane@example.test"]
    assert "passportNumber" not in json.dumps(result)
    assert "SECRET-ID" not in json.dumps(result)


def test_native_interest_projection_returns_hobbies_and_preferences_without_metadata() -> None:
    adapter = UrllibAtlasProviderAdapter()
    interests = [
        record(
            "preference-1",
            "interest",
            "Communication",
            {
                "kind": "preference",
                "domain": "communication",
                "value": "Async by default",
                "strength": "strong",
                "context": "Deep work",
                "rationale": "Protects focus",
                "effectiveFrom": "2026-01",
                "effectiveTo": None,
                "privateMetadata": "exclude",
            },
            revision=4,
        ),
        record(
            "hobby-1",
            "interest",
            "Photography",
            {
                "kind": "hobby",
                "description": "Street photography",
                "engagement": "regular",
                "skillLevel": "advanced",
                "started": "2020-05",
                "notes": "Photo walks",
            },
        ),
    ]
    adapter._request = lambda *args, **kwargs: interests  # type: ignore[method-assign]  # noqa: SLF001

    result = adapter.execute(OPERATIONS["atlas.interest.get"], {})
    assert adapter.execute(OPERATIONS["atlas.interest.list"], {}) == result

    assert result == {
        "hobbies": [
            {
                "title": "Photography",
                "description": "Street photography",
                "engagement": "regular",
                "skill_level": "advanced",
                "started": "2020-05",
                "notes": "Photo walks",
            }
        ],
        "preferences": [
            {
                "title": "Communication",
                "domain": "communication",
                "value": "Async by default",
                "strength": "strong",
                "context": "Deep work",
                "rationale": "Protects focus",
                "effective_from": "2026-01",
                "effective_to": None,
            }
        ],
    }
    serialized = json.dumps(result)
    for field in ("id", "category", "kind", "createdAt", "revision", "privateMetadata"):
        assert field not in serialized


def test_native_experience_and_project_normalization_preserves_ordering() -> None:
    adapter = UrllibAtlasProviderAdapter()
    experiences = [
        record("older", "experience", "Older", {"startDate": "2020-01", "narrative": "Earlier"}),
        record(
            "newer",
            "experience",
            "Newer",
            {"startDate": "2024-01", "endDate": "", "ongoing": True, "narrative": "Later"},
        ),
    ]
    projects = [
        record("z", "project", "Zulu", {"context": "Z", "status": "active"}),
        record("a", "project", "alpha", {"context": "A", "githubLink": "https://example.test/a"}),
    ]
    adapter._request = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda path, *_args, **_kwargs: experiences if "experience" in path else projects
    )

    experience_result = adapter.execute(OPERATIONS["atlas.experience.list"], {})
    project_result = adapter.execute(OPERATIONS["atlas.project.list"], {})

    assert [item["title"] for item in experience_result["experiences"]] == ["Newer", "Older"]
    assert experience_result["experiences"][0]["time"] == {
        "start_date": "2024-01",
        "end_date": None,
        "ongoing": True,
    }
    assert [item["title"] for item in project_result["projects"]] == ["alpha", "Zulu"]


def test_native_goals_rebuild_hierarchy_and_progression_edges() -> None:
    adapter = UrllibAtlasProviderAdapter()
    goals = [
        record("root", "goal", "Root", {"importance": "high", "description": "Top"}, parentId=None, position=0, progress=40, revision=1),
        record("second", "goal", "Second", {"result": "Delivered"}, parentId="root", position=1, progress=60, revision=2),
        record("first", "goal", "First", {"horizon": "short"}, parentId="root", position=0, progress=20, revision=3),
    ]
    calls = []

    def request(path, *_args, **_kwargs):
        calls.append(path)
        if path.startswith("/api/records"):
            return goals
        return {"dependencies": [{"goalId": "second", "prerequisiteId": "first"}]}

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.goal.list"], {})

    assert [item["id"] for item in result["goals"][0]["subgoals"]] == ["first", "second"]
    assert result["goals"][0]["progress"] == 40
    assert result["goals"][0]["revision"] == 1
    assert [item["progress"] for item in result["goals"][0]["subgoals"]] == [20, 60]
    assert result["goals"][0]["subgoals"][1]["result"] == "Delivered"
    assert result["goals"][0]["subgoals"][1]["following_goal_ids"] == ["first"]
    Draft202012Validator(OPERATIONS["atlas.goal.list"].output_schema).validate(result)
    assert result["progressions"] == [
        {
            "parent_goal_id": "root",
            "subgoal_ids": ["first", "second"],
            "edges": [{"prerequisite_goal_id": "first", "dependent_goal_id": "second"}],
        }
    ]
    assert "/api/goals/root/progression" in calls


@pytest.mark.parametrize("progress", [None, True, -1, 101, 10.5])
def test_native_goal_list_rejects_invalid_progress(progress) -> None:
    adapter = UrllibAtlasProviderAdapter()
    adapter._request = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda path, *_args, **_kwargs: [record("root", "goal", "Root", {}, parentId=None, position=0, progress=progress, revision=1)]
    )
    with pytest.raises(IntegrationProviderError, match="invalid Goal progress"):
        adapter.execute(OPERATIONS["atlas.goal.list"], {})


def test_goal_create_supports_top_level_and_idempotent_subgoal() -> None:
    adapter = UrllibAtlasProviderAdapter()
    parent = record("parent", "goal", "Plan", {"horizon": "long"}, parentId=None, revision=1, trashed=False)
    created = record(
        "child", "goal", "Draft", {"horizon": "long", "importance": "medium", "description": "",
                                   "targetDate": "", "progress": 100, "result": "Done"},
        parentId="parent", revision=1, trashed=False,
    )
    calls = []

    def request(path, body, *, method, **_kwargs):
        calls.append((method, path, body))
        if method == "GET" and path == "/api/records/parent":
            return parent
        if method == "POST" and path.startswith("/api/goals/"):
            return {"record": created, "created": False}
        if path == "/api/goals/child/progression":
            return {"goal": {"id": "child", "progress": 100}, "nodes": [], "dependencies": []}
        if path == "/api/goals/parent/progression":
            return {"dependencies": [{"goalId": "child", "prerequisiteId": "first"}]}
        raise AssertionError(path)

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.goal.create"], {
        "title": "Draft", "parent_goal_id": "parent", "following_goal_ids": ["first"],
        "progress": 100, "result": "Done", "request_id": "11111111-1111-4111-8111-111111111111",
    })
    assert result["created"] is False
    assert result["goal"]["horizon"] == "long"
    assert result["goal"]["following_goal_ids"] == ["first"]
    assert result["goal"]["result"] == "Done"
    assert calls[1] == ("POST", "/api/goals/parent/subgoals", {
        "requestId": "11111111-1111-4111-8111-111111111111", "title": "Draft",
        "data": created["data"], "customFieldValues": {}, "prerequisiteIds": ["first"],
    })
    Draft202012Validator(OPERATIONS["atlas.goal.create"].output_schema).validate(result)


def test_goal_create_top_level_uses_native_record_api() -> None:
    adapter = UrllibAtlasProviderAdapter()
    calls = []

    def request(path, body, *, method, **_kwargs):
        calls.append((method, path, body))
        if method == "POST":
            return record("root", "goal", "Plan", body["data"], parentId=None, revision=1, trashed=False)
        return {"goal": {"id": "root", "progress": 0}, "dependencies": []}

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.goal.create"], {"title": "Plan"})
    assert calls[0] == ("POST", "/api/records", {
        "category": "goal", "title": "Plan", "customFieldValues": {},
        "data": {"horizon": "short", "importance": "medium", "targetDate": "", "description": "", "progress": 0},
    })
    assert result["goal"]["parent_goal_id"] is None
    assert result["goal"]["progress"] == 0
    assert result["created"] is True
    Draft202012Validator(OPERATIONS["atlas.goal.create"].output_schema).validate(result)


def test_goal_update_preserves_other_data_and_updates_result_and_following_goals() -> None:
    adapter = UrllibAtlasProviderAdapter()
    current = record(
        "leaf", "goal", "Write report",
        {"horizon": "short", "importance": "high", "description": "Keep this", "progress": 20, "result": ""},
        parentId="parent", revision=4, trashed=False,
    )
    calls = []

    def request(path, body, *, method, **_kwargs):
        calls.append((method, path, body))
        if method == "GET":
            if path == "/api/records/leaf":
                return current
            if path == "/api/goals/leaf/progression":
                return {"goal": {"id": "leaf", "progress": 100}, "nodes": []}
            return {"dependencies": [{"goalId": "leaf", "prerequisiteId": "first"}]}
        return {**current, "title": body["title"], "revision": 5, "data": body["data"]}

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(
        OPERATIONS["atlas.goal.update"], {
            "id": "leaf", "expected_revision": 4, "title": "Submit report",
            "progress": 100, "result": "Delivered", "following_goal_ids": ["first"],
        }
    )
    assert calls == [
        ("GET", "/api/records/leaf", None),
        ("PATCH", "/api/records/leaf", {
            "revision": 4,
            "title": "Submit report", "prerequisiteIds": ["first"],
            "data": {"horizon": "short", "importance": "high", "description": "Keep this", "progress": 100, "result": "Delivered"},
        }),
        ("GET", "/api/goals/leaf/progression", None),
        ("GET", "/api/goals/parent/progression", None),
    ]
    assert result["goal"]["title"] == "Submit report"
    assert result["goal"]["revision"] == 5
    assert result["goal"]["progress"] == 100
    assert result["goal"]["result"] == "Delivered"
    assert result["goal"]["following_goal_ids"] == ["first"]
    Draft202012Validator(OPERATIONS["atlas.goal.update"].output_schema).validate(result)


@pytest.mark.parametrize("reason", ["top_result", "stale", "trashed", "empty"])
def test_goal_update_rejects_unusable_goal_without_patching(reason) -> None:
    adapter = UrllibAtlasProviderAdapter()
    current = record("leaf", "goal", "Goal", {"horizon": "short", "progress": 20}, revision=4, trashed=reason == "trashed")
    methods = []

    def request(path, _body, *, method, **_kwargs):
        methods.append(method)
        return current

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    expected_revision = 3 if reason == "stale" else 4
    extra = {} if reason == "empty" else {"result": "Done"} if reason == "top_result" else {"progress": 50}
    with pytest.raises(IntegrationProviderError) as error:
        adapter.execute(OPERATIONS["atlas.goal.update"], {
            "id": "leaf", "expected_revision": expected_revision, **extra,
        })
    assert error.value.error_type == ("stale_revision" if reason == "stale" else "invalid_input")
    assert "PATCH" not in methods


def test_native_request_never_sends_authorization_header(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        def read(self, _size):
            return b"[]"

        def close(self):
            return None

    class Opener:
        def open(self, request, timeout):
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(provider_module, "build_opener", lambda *_args: Opener())
    adapter = UrllibAtlasProviderAdapter()

    adapter._request("/api/records?category=person", None, timeout=1, max_bytes=100, method="GET")  # noqa: SLF001

    assert "Authorization" not in captured["headers"]
