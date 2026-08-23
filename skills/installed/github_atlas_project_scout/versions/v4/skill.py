"""Analyze GitHub Trending repositories against local Atlas projects."""

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
ANALYSIS_FALLBACK = "Analysis unavailable because Codex did not return the required structured response."


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Return GitHub facts enriched with one Codex analysis per repository."""
    limit = payload.get("limit", 10)
    language = payload.get("language")
    period = payload.get("period", "weekly")
    atlas_keywords = payload.get("atlas_keywords", [])
    analysis_focus = payload.get("analysis_focus")

    github_input: dict[str, Any] = {
        "period": period,
        "limit": min(limit, MAX_GITHUB_LIMIT),
    }
    if language is not None:
        github_input["language"] = language
    github_result = integration_runtime_capabilities.call(
        operation=GITHUB_OPERATION,
        input=github_input,
    )
    repositories = github_result.get("repositories", [])
    if not repositories:
        return {"repositories": []}

    atlas_input: dict[str, Any] = {"limit": 100}
    keyword_query = _bounded_keywords(atlas_keywords)
    if keyword_query:
        atlas_input["keywords"] = keyword_query
    atlas_result = integration_runtime_capabilities.call(
        operation=ATLAS_OPERATION,
        input=atlas_input,
    )

    codex_result = function_runtime_capabilities.call_codex(
        prompt=_analysis_prompt(),
        context={
            "repositories": repositories,
            "atlas_projects": atlas_result.get("projects", []),
            "analysis_focus": analysis_focus,
        },
        internet_access=False,
    )
    try:
        analyses = _parse_analyses(codex_result["response"], repositories)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        analyses = {repository["full_name"]: ANALYSIS_FALLBACK for repository in repositories}

    return {
        "repositories": [
            {
                "name": repository["full_name"],
                "url": repository["html_url"],
                "stars": repository["stars"],
                "description": repository.get("description"),
                "analysis": analyses[repository["full_name"]],
            }
            for repository in repositories
        ]
    }


def _bounded_keywords(keywords: list[str]) -> str:
    selected: list[str] = []
    for keyword in keywords:
        candidate = " ".join([*selected, keyword])
        if len(candidate) > MAX_ATLAS_KEYWORDS_LENGTH:
            break
        selected.append(keyword)
    return " ".join(selected)


def _analysis_prompt() -> str:
    return (
        "Analyze every supplied GitHub repository using only the supplied repository and Atlas project data. "
        "Return only valid JSON with exactly this shape: {\"analyses\": {\"owner/repository\": "
        "\"analysis\"}}. The analyses object must contain every supplied repository full_name exactly once "
        "and no other keys. Each value must be one concise, non-empty analysis string of 2 to 4 sentences. "
        "Explain the project's high-level principle, mention anything genuinely interesting, and state any "
        "concrete relevance, lesson, or possible use for the supplied Atlas projects. If there is no meaningful "
        "relevance or notable insight, say so briefly instead of forcing one. Do not repeat basic metadata, add "
        "nested fields, use Markdown, or invent facts."
    )


def _parse_analyses(raw_response: str, repositories: list[dict[str, Any]]) -> dict[str, str]:
    parsed = json.loads(raw_response)
    if not isinstance(parsed, dict) or set(parsed) != {"analyses"}:
        raise ValueError("Codex response has the wrong top-level shape")
    analyses = parsed["analyses"]
    if not isinstance(analyses, dict):
        raise ValueError("analyses must be an object")

    expected_names = [repository["full_name"] for repository in repositories]
    if len(expected_names) != len(set(expected_names)) or set(analyses) != set(expected_names):
        raise ValueError("analyses must match the supplied repositories exactly")

    normalized: dict[str, str] = {}
    for name in expected_names:
        analysis = analyses[name]
        if not isinstance(analysis, str) or not analysis.strip():
            raise ValueError("every analysis must be a non-empty string")
        normalized[name] = analysis.strip()
    return normalized


def main() -> None:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Skill input must be a JSON object")
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
