import json

from app.services import atlas_provider as provider_module
from app.services.atlas_provider import UrllibAtlasProviderAdapter
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
        record("root", "goal", "Root", {"importance": "high", "description": "Top"}, parentId=None, position=0),
        record("second", "goal", "Second", {}, parentId="root", position=1),
        record("first", "goal", "First", {"horizon": "short"}, parentId="root", position=0),
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
    assert result["progressions"] == [
        {
            "parent_goal_id": "root",
            "subgoal_ids": ["first", "second"],
            "edges": [{"prerequisite_goal_id": "first", "dependent_goal_id": "second"}],
        }
    ]
    assert "/api/goals/root/progression" in calls


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
