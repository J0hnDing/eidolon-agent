"""Compare recent GitHub repositories with local Atlas projects."""

from __future__ import annotations

import json
import sys
from typing import Any

import function_runtime_capabilities
import integration_runtime_capabilities

GITHUB_OPERATION = "github.repository.trending.list"
ATLAS_OPERATION = "atlas.project.list"
MAX_GITHUB_LIMIT = 25
MAX_ATLAS_KEYWORDS_LENGTH = 200


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the bounded comparison workflow and return manifest-shaped output."""
    limit = payload.get("limit", 10)
    language = payload.get("language")
    atlas_keywords = payload.get("atlas_keywords", [])
    analysis_focus = payload.get("analysis_focus")

    github_limit = min(limit, MAX_GITHUB_LIMIT)
    limitations: list[str] = []
    if limit > MAX_GITHUB_LIMIT:
        limitations.append(
            f"The selected GitHub operation returns at most {MAX_GITHUB_LIMIT} repositories; "
            f"the requested limit of {limit} was capped."
        )

    github_input: dict[str, Any] = {"lookback_days": 30, "limit": github_limit}
    if language is not None:
        github_input["language"] = language
    github_result = integration_runtime_capabilities.call(
        operation="github.repository.trending.list",
        input=github_input,
    )

    atlas_input: dict[str, Any] = {"limit": 100}
    keyword_query, keywords_truncated = _bounded_keywords(atlas_keywords)
    if keyword_query:
        atlas_input["keywords"] = keyword_query
    if keywords_truncated:
        limitations.append(
            "Atlas keyword filtering was shortened to the selected operation's 200-character limit."
        )
    atlas_result = integration_runtime_capabilities.call(
        operation="atlas.project.list",
        input=atlas_input,
    )

    repositories = github_result.get("repositories", [])
    projects = atlas_result.get("projects", [])
    context = {
        "ranking": github_result.get("ranking"),
        "lookback_days": github_result.get("lookback_days"),
        "language": github_result.get("language"),
        "github_results_truncated": github_result.get("truncated", False),
        "repositories": repositories,
        "atlas_projects": projects,
        "analysis_focus": analysis_focus,
    }
    codex_result = function_runtime_capabilities.call_codex(
        prompt=_analysis_prompt(),
        context=context,
        internet_access=False,
    )

    try:
        analysis = _parse_analysis(codex_result["response"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        analysis = {
            "report": "The comparison data was collected, but Codex did not return valid structured analysis.",
            "interesting_repositories": [],
            "learning_matches": [],
            "limitations": ["The Codex response could not be parsed as the required JSON object."],
        }

    if not projects:
        limitations.append("No matching Atlas projects were available for project-specific comparison.")
    if not repositories:
        limitations.append("No suitable recent GitHub repositories were returned by the selected ranking.")
    if github_result.get("truncated"):
        limitations.append("The GitHub integration reported that its ranked result was truncated.")
    analysis["limitations"] = _unique_strings([*analysis["limitations"], *limitations])
    return analysis


def _bounded_keywords(keywords: list[str]) -> tuple[str, bool]:
    selected: list[str] = []
    for keyword in keywords:
        candidate = " ".join([*selected, keyword])
        if len(candidate) > MAX_ATLAS_KEYWORDS_LENGTH:
            return " ".join(selected), True
        selected.append(keyword)
    return " ".join(selected), False


def _analysis_prompt() -> str:
    return (
        "Compare the supplied ranked GitHub repositories with the supplied Atlas projects. "
        "Use only the supplied metadata and analysis focus. Return only one JSON object with exactly "
        "these keys: report (concise string), interesting_repositories (array of objects with full_name, "
        "url, summary, why_interesting), learning_matches (array of objects with atlas_project, "
        "github_repository, relevance, realistic_lessons as an array of strings), and limitations "
        "(array of strings). Separate generally interesting repositories from realistic project matches. "
        "Do not invent repository or project facts, and disclose insufficient metadata."
    )


def _parse_analysis(raw_response: str) -> dict[str, Any]:
    parsed = json.loads(raw_response)
    if not isinstance(parsed, dict) or set(parsed) != {
        "report",
        "interesting_repositories",
        "learning_matches",
        "limitations",
    }:
        raise ValueError("Codex response has the wrong top-level shape")
    if not isinstance(parsed["report"], str):
        raise ValueError("report must be a string")
    if not all(isinstance(parsed[key], list) for key in (
        "interesting_repositories",
        "learning_matches",
        "limitations",
    )):
        raise ValueError("analysis collections must be arrays")
    _validate_interesting(parsed["interesting_repositories"])
    _validate_matches(parsed["learning_matches"])
    if not all(isinstance(item, str) for item in parsed["limitations"]):
        raise ValueError("limitations must contain strings")
    return parsed


def _validate_interesting(items: list[Any]) -> None:
    required = {"full_name", "url", "summary", "why_interesting"}
    for item in items:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("interesting repository has the wrong shape")
        if not all(isinstance(item[key], str) for key in required):
            raise ValueError("interesting repository fields must be strings")


def _validate_matches(items: list[Any]) -> None:
    required = {"atlas_project", "github_repository", "relevance", "realistic_lessons"}
    for item in items:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("learning match has the wrong shape")
        if not all(isinstance(item[key], str) for key in required - {"realistic_lessons"}):
            raise ValueError("learning match text fields must be strings")
        lessons = item["realistic_lessons"]
        if not isinstance(lessons, list) or not all(isinstance(lesson, str) for lesson in lessons):
            raise ValueError("realistic_lessons must be an array of strings")


def _unique_strings(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def main() -> None:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Skill input must be a JSON object")
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
