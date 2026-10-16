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
                "postalCode": "M5V 1A1",
                "occupation": "Engineer",
                "instagram": "@jane",
                "discord": "jane.123",
                "linkedinLink": "https://www.linkedin.com/in/jane",
                "wechatNumber": "jane-wechat",
                "facebook": "jane.doe",
                "personalWebsite": "https://jane.example.test",
                "summary": "Builder",
                "passportNumber": "SECRET-PASSPORT",
                "physicalHealth": {"measurements": [{"weightKg": 70}]},
                "pronouns": "she/her",
                "nationalId": "SECRET-ID",
            },
        )
    ]
    resume = {"recordId": "person-1", "filename": "resume.pdf", "mimeType": "application/pdf",
              "byteLength": 1024, "createdAt": "2026-01-02T00:00:00.000Z", "updatedAt": "2026-01-03T00:00:00.000Z"}
    completeness = {"overall": {"rate": 0.5}}
    adapter._request = lambda path, *args, **kwargs: (  # type: ignore[method-assign]  # noqa: SLF001
        resume if path.endswith("/resume") else completeness if path.endswith("/completeness") else payload
    )

    result = adapter.execute(OPERATIONS["atlas.person.get"], {})

    assert result["completeness"] == completeness
    assert result["personal_info"]["data"]["physicalHealth"]["measurements"][0]["weightKg"] == 70
    assert result["personal_info"]["data"]["pronouns"] == "she/her"
    assert result["personal_info"]["name"] == "Jane Doe"
    assert result["personal_info"]["emails"] == ["jane@example.test"]
    assert result["personal_info"]["postal_code"] == "M5V 1A1"
    assert result["personal_info"]["occupation"] == "Engineer"
    assert result["personal_info"]["instagram"] == "@jane"
    assert result["personal_info"]["discord"] == "jane.123"
    assert result["personal_info"]["linkedin_link"] == "https://www.linkedin.com/in/jane"
    assert result["personal_info"]["wechat_number"] == "jane-wechat"
    assert result["personal_info"]["facebook"] == "jane.doe"
    assert result["personal_info"]["personal_website"] == "https://jane.example.test"
    assert result["personal_info"]["resume"]["filename"] == "resume.pdf"
    Draft202012Validator(OPERATIONS["atlas.person.get"].output_schema).validate(result)
    assert "passportNumber" not in json.dumps(result)
    assert "SECRET-ID" not in json.dumps(result)


def test_native_person_projection_handles_missing_resume() -> None:
    adapter = UrllibAtlasProviderAdapter()
    calls: list[str] = []

    def request(path, *args, **kwargs):  # noqa: ANN001, ANN202, ARG001
        calls.append(path)
        if path.endswith("/completeness"):
            return {"overall": {"rate": 0}}
        return None if path.endswith("/resume") else [record("person-1", "person", "Jane Doe", {})]

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.person.get"], {})
    assert result["personal_info"]["resume"] is None
    assert calls == ["/api/records?category=person", "/api/records/person-1/resume", "/api/person/completeness"]
    Draft202012Validator(OPERATIONS["atlas.person.get"].output_schema).validate(result)


def test_native_interest_projection_preserves_numeric_ratings_and_data() -> None:
    adapter = UrllibAtlasProviderAdapter()
    interests = [record("i-1", "interest", "Photography", {
        "liking": 85.5, "skillLevel": 40, "provenance": {"attribution": "stated", "evidence": []},
        "facts": [{"claim": "Practices weekly"}],
    }, revision=4, customFieldValues={"format": "digital"})]
    adapter._request = lambda *args, **kwargs: interests  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.interest.get"], {})
    assert adapter.execute(OPERATIONS["atlas.interest.list"], {}) == result
    assert result["interests"][0]["liking"] == 85.5
    assert result["interests"][0]["skill_level"] == 40
    assert result["interests"][0]["data"] == interests[0]["data"]
    assert result["interests"][0]["custom_field_values"] == {"format": "digital"}
    Draft202012Validator(OPERATIONS["atlas.interest.list"].output_schema).validate(result)


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


def test_legacy_record_lists_keep_enriched_native_data_and_custom_fields() -> None:
    adapter = UrllibAtlasProviderAdapter()
    payloads = {
        "experience": [record("e", "experience", "Trip", {"narrative": "Story", "place": "Somewhere", "facts": []},
                              revision=2, customFieldValues={"mood": "happy"})],
        "project": [record("p", "project", "Build", {"context": "Context", "provenance": {"attribution": "stated"}},
                           revision=3, customFieldValues={"team": "solo"})],
        "relationship": [record("r", "relationship", "Alex", {"kind": "friend", "contact": "Alex contact", "facts": []},
                                revision=4, customFieldValues={"met": "School"})],
    }
    adapter._request = lambda path, *_args, **_kwargs: payloads[path.split("category=")[1]]  # type: ignore[method-assign]  # noqa: SLF001
    for category, key in (("experience", "experiences"), ("project", "projects"), ("relationship", "relationships")):
        result = adapter.execute(OPERATIONS[f"atlas.{category}.list"], {})
        assert result[key][0]["data"] == payloads[category][0]["data"]
        assert result[key][0]["custom_field_values"] == payloads[category][0]["customFieldValues"]
        assert result[key][0]["revision"] == payloads[category][0]["revision"]


def test_person_completeness_is_available_without_a_profile() -> None:
    adapter = UrllibAtlasProviderAdapter()
    completeness = {"overall": {"rate": 0}, "sections": []}
    adapter._request = lambda path, *_args, **_kwargs: (  # type: ignore[method-assign]  # noqa: SLF001
        completeness if path.endswith("/completeness") else []
    )
    assert adapter.execute(OPERATIONS["atlas.person.get"], {}) == {
        "personal_info": None, "completeness": completeness,
    }


def native_goal(record_id="goal", **extra):
    return record(record_id, "goal", "Write report", {
        "description": "A report", "successConditions": "Published", "pursuitStatus": "active",
        "commitment": 85, "horizon": "short", "targetDate": "2026-12",
        "provenance": {"attribution": "stated", "evidence": []},
        "blueprint": {"nodes": [{"id": "a", "title": "Draft", "description": "", "progress": 33.5}], "edges": []},
    }, revision=4, trashed=False, progress=33.5, **extra)


def test_native_goals_use_flat_records_and_motivational_graph() -> None:
    adapter = UrllibAtlasProviderAdapter()
    goals = [native_goal(), native_goal("other")]
    graph = {"nodes": goals, "edges": [{"sourceId": "goal", "targetId": "other", "type": "REQUIRES"}]}
    calls = []

    def request(path, *_args, **_kwargs):
        calls.append(path)
        return goals if path.startswith("/api/records") else graph

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.goal.list"], {"horizon": "short", "pursuit_status": "active", "limit": 1})
    assert len(result["goals"]) == 1
    assert result["goals"][0]["progress"] == 33.5
    assert result["goals"][0]["blueprint"]["nodes"][0]["id"] == "a"
    assert result["goals"][0]["pursuit_status"] == "active"
    assert result["graph"] == graph
    assert calls == ["/api/records?category=goal", "/api/goal-graph"]
    Draft202012Validator(OPERATIONS["atlas.goal.list"].output_schema).validate(result)


@pytest.mark.parametrize("progress", [True, -1, 101, float("inf"), float("nan"), "20"])
def test_native_goal_list_rejects_invalid_progress(progress) -> None:
    adapter = UrllibAtlasProviderAdapter()
    goal = {**native_goal(), "progress": progress}
    adapter._request = lambda path, *_args, **_kwargs: (  # type: ignore[method-assign]  # noqa: SLF001
        [goal] if path.startswith("/api/records") else {"nodes": [], "edges": []}
    )
    with pytest.raises(IntegrationProviderError, match="invalid Goal progress"):
        adapter.execute(OPERATIONS["atlas.goal.list"], {})


def test_goal_without_blueprint_progress_is_unknown() -> None:
    goal = {**native_goal(), "progress": None}
    assert UrllibAtlasProviderAdapter._goal_view(goal)["progress"] is None  # noqa: SLF001


def test_goal_create_uses_current_native_model_and_blueprint() -> None:
    adapter = UrllibAtlasProviderAdapter()
    calls = []
    blueprint = native_goal()["data"]["blueprint"]

    def request(path, body, *, method, **_kwargs):
        calls.append((method, path, body))
        return {**native_goal(), "data": body["data"], "customFieldValues": body["customFieldValues"]}

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.goal.create"], {
        "title": "Plan", "blueprint": blueprint, "commitment": 70,
        "pursuit_status": "active", "success_conditions": "Delivered", "custom_field_values": {"tag": "work"},
    })
    assert calls == [("POST", "/api/records", {
        "category": "goal", "title": "Plan", "customFieldValues": {"tag": "work"},
        "data": {"horizon": "short", "description": "", "successConditions": "Delivered",
                 "pursuitStatus": "active", "commitment": 70, "blueprint": blueprint},
    })]
    assert result["goal"]["progress"] == 33.5
    assert result["created"] is True
    Draft202012Validator(OPERATIONS["atlas.goal.create"].output_schema).validate(result)


@pytest.mark.parametrize("old_field", ["progress", "result", "importance", "parent_goal_id", "following_goal_ids", "request_id"])
def test_goal_writes_reject_obsolete_fields_without_transport(old_field) -> None:
    adapter = UrllibAtlasProviderAdapter()
    adapter._request = lambda *_args, **_kwargs: pytest.fail("Unexpected transport")  # type: ignore[method-assign]  # noqa: SLF001
    for operation in ("atlas.goal.create", "atlas.goal.update"):
        with pytest.raises(IntegrationProviderError) as error:
            adapter.execute(OPERATIONS[operation], {"title": "Plan", old_field: "obsolete"})
        assert error.value.error_type == "invalid_input"


def test_goal_update_preserves_other_data_and_clears_target_date() -> None:
    adapter = UrllibAtlasProviderAdapter()
    current = native_goal()
    calls = []

    def request(path, body, *, method, **_kwargs):
        calls.append((method, path, body))
        return current if method == "GET" else {**current, "revision": 5, "data": body["data"]}

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.execute(OPERATIONS["atlas.goal.update"], {
        "id": "goal", "expected_revision": 4, "commitment": 100, "target_date": None,
    })
    assert len(calls) == 2
    assert calls[0] == ("GET", "/api/records/goal", None)
    assert calls[1][2]["revision"] == 4
    assert calls[1][2]["data"]["description"] == "A report"
    assert calls[1][2]["data"]["commitment"] == 100
    assert "targetDate" not in calls[1][2]["data"]
    assert result["goal"]["revision"] == 5
    assert result["goal"]["target_date"] is None
    Draft202012Validator(OPERATIONS["atlas.goal.update"].output_schema).validate(result)


@pytest.mark.parametrize("reason", ["stale", "trashed", "empty"])
def test_goal_update_rejects_unusable_goal_without_patching(reason) -> None:
    adapter = UrllibAtlasProviderAdapter()
    current = {**native_goal(), "trashed": reason == "trashed"}
    methods = []

    def request(path, _body, *, method, **_kwargs):
        methods.append(method)
        return current

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    extra = {} if reason == "empty" else {"commitment": 50}
    with pytest.raises(IntegrationProviderError) as error:
        adapter.execute(OPERATIONS["atlas.goal.update"], {
            "id": "goal", "expected_revision": 3 if reason == "stale" else 4, **extra,
        })
    assert error.value.error_type == ("stale_revision" if reason == "stale" else "invalid_input")
    assert "PATCH" not in methods


@pytest.mark.parametrize("value", ["2026-1", "2026/01", "2026-02-30", "0000", "2026-01junk"])
def test_goal_write_rejects_invalid_partial_date(value) -> None:
    adapter = UrllibAtlasProviderAdapter()
    adapter._request = lambda *_args, **_kwargs: pytest.fail("Unexpected transport")  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(IntegrationProviderError) as error:
        adapter.execute(OPERATIONS["atlas.goal.create"], {"title": "Plan", "target_date": value})
    assert error.value.error_type == "invalid_input"


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
