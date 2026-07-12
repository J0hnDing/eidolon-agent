import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def skill_module():
    spec = importlib.util.spec_from_file_location("github_trending_insights_skill", ROOT / "skill.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def repository_article(
    identifier,
    *,
    description="A useful project",
    language="Python",
    stars="1,234",
    forks="56",
    recent="78 stars this week",
):
    repository_link = f'<h2><a href="/{identifier}">{identifier}</a></h2>' if identifier else "<h2>missing link</h2>"
    description_html = f"<p>{description}</p>" if description is not None else ""
    language_html = f'<span itemprop="programmingLanguage">{language}</span>' if language is not None else ""
    stars_html = f'<a href="/{identifier}/stargazers">{stars}</a>' if identifier and stars is not None else ""
    forks_html = f'<a href="/{identifier}/forks">{forks}</a>' if identifier and forks is not None else ""
    recent_html = f'<span class="float-sm-right">{recent}</span>' if recent is not None else ""
    return (
        '<article class="Box-row">'
        f"{repository_link}{description_html}{language_html}{stars_html}{forks_html}{recent_html}"
        "</article>"
    )


def analysis(identifier, rank):
    return {
        "identifier": identifier,
        "rank": rank,
        "purpose": f"Purpose for {identifier}",
        "notable_qualities": ["Focused"],
        "likely_use_cases": ["Automation"],
        "potential_limitations": ["Needs evaluation"],
    }


class FakeHeaders:
    def get_content_charset(self):
        return "utf-8"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        self.headers = FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


def test_parser_preserves_display_order_uniqueness_limit_and_metadata(skill_module):
    articles = [
        repository_article(
            "owner/first",
            description="First &amp; best",
            language="Rust",
            stars="1.2k",
            forks="2.5k",
            recent="3,456 stars this week",
        ),
        repository_article(
            "owner/second",
            description=None,
            language=None,
            stars=None,
            forks="not available",
            recent=None,
        ),
        repository_article("OWNER/FIRST"),
    ]
    articles.extend(repository_article(f"org/repo-{index}") for index in range(3, 14))

    parser = skill_module.TrendingParser(limit=10)
    parser.feed("<html><body>" + "".join(articles) + "</body></html>")
    parser.close()

    assert len(parser.projects) == 10
    assert [project["rank"] for project in parser.projects] == list(range(1, 11))
    assert [project["identifier"] for project in parser.projects[:3]] == [
        "owner/first",
        "owner/second",
        "org/repo-3",
    ]
    assert len({project["identifier"].lower() for project in parser.projects}) == 10
    first = parser.projects[0]
    assert first["url"] == "https://github.com/owner/first"
    assert first["description"] == "First & best"
    assert first["primary_language"] == "Rust"
    assert (first["stars"], first["forks"], first["recent_stars"]) == (1200, 2500, 3456)
    assert first["metadata_missing"] == []
    second = parser.projects[1]
    assert second["description"] is None
    assert second["primary_language"] is None
    assert second["stars"] is None
    assert second["forks"] is None
    assert second["recent_stars"] is None
    assert set(second["metadata_missing"]) == {
        "description",
        "primary_language",
        "stars",
        "forks",
        "recent_stars",
    }
    assert parser.entry_failures == []


def test_parser_ignores_entries_after_requested_limit(skill_module):
    page = (
        repository_article("owner/first")
        + repository_article("OWNER/FIRST")
        + repository_article(None)
    )

    parser = skill_module.TrendingParser(limit=1)
    parser.feed(page)
    parser.close()

    assert [project["identifier"] for project in parser.projects] == ["owner/first"]
    assert parser.entry_failures == []


def test_run_retains_valid_entries_and_reports_malformed_entries(skill_module, monkeypatch):
    page = repository_article(None) + repository_article("valid/repository", recent="42 stars today")
    monkeypatch.setattr(skill_module, "_retrieve", lambda url: page)
    monkeypatch.setattr(
        skill_module,
        "_call_codex",
        lambda projects: ({(projects[0]["identifier"], projects[0]["rank"]): analysis("valid/repository", 1)}, []),
    )

    report = skill_module.run({"since": "daily", "limit": 10})

    assert report["status"] == "partial"
    assert [project["identifier"] for project in report["projects"]] == ["valid/repository"]
    assert report["projects"][0]["rank"] == 1
    assert report["projects"][0]["recent_stars"] == 42
    assert report["projects"][0]["analysis"]["purpose"] == "Purpose for valid/repository"
    assert any(failure["stage"] == "parsing" and "skipped" in failure["message"] for failure in report["failures"])


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("late"), ValueError("bad domain")])
def test_retrieval_failures_return_structured_failed_report(skill_module, monkeypatch, error):
    def fail_retrieval(url):
        raise error

    monkeypatch.setattr(skill_module, "_retrieve", fail_retrieval)
    report = skill_module.run({})

    assert report["status"] == "failed"
    assert report["projects"] == []
    assert report["source"] == {
        "url": "https://github.com/trending?since=weekly",
        "since": "weekly",
        "language": None,
    }
    assert len(report["failures"]) == 1
    assert report["failures"][0]["stage"] == "retrieval"
    assert type(error).__name__ in report["failures"][0]["message"]


def test_empty_page_skips_codex_and_reports_parsing_failure(skill_module, monkeypatch):
    monkeypatch.setattr(skill_module, "_retrieve", lambda url: "<html><body>No projects</body></html>")
    calls = []
    monkeypatch.setattr(skill_module, "_call_codex", lambda projects: calls.append(projects))

    report = skill_module.run({})

    assert calls == []
    assert report["status"] == "failed"
    assert report["projects"] == []
    assert any(failure["stage"] == "parsing" and "No repository" in failure["message"] for failure in report["failures"])


def test_retrieve_allows_only_https_github_and_uses_bounded_timeout(skill_module, monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse("<html></html>")

    monkeypatch.setattr(skill_module, "urlopen", fake_urlopen)
    assert skill_module._retrieve("https://github.com/trending?since=weekly") == "<html></html>"
    assert len(requests) == 1
    assert requests[0][0].full_url.startswith("https://github.com/")
    assert 0 < requests[0][1] == skill_module.SOURCE_TIMEOUT_SECONDS < 120

    for forbidden in ("http://github.com/trending", "https://example.com/trending", "https://github.com.evil.test/trending"):
        with pytest.raises(ValueError, match="outside the approved"):
            skill_module._retrieve(forbidden)
    assert len(requests) == 1


def test_multiple_projects_use_one_bounded_batched_codex_request(skill_module, monkeypatch):
    projects = [
        {"identifier": "org/one", "rank": 1, "description": None},
        {"identifier": "org/two", "rank": 2, "description": "Two"},
        {"identifier": "org/three", "rank": 3, "description": "Three"},
    ]
    returned = [analysis("org/three", 3), analysis("org/one", 1), analysis("org/two", 2)]
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        envelope = {"response": json.dumps({"analyses": returned})}
        return FakeResponse(json.dumps(envelope))

    monkeypatch.setenv("PERSONAL_AGENT_SKILL_ID", "skill id")
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://backend.local:8000")
    monkeypatch.setattr(skill_module, "urlopen", fake_urlopen)

    mapped, failures = skill_module._call_codex(projects)

    assert failures == []
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "http://backend.local:8000/skills/skill%20id/codex"
    assert request.get_method() == "POST"
    assert 0 < timeout == skill_module.CODEX_TIMEOUT_SECONDS
    assert skill_module.SOURCE_TIMEOUT_SECONDS + timeout < 120
    payload = json.loads(request.data)
    assert payload["context"]["repositories"] == projects
    assert payload["codex_permissions"] == {"call_response": True, "internet_access": False}
    prompt = payload["prompt"]
    for term in ("identifier", "rank", "purpose", "notable_qualities", "likely_use_cases", "potential_limitations"):
        assert term in prompt
    assert "every repository" in prompt.lower()
    assert mapped[("org/one", 1)]["purpose"] == "Purpose for org/one"
    assert mapped[("org/two", 2)]["likely_use_cases"] == ["Automation"]
    assert mapped[("org/three", 3)]["potential_limitations"] == ["Needs evaluation"]


def test_codex_mapping_reports_duplicate_partial_malformed_and_unmatched_items(skill_module, monkeypatch):
    projects = [
        {"identifier": "org/one", "rank": 1},
        {"identifier": "org/two", "rank": 2},
        {"identifier": "org/three", "rank": 3},
    ]
    items = [
        analysis("org/two", 2),
        analysis("org/two", 2),
        {"identifier": "org/one", "rank": 1, "purpose": "Incomplete"},
        "not an object",
        analysis("unknown/repo", 99),
    ]
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_ID", "7")
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://backend.local")
    monkeypatch.setattr(
        skill_module,
        "urlopen",
        lambda request, timeout: FakeResponse(json.dumps({"response": json.dumps({"analyses": items})})),
    )

    mapped, failures = skill_module._call_codex(projects)

    assert set(mapped) == {("org/two", 2)}
    messages = [failure["message"] for failure in failures]
    assert any("Duplicate" in message for message in messages)
    assert any("Partial" in message for message in messages)
    assert any("Malformed" in message for message in messages)
    assert any("Unmatched" in message for message in messages)


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), URLError("unavailable")])
def test_codex_failure_keeps_ranked_repository_data_without_fabricated_analysis(skill_module, monkeypatch, failure):
    monkeypatch.setattr(skill_module, "_retrieve", lambda url: repository_article("org/one"))
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_ID", "7")
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://backend.local")

    def fail_codex(request, timeout):
        raise failure

    monkeypatch.setattr(skill_module, "urlopen", fail_codex)
    report = skill_module.run({})

    assert report["status"] == "partial"
    assert len(report["projects"]) == 1
    assert report["projects"][0]["identifier"] == "org/one"
    assert report["projects"][0]["rank"] == 1
    assert report["projects"][0]["analysis"] is None
    codex_failures = [item for item in report["failures"] if item["stage"] == "codex"]
    assert any("Batched analysis request failed" in item["message"] for item in codex_failures)
    assert any("Analysis is unavailable" in item["message"] for item in codex_failures)


def test_run_maps_out_of_order_results_and_marks_each_missing_analysis(skill_module, monkeypatch):
    page = repository_article("org/one") + repository_article("org/two") + repository_article("org/three")
    monkeypatch.setattr(skill_module, "_retrieve", lambda url: page)
    monkeypatch.setattr(
        skill_module,
        "_call_codex",
        lambda projects: (
            {
                ("org/three", 3): analysis("org/three", 3),
                ("org/one", 1): analysis("org/one", 1),
            },
            [{"stage": "codex", "message": "One response item was malformed."}],
        ),
    )

    report = skill_module.run({})

    assert report["status"] == "partial"
    assert [project["rank"] for project in report["projects"]] == [1, 2, 3]
    assert report["projects"][0]["analysis"]["purpose"] == "Purpose for org/one"
    assert report["projects"][1]["analysis"] is None
    assert report["projects"][2]["analysis"]["purpose"] == "Purpose for org/three"
    unavailable = [failure for failure in report["failures"] if "Analysis is unavailable" in failure["message"]]
    assert unavailable == [
        {"stage": "codex", "repository": "org/two", "rank": 2, "message": "Analysis is unavailable for this repository."}
    ]


def test_manifest_and_documentation_describe_hidden_bounded_weekly_automation():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    skill_doc = (ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()

    assert manifest["interface_type"] == "hidden"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["tool_ui_schema"] is None
    assert manifest["dependencies"] == []
    assert manifest["permissions"] == {
        "network": ["github.com"],
        "filesystem_read": ["./cache"],
        "filesystem_write": ["./cache"],
        "secrets": [],
        "shell": False,
        "codex": {"call_response": True, "internet_access": False},
    }
    assert manifest["schedule"]["type"] == "weekly"
    assert manifest["schedule"]["day"] == "monday"
    assert manifest["schedule"]["time"] == "09:00"
    assert manifest["schedule"]["timezone"]
    assert manifest["schedule"]["input"] == {"language": "", "since": "weekly", "limit": 10}

    for text in (skill_doc, readme):
        assert "hidden" in text
        assert "monday" in text and "09:00" in text
        assert "one" in text and "codex" in text and "request" in text
        assert "metadata_missing" in text
        assert "null" in text
        assert "github.com" in text
        assert "shell" in text
        assert "browser automation" in text
        assert "third-party" in text
    for field in ("purpose", "notable qualities", "likely use cases", "potential limitations"):
        assert field in readme
