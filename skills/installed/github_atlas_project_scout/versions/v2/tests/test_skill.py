import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[6] / "backend"
SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(SKILL_DIR))

import skill  # noqa: E402

from integration_test_adapter import DeterministicFakeIntegrationAdapter  # noqa: E402


def _github_result(*, repositories=None, truncated=False):
    return {
        "ranking": "github_trending",
        "period": "weekly",
        "language": "python",
        "repositories": repositories or [],
        "truncated": truncated,
    }


def _codex_response(payload):
    return {"response": json.dumps(payload), "model": "test-model", "internet_access": False}


def test_run_uses_selected_operations_and_one_bounded_codex_call(monkeypatch):
    repository = {
        "rank": 1,
        "full_name": "example/new-tool",
        "description": "A useful new tool",
        "language": "Python",
        "html_url": "https://github.com/example/new-tool",
        "stars": 120,
        "forks": 15,
        "stars_gained": 40,
        "readme": "# New Tool\nA local project knowledge utility.",
        "readme_truncated": False,
    }
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
    expected = {
        "report": "One realistic match was found.",
        "interesting_repositories": [{
            "full_name": "example/new-tool",
            "url": repository["html_url"],
            "summary": "A useful new tool",
            "why_interesting": "Strong early adoption.",
        }],
        "learning_matches": [{
            "atlas_project": "Atlas Companion",
            "github_repository": "example/new-tool",
            "relevance": "Both organize project knowledge.",
            "realistic_lessons": ["Adopt a smaller onboarding path."],
        }],
        "limitations": [],
    }

    def fake_codex(**kwargs):
        codex_calls.append(kwargs)
        return _codex_response(expected)

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", fake_codex)
    result = skill.run({
        "limit": 10,
        "language": "python",
        "period": "weekly",
        "atlas_keywords": ["local", "knowledge"],
        "analysis_focus": "Prioritize onboarding lessons.",
    })

    assert result == expected
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


def test_run_caps_integration_limit_and_returns_valid_empty_result(monkeypatch):
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(),
        skill.ATLAS_OPERATION: {"projects": []},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **kwargs: _codex_response({
        "report": "No comparable items were found.",
        "interesting_repositories": [],
        "learning_matches": [],
        "limitations": ["Available metadata contained no candidates."],
    }))

    result = skill.run({"limit": 50, "atlas_keywords": []})

    assert adapter.calls[0]["input"] == {"period": "weekly", "limit": 25}
    assert result["report"] == "No comparable items were found."
    assert result["interesting_repositories"] == []
    assert result["learning_matches"] == []
    assert any("requested limit of 50 was capped" in item for item in result["limitations"])
    assert any("No matching Atlas projects" in item for item in result["limitations"])
    assert any("No GitHub Trending repositories" in item for item in result["limitations"])


def test_invalid_codex_shape_falls_back_to_manifest_shaped_output(monkeypatch):
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=[{
            "rank": 1,
            "full_name": "example/repo",
            "description": None,
            "language": None,
            "html_url": "https://github.com/example/repo",
            "stars": 1,
            "forks": 0,
            "stars_gained": 1,
            "readme": None,
            "readme_truncated": False,
        }]),
        skill.ATLAS_OPERATION: {"projects": [{"title": "Project"}]},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **kwargs: {
        "response": "not json", "model": None, "internet_access": False
    })

    result = skill.run({})

    assert result == {
        "report": "The comparison data was collected, but Codex did not return valid structured analysis.",
        "interesting_repositories": [],
        "learning_matches": [],
        "limitations": ["The Codex response could not be parsed as the required JSON object."],
    }


def test_keyword_filter_is_bounded_to_selected_atlas_contract(monkeypatch):
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(),
        skill.ATLAS_OPERATION: {"projects": []},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **kwargs: _codex_response({
        "report": "No matches.",
        "interesting_repositories": [],
        "learning_matches": [],
        "limitations": [],
    }))

    result = skill.run({"atlas_keywords": ["a" * 150, "b" * 100]})

    assert adapter.calls[1]["input"]["keywords"] == "a" * 150
    assert any("keyword filtering was shortened" in item for item in result["limitations"])


@pytest.mark.parametrize("response", [
    {"report": "ok", "interesting_repositories": [{}], "learning_matches": [], "limitations": []},
    {"report": "ok", "interesting_repositories": [], "learning_matches": [{}], "limitations": []},
])
def test_parse_analysis_rejects_malformed_nested_items(response):
    with pytest.raises(ValueError):
        skill._parse_analysis(json.dumps(response))
