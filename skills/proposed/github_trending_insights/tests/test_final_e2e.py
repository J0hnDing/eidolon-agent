import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def skill_module():
    spec = importlib.util.spec_from_file_location("github_trending_insights_final", ROOT / "skill.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


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


def article(
    identifier,
    *,
    description="Useful project",
    language="Python",
    stars="1,234",
    forks="56",
    recent="78 stars this week",
):
    description_html = f"<p>{description}</p>" if description is not None else ""
    language_html = f'<span itemprop="programmingLanguage">{language}</span>' if language is not None else ""
    stars_html = f'<a href="/{identifier}/stargazers">{stars}</a>' if stars is not None else ""
    forks_html = f'<a href="/{identifier}/forks">{forks}</a>' if forks is not None else ""
    recent_html = f'<span class="float-sm-right">{recent}</span>' if recent is not None else ""
    return (
        '<article class="Box-row">'
        f'<h2><a href="/{identifier}">{identifier}</a></h2>'
        f"{description_html}{language_html}{stars_html}{forks_html}{recent_html}"
        "</article>"
    )


def analysis(identifier, rank):
    return {
        "identifier": identifier,
        "rank": rank,
        "purpose": f"Purpose of {identifier}",
        "notable_qualities": ["Notable"],
        "likely_use_cases": ["Evaluation"],
        "potential_limitations": ["Requires validation"],
    }


def test_representative_weekly_run_is_ranked_unique_batched_and_structured(skill_module, monkeypatch):
    identifiers = ["org/alpha", "org/beta", "ORG/ALPHA"] + [f"org/repo-{n}" for n in range(3, 14)]
    page = "<html><body>" + article(
        identifiers[0], stars="1.2k", forks="2,345", recent="3.4k stars this week"
    ) + article(
        identifiers[1], description=None, language=None, stars=None, forks=None, recent=None
    ) + "".join(article(identifier) for identifier in identifiers[2:]) + "</body></html>"
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        if request.get_method() == "GET":
            return FakeResponse(page)
        payload = json.loads(request.data)
        repositories = payload["context"]["repositories"]
        returned = [analysis(item["identifier"], item["rank"]) for item in reversed(repositories)]
        return FakeResponse(json.dumps({"response": json.dumps({"analyses": returned})}))

    monkeypatch.setenv("PERSONAL_AGENT_SKILL_ID", "weekly insights")
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://backend.local:8000")
    monkeypatch.setattr(skill_module, "urlopen", fake_urlopen)

    report = skill_module.run({"language": "", "since": "weekly", "limit": 10})

    assert report["status"] == "complete"
    assert report["source"] == {
        "url": "https://github.com/trending?since=weekly",
        "since": "weekly",
        "language": None,
    }
    assert len(report["projects"]) == 10
    assert [item["rank"] for item in report["projects"]] == list(range(1, 11))
    assert [item["identifier"] for item in report["projects"][:3]] == [
        "org/alpha",
        "org/beta",
        "org/repo-3",
    ]
    assert len({item["identifier"].lower() for item in report["projects"]}) == 10
    assert report["projects"][0]["stars"] == 1200
    assert report["projects"][0]["forks"] == 2345
    assert report["projects"][0]["recent_stars"] == 3400
    missing = report["projects"][1]
    assert missing["description"] is None
    assert missing["primary_language"] is None
    assert missing["stars"] is None
    assert missing["forks"] is None
    assert missing["recent_stars"] is None
    assert set(missing["metadata_missing"]) == {
        "description",
        "primary_language",
        "stars",
        "forks",
        "recent_stars",
    }
    for project in report["projects"]:
        assert project["analysis"]["purpose"] == f"Purpose of {project['identifier']}"
    assert report["failures"] == []

    assert len(calls) == 2
    retrievals = [(request, timeout) for request, timeout in calls if request.get_method() == "GET"]
    codex_calls = [(request, timeout) for request, timeout in calls if request.get_method() == "POST"]
    assert len(retrievals) == 1
    assert len(codex_calls) == 1
    assert 0 < retrievals[0][1] == skill_module.SOURCE_TIMEOUT_SECONDS
    request, timeout = codex_calls[0]
    assert 0 < timeout == skill_module.CODEX_TIMEOUT_SECONDS
    assert skill_module.SOURCE_TIMEOUT_SECONDS + timeout < 120
    payload = json.loads(request.data)
    assert len(payload["context"]["repositories"]) == 10
    assert [item["identifier"] for item in payload["context"]["repositories"]] == [
        item["identifier"] for item in report["projects"]
    ]
    assert payload["codex_permissions"] == {"call_response": True, "internet_access": False}


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("late")])
def test_retrieval_failures_are_transparent_and_do_not_call_codex(skill_module, monkeypatch, error):
    calls = []

    def fail_retrieval(request, timeout):
        calls.append((request, timeout))
        raise error

    monkeypatch.setattr(skill_module, "urlopen", fail_retrieval)
    report = skill_module.run({})

    assert len(calls) == 1
    assert report["status"] == "failed"
    assert report["projects"] == []
    assert report["failures"] == [
        {
            "stage": "retrieval",
            "message": f"GitHub Trending retrieval failed: {type(error).__name__}.",
        }
    ]


def test_parsing_failure_is_explicit_and_skips_codex(skill_module, monkeypatch):
    calls = []
    monkeypatch.setattr(skill_module, "_retrieve", lambda url: '<article class="Box-row"><p>No repository link</p></article>')
    monkeypatch.setattr(skill_module, "_call_codex", lambda projects: calls.append(projects))

    report = skill_module.run({})

    assert calls == []
    assert report["status"] == "failed"
    assert report["projects"] == []
    assert all(item["stage"] == "parsing" for item in report["failures"])
    assert any("entry skipped" in item["message"] for item in report["failures"])
    assert any("No repository entries" in item["message"] for item in report["failures"])


@pytest.mark.parametrize("codex_result", [TimeoutError("late"), "not-json", {"wrong": []}])
def test_codex_timeout_or_malformed_response_retains_projects_without_fabrication(
    skill_module, monkeypatch, codex_result
):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        if request.get_method() == "GET":
            return FakeResponse(article("org/one") + article("org/two"))
        if isinstance(codex_result, BaseException):
            raise codex_result
        response = codex_result if isinstance(codex_result, str) else json.dumps(codex_result)
        return FakeResponse(json.dumps({"response": response}))

    monkeypatch.setenv("PERSONAL_AGENT_SKILL_ID", "7")
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://backend.local")
    monkeypatch.setattr(skill_module, "urlopen", fake_urlopen)

    report = skill_module.run({})

    assert len(calls) == 2
    assert len([request for request, _ in calls if request.get_method() == "POST"]) == 1
    assert report["status"] == "partial"
    assert [item["identifier"] for item in report["projects"]] == ["org/one", "org/two"]
    assert [item["analysis"] for item in report["projects"]] == [None, None]
    assert any(item["stage"] == "codex" and "Batched analysis" in item["message"] for item in report["failures"])
    unavailable = [item for item in report["failures"] if "Analysis is unavailable" in item["message"]]
    assert [(item["repository"], item["rank"]) for item in unavailable] == [("org/one", 1), ("org/two", 2)]


def test_partial_codex_results_map_only_valid_items(skill_module, monkeypatch):
    page = article("org/one") + article("org/two") + article("org/three")
    returned = [
        analysis("org/three", 3),
        {"identifier": "org/one", "rank": 1, "purpose": "Incomplete"},
        analysis("unknown/repository", 99),
    ]
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_ID", "7")
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://backend.local")

    def fake_urlopen(request, timeout):
        if request.get_method() == "GET":
            return FakeResponse(page)
        return FakeResponse(json.dumps({"response": json.dumps({"analyses": returned})}))

    monkeypatch.setattr(skill_module, "urlopen", fake_urlopen)
    report = skill_module.run({})

    assert report["status"] == "partial"
    assert [item["analysis"] is not None for item in report["projects"]] == [False, False, True]
    assert report["projects"][2]["analysis"]["purpose"] == "Purpose of org/three"
    messages = [item["message"] for item in report["failures"]]
    assert any("Partial analysis" in message for message in messages)
    assert any("Unmatched analysis" in message for message in messages)
    assert sum("Analysis is unavailable" in message for message in messages) == 2


def test_executable_accepts_json_stdin_and_emits_one_json_object():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "skill.py")],
        input="not valid json",
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )

    output = json.loads(completed.stdout)
    assert completed.stderr == ""
    assert output["status"] == "failed"
    assert output["source"] is None
    assert output["projects"] == []
    assert output["failures"][0]["stage"] == "input"


def test_completed_package_and_backend_manifest_contract():
    for filename in ("skill.py", "SKILL.md", "README.md"):
        path = ROOT / filename
        assert path.is_file()
        assert path.read_text(encoding="utf-8").strip()

    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["interface_type"] == "hidden"
    assert manifest["entrypoint"] == "skill.py"
    assert manifest["instructions_path"] == "SKILL.md"
    assert manifest["tool_ui_schema"] is None
    assert manifest["permissions"] == {
        "network": ["github.com"],
        "filesystem_read": ["./cache"],
        "filesystem_write": ["./cache"],
        "secrets": [],
        "shell": False,
        "codex": {"call_response": True, "internet_access": False},
    }
    assert manifest["schedule"] == {
        "type": "weekly",
        "day": "monday",
        "time": "09:00",
        "timezone": "America/Toronto",
        "input": {"language": "", "since": "weekly", "limit": 10},
    }
    assert manifest["output_schema"]["required"] == ["status", "source", "projects", "failures"]
    assert manifest["output_schema"]["properties"]["projects"]["maxItems"] == 10
