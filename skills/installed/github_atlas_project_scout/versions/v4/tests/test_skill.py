import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
for parent in Path(__file__).resolve().parents:
    backend_dir = parent / "backend"
    if (backend_dir / "function_runtime_capabilities.py").is_file():
        sys.path.insert(0, str(backend_dir))
        break
sys.path.insert(0, str(SKILL_DIR))

import skill  # noqa: E402

from integration_test_adapter import DeterministicFakeIntegrationAdapter  # noqa: E402


def _github_result(*, repositories=None):
    return {
        "ranking": "github_trending",
        "period": "weekly",
        "language": "python",
        "repositories": repositories or [],
        "truncated": False,
    }


def _repository(*, full_name="example/new-tool", description="A useful new tool"):
    return {
        "rank": 1,
        "full_name": full_name,
        "description": description,
        "language": "Python",
        "html_url": f"https://github.com/{full_name}",
        "stars": 120,
        "forks": 15,
        "stars_gained": 40,
        "readme": "# New Tool\nA local project knowledge utility.",
        "readme_truncated": False,
    }


def _codex_response(analyses):
    return {
        "response": json.dumps({"analyses": analyses}),
        "model": "test-model",
        "internet_access": False,
    }


def test_run_returns_only_github_facts_and_parsed_analysis(monkeypatch):
    repository = _repository()
    project = {
        "title": "Atlas Companion",
        "description": "Local project knowledge",
        "status": "active",
        "githubLink": "https://github.com/example/atlas-companion",
    }
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=[repository]),
        skill.ATLAS_OPERATION: {"projects": [project]},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    codex_calls = []
    analysis = (
        "The project packages local knowledge as a focused utility. "
        "Its narrow interface may offer Atlas a useful pattern for simpler onboarding."
    )

    def fake_codex(**kwargs):
        codex_calls.append(kwargs)
        return _codex_response({repository["full_name"]: analysis})

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", fake_codex)
    result = skill.run({
        "limit": 10,
        "language": "python",
        "period": "weekly",
        "atlas_keywords": ["local", "knowledge"],
        "analysis_focus": "Prioritize onboarding lessons.",
    })

    assert result == {
        "repositories": [{
            "name": repository["full_name"],
            "url": repository["html_url"],
            "stars": repository["stars"],
            "description": repository["description"],
            "analysis": analysis,
        }]
    }
    assert set(result["repositories"][0]) == {"name", "url", "stars", "description", "analysis"}
    assert [call["operation"] for call in adapter.calls] == [
        "github.repository.trending.list",
        "atlas.project.list",
    ]
    assert adapter.calls[0]["input"] == {"period": "weekly", "limit": 10, "language": "python"}
    assert adapter.calls[1]["input"] == {"limit": 100, "keywords": "local knowledge"}
    assert len(codex_calls) == 1
    assert codex_calls[0]["internet_access"] is False
    assert codex_calls[0]["context"]["repositories"] == [repository]
    assert codex_calls[0]["context"]["atlas_projects"] == [project]


def test_empty_github_result_skips_unneeded_atlas_and_codex_calls(monkeypatch):
    adapter = DeterministicFakeIntegrationAdapter({skill.GITHUB_OPERATION: _github_result()})
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)

    def unexpected_codex(**kwargs):
        raise AssertionError("Codex must not run when there are no repositories")

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", unexpected_codex)

    assert skill.run({}) == {"repositories": []}
    assert [call["operation"] for call in adapter.calls] == [skill.GITHUB_OPERATION]


def test_invalid_codex_shape_returns_repository_with_explicit_fallback(monkeypatch):
    repository = _repository(description=None)
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=[repository]),
        skill.ATLAS_OPERATION: {"projects": []},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **kwargs: {
        "response": "not json",
        "model": None,
        "internet_access": False,
    })

    result = skill.run({})

    assert result == {
        "repositories": [{
            "name": repository["full_name"],
            "url": repository["html_url"],
            "stars": repository["stars"],
            "description": None,
            "analysis": skill.ANALYSIS_FALLBACK,
        }]
    }


def test_keyword_filter_and_github_limit_are_bounded(monkeypatch):
    repository = _repository()
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=[repository]),
        skill.ATLAS_OPERATION: {"projects": []},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **kwargs: _codex_response({repository["full_name"]: "No clear Atlas relevance."}),
    )

    skill.run({"limit": 50, "atlas_keywords": ["a" * 150, "b" * 100]})

    assert adapter.calls[0]["input"]["limit"] == 25
    assert adapter.calls[1]["input"]["keywords"] == "a" * 150


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"analyses": []},
        {"analyses": {}},
        {"analyses": {"example/new-tool": ""}},
        {"analyses": {"example/new-tool": "ok", "extra/repo": "extra"}},
    ],
)
def test_parse_analyses_rejects_malformed_or_mismatched_results(response):
    with pytest.raises(ValueError):
        skill._parse_analyses(json.dumps(response), [_repository()])
