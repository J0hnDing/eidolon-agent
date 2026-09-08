"""Select useful GitHub Trending repositories with optional Atlas context."""

from __future__ import annotations

import json
import sys
from typing import Any

import function_runtime_capabilities
import integration_runtime_capabilities

GITHUB_OPERATION = "github.repository.trending.list"
ATLAS_GOAL_OPERATION = "atlas.goal.list"
ATLAS_PROJECT_OPERATION = "atlas.project.list"
ATLAS_INTEREST_OPERATION = "atlas.interest.list"
MAX_GITHUB_LIMIT = 25
MAX_CANDIDATES = 15
MAX_RECOMMENDATIONS = 6
MAX_ANALYSIS_LENGTH = 2_000

CODEX_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repositories": {
            "type": "array",
            "maxItems": MAX_RECOMMENDATIONS,
            "items": {
                "type": "object",
                "properties": {
                    "full_name": {"type": "string", "minLength": 1},
                    "analysis": {"type": "string", "minLength": 1, "maxLength": MAX_ANALYSIS_LENGTH},
                },
                "required": ["full_name", "analysis"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["repositories"],
    "additionalProperties": False,
}


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Return selected repositories with authoritative GitHub metadata."""
    limit = payload.get("limit", MAX_GITHUB_LIMIT)
    language = payload.get("language")
    period = payload.get("period", "weekly")
    excluded_repositories = _excluded_repositories(payload.get("excluded_repo", []))

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
    candidates = _candidate_repositories(github_result.get("repositories", []), excluded_repositories)
    if not candidates:
        return {"repositories": [], "seen_repo": []}

    goals_result = integration_runtime_capabilities.call(
        operation=ATLAS_GOAL_OPERATION,
        input={"limit": 100},
    )
    projects_result = integration_runtime_capabilities.call(
        operation=ATLAS_PROJECT_OPERATION,
        input={"limit": 100},
    )
    interests_result = integration_runtime_capabilities.call(
        operation=ATLAS_INTEREST_OPERATION,
        input={},
    )

    codex_result = function_runtime_capabilities.call_codex(
        prompt=_analysis_prompt(),
        context={
            "repositories": candidates,
            "atlas_goals": goals_result.get("goals", []),
            "atlas_projects": projects_result.get("projects", []),
            "atlas_interests": interests_result,
        },
        internet_access=False,
        response_schema=CODEX_RESPONSE_SCHEMA,
    )
    selections = _parse_recommendations(codex_result["response"], candidates)
    by_name = {repository["full_name"]: repository for repository in candidates}
    return {
        "repositories": [
            {
                "name": selection["full_name"],
                "url": by_name[selection["full_name"]]["html_url"],
                "stars": by_name[selection["full_name"]]["stars"],
                "description": by_name[selection["full_name"]].get("description"),
                "analysis": selection["analysis"],
            }
            for selection in selections
        ],
        "seen_repo": [repository["full_name"] for repository in candidates],
    }


def _excluded_repositories(value: object) -> set[str]:
    if value is None:
        return set()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list):
        raise ValueError("excluded_repo must be an array of repository full names")
    excluded: set[str] = set()
    for name in values:
        if not isinstance(name, str) or not _is_full_name(name):
            raise ValueError("excluded_repo must contain repository full names")
        excluded.add(name.casefold())
    return excluded


def _candidate_repositories(repositories: object, excluded: set[str]) -> list[dict[str, Any]]:
    if not isinstance(repositories, list):
        raise ValueError("GitHub Trending returned an invalid repository list")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for repository in repositories:
        if not isinstance(repository, dict):
            raise ValueError("GitHub Trending returned an invalid repository")
        full_name = repository.get("full_name")
        if not isinstance(full_name, str) or not _is_full_name(full_name):
            raise ValueError("GitHub Trending returned an invalid repository full name")
        normalized_name = full_name.casefold()
        if normalized_name in excluded or normalized_name in seen:
            continue
        seen.add(normalized_name)
        candidates.append(repository)
        if len(candidates) == MAX_CANDIDATES:
            break
    return candidates


def _is_full_name(value: str) -> bool:
    owner, separator, repository = value.partition("/")
    return bool(separator and owner and repository and value.count("/") == 1 and not any(char.isspace() for char in value))


def _analysis_prompt() -> str:
    return (
        "Select 0-6 repositories that are genuinely likely to be useful, educational, or interesting to this user, "
        "considering the context provided to you. Context is not a matching target: a repository can be chosen "
        "without a connection to any project, interest, or goal.\n\n"
        "Recommend a repository when there is strong evidence of at least one of:\n"
        "- practical usefulness to the user;\n"
        "- meaningful alignment with the user's projects, interests, or goals;\n"
        "- substantial learning value;\n"
        "- a genuinely novel or technically interesting idea;\n"
        "- unusually popular.\n\n"
        "Do not manufacture arbitrary connections to the user's projects. "
        "Omit weak candidates; returning zero repositories is valid. Avoid selecting several repositories that "
        "provide essentially the same value. For each selected repository, write one analysis paragraph explaining "
        "what this repo is and how it works on a high-level, using relatively simple language and metaphors if appropriate, and how is it useful/interesting to the user. Return only valid JSON with exactly this shape: "
        '{"repositories":[{"full_name":"owner/repository","analysis":"concise analysis"}]}. '
        "Use only supplied repository full names and do not repeat GitHub metadata in the analysis."
    )


def _parse_recommendations(raw_response: str, candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    parsed = json.loads(raw_response)
    if not isinstance(parsed, dict) or set(parsed) != {"repositories"}:
        raise ValueError("Codex response has the wrong top-level shape")
    repositories = parsed["repositories"]
    if not isinstance(repositories, list) or len(repositories) > MAX_RECOMMENDATIONS:
        raise ValueError("Codex returned an invalid repository selection")

    candidate_names = {repository["full_name"] for repository in candidates}
    selected_names: set[str] = set()
    selections: list[dict[str, str]] = []
    for repository in repositories:
        if not isinstance(repository, dict) or set(repository) != {"full_name", "analysis"}:
            raise ValueError("Codex returned an invalid repository selection")
        full_name = repository["full_name"]
        analysis = repository["analysis"]
        if (
            not isinstance(full_name, str)
            or full_name not in candidate_names
            or full_name in selected_names
            or not isinstance(analysis, str)
            or not analysis.strip()
            or len(analysis) > MAX_ANALYSIS_LENGTH
        ):
            raise ValueError("Codex returned an invalid repository selection")
        selected_names.add(full_name)
        selections.append({"full_name": full_name, "analysis": analysis.strip()})
    return selections


def main() -> None:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Skill input must be a JSON object")
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
