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


def _repository(*, full_name="example/new-tool", rank=1, description="A useful new tool", stars=120):
    return {
        "rank": rank,
        "full_name": full_name,
        "description": description,
        "language": "Python",
        "html_url": f"https://github.com/{full_name}",
        "stars": stars,
        "forks": 15,
        "stars_gained": 40,
        "readme": "# New Tool\nA local project knowledge utility.",
        "readme_truncated": False,
    }


def _codex_response(repositories):
    return {
        "response": json.dumps({"repositories": repositories}),
        "model": "test-model",
        "internet_access": False,
    }


def test_run_discovers_before_personalizing_and_merges_only_selected_repositories(monkeypatch):
    github_repositories = [
        _repository(full_name=f"example/repo-{index}", rank=index, stars=index * 10)
        for index in range(1, 19)
    ]
    goals = {"goals": [{"title": "Learn systems"}], "progressions": []}
    projects = {"projects": [{"title": "Atlas Companion"}]}
    interests = {"hobbies": [{"title": "Programming"}], "preferences": []}
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=github_repositories),
        skill.ATLAS_GOAL_OPERATION: goals,
        skill.ATLAS_PROJECT_OPERATION: projects,
        skill.ATLAS_INTEREST_OPERATION: interests,
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    codex_calls = []

    def fake_codex(**kwargs):
        codex_calls.append(kwargs)
        return _codex_response([
            {"full_name": "example/repo-3", "analysis": "A useful systems-learning example."},
            {"full_name": "example/repo-17", "analysis": "A novel technical idea worth studying."},
        ])

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", fake_codex)

    result = skill.run({
        "limit": 25,
        "period": "weekly",
        "excluded_repo": ["example/repo-1", "example/repo-2"],
    })

    assert result == {
        "repositories": [
            {
                "name": "example/repo-3",
                "url": "https://github.com/example/repo-3",
                "stars": 30,
                "description": "A useful new tool",
                "analysis": "A useful systems-learning example.",
            },
            {
                "name": "example/repo-17",
                "url": "https://github.com/example/repo-17",
                "stars": 170,
                "description": "A useful new tool",
                "analysis": "A novel technical idea worth studying.",
            },
        ],
        "seen_repo": [f"example/repo-{index}" for index in range(3, 18)],
    }
    assert [call["operation"] for call in adapter.calls] == [
        skill.GITHUB_OPERATION,
        skill.ATLAS_GOAL_OPERATION,
        skill.ATLAS_PROJECT_OPERATION,
        skill.ATLAS_INTEREST_OPERATION,
    ]
    assert adapter.calls[0]["input"] == {"period": "weekly", "limit": 25}
    assert [item["full_name"] for item in codex_calls[0]["context"]["repositories"]] == [
        f"example/repo-{index}" for index in range(3, 18)
    ]
    assert codex_calls[0]["context"]["atlas_goals"] == goals["goals"]
    assert codex_calls[0]["context"]["atlas_projects"] == projects["projects"]
    assert codex_calls[0]["context"]["atlas_interests"] == interests
    assert codex_calls[0]["response_schema"] == skill.CODEX_RESPONSE_SCHEMA
    assert codex_calls[0]["internet_access"] is False


def test_run_returns_zero_without_personalization_when_all_candidates_are_excluded(monkeypatch):
    repository = _repository()
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=[repository]),
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: pytest.fail("Codex must not run without candidates"),
    )

    assert skill.run({"excluded_repo": [repository["full_name"]]}) == {
        "repositories": [],
        "seen_repo": [],
    }
    assert [call["operation"] for call in adapter.calls] == [skill.GITHUB_OPERATION]


def test_run_allows_zero_codex_recommendations(monkeypatch):
    repository = _repository()
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=[repository]),
        skill.ATLAS_GOAL_OPERATION: {"goals": [], "progressions": []},
        skill.ATLAS_PROJECT_OPERATION: {"projects": []},
        skill.ATLAS_INTEREST_OPERATION: {"hobbies": [], "preferences": []},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **_kwargs: _codex_response([]))

    assert skill.run({}) == {"repositories": [], "seen_repo": [repository["full_name"]]}


def test_limit_is_capped_at_github_bound(monkeypatch):
    repository = _repository()
    adapter = DeterministicFakeIntegrationAdapter({
        skill.GITHUB_OPERATION: _github_result(repositories=[repository]),
        skill.ATLAS_GOAL_OPERATION: {"goals": [], "progressions": []},
        skill.ATLAS_PROJECT_OPERATION: {"projects": []},
        skill.ATLAS_INTEREST_OPERATION: {"hobbies": [], "preferences": []},
    })
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", adapter.call)
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: _codex_response([{"full_name": repository["full_name"], "analysis": "Worth learning."}]),
    )

    skill.run({"limit": 50})

    assert adapter.calls[0]["input"] == {"period": "weekly", "limit": 25}


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"repositories": {}},
        {"repositories": [{"full_name": "unknown/repo", "analysis": "Invented."}]},
        {"repositories": [{"full_name": "example/new-tool", "analysis": ""}]},
        {"repositories": [{"full_name": "example/new-tool", "analysis": "One"}, {"full_name": "example/new-tool", "analysis": "Two"}]},
    ],
)
def test_parse_recommendations_rejects_malformed_or_unavailable_results(response):
    with pytest.raises(ValueError):
        skill._parse_recommendations(json.dumps(response), [_repository()])
