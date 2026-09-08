"""Create a deterministic weekly Notion report from GitHub Scout output."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import function_runtime_capabilities
import integration_runtime_capabilities

SCOUT_FUNCTION = "github_repo_scout"
REPORT_CREATE_OPERATION = "notion.report.create"
REPORT_SELECT = "GitHub Projects"
REPORT_TIMEZONE = ZoneInfo("America/Toronto")
SCOUT_INPUT = {"limit": 10, "period": "weekly", "atlas_keywords": []}
REPOSITORY_FIELDS = {"name", "url", "stars", "description", "analysis"}
MAX_REPOSITORIES = 10
MAX_NOTION_TEXT = 2_000


def run(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Call Scout once, validate its contract, and create one native Notion report."""
    if payload:
        raise ValueError("Weekly Report service input must be an empty object")
    scout_output = function_runtime_capabilities.call_function(
        SCOUT_FUNCTION,
        dict(SCOUT_INPUT),
    )
    repositories = _validated_repositories(scout_output)
    report_date = (now or datetime.now(REPORT_TIMEZONE)).astimezone(REPORT_TIMEZONE).date().isoformat()
    report_name = f"Weekly GitHub Projects Report — {report_date}"
    report = integration_runtime_capabilities.call(
        operation=REPORT_CREATE_OPERATION,
        input={
            "name": report_name,
            "select": REPORT_SELECT,
            "children": _report_blocks(repositories, report_date),
        },
    )
    _validate_created_report(report, report_name)
    return {"report": report, "repository_count": len(repositories)}


def _validated_repositories(output: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(output, dict) or set(output) != {"repositories"}:
        raise ValueError("GitHub Scout output has the wrong top-level shape")
    repositories = output["repositories"]
    if not isinstance(repositories, list) or len(repositories) > MAX_REPOSITORIES:
        raise ValueError("GitHub Scout returned an invalid repository list")
    validated: list[dict[str, Any]] = []
    for repository in repositories:
        if not isinstance(repository, dict) or set(repository) != REPOSITORY_FIELDS:
            raise ValueError("GitHub Scout returned an invalid repository object")
        name = repository["name"]
        url = repository["url"]
        stars = repository["stars"]
        description = repository["description"]
        analysis = repository["analysis"]
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NOTION_TEXT:
            raise ValueError("GitHub Scout returned an invalid repository name")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            raise ValueError("GitHub Scout returned an invalid repository URL")
        if not isinstance(stars, int) or isinstance(stars, bool) or stars < 0:
            raise ValueError("GitHub Scout returned an invalid star count")
        if description is not None and (
            not isinstance(description, str) or len(description) > MAX_NOTION_TEXT
        ):
            raise ValueError("GitHub Scout returned an invalid repository description")
        if not isinstance(analysis, str) or not analysis.strip() or len(analysis) > MAX_NOTION_TEXT:
            raise ValueError("GitHub Scout returned an invalid repository analysis")
        validated.append(repository)
    return validated


def _report_blocks(repositories: list[dict[str, Any]], report_date: str) -> list[dict[str, Any]]:
    blocks = [
        _heading("heading_1", "GitHub Projects Weekly Report"),
        {
            "type": "callout",
            "callout": {
                "rich_text": [_text(f"Generated deterministically on {report_date} from GitHub Scout output.")],
                "icon": {"type": "emoji", "emoji": "📊"},
                "color": "blue_background",
            },
        },
        {"type": "divider", "divider": {}},
    ]
    if not repositories:
        blocks.append(_paragraph([_text("GitHub Scout returned no repositories for this weekly period.")]))
        return blocks
    for repository in repositories:
        blocks.extend(
            [
                _heading("heading_2", repository["name"], link=repository["url"]),
                _paragraph(
                    [
                        _text(f"★ {repository['stars']:,} stars", bold=True),
                        _text(" · "),
                        _text(repository["description"] or "No GitHub description provided."),
                    ]
                ),
                _paragraph([_text(repository["analysis"])]),
                {"type": "bookmark", "bookmark": {"url": repository["url"]}},
                {"type": "divider", "divider": {}},
            ]
        )
    return blocks


def _heading(block_type: str, value: str, *, link: str | None = None) -> dict[str, Any]:
    return {
        "type": block_type,
        block_type: {"rich_text": [_text(value, link=link)], "color": "default", "is_toggleable": False},
    }


def _paragraph(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "paragraph", "paragraph": {"rich_text": rich_text, "color": "default"}}


def _text(value: str, *, bold: bool = False, link: str | None = None) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": value, "link": {"url": link} if link else None},
        "annotations": {
            "bold": bold,
            "italic": False,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": "default",
        },
    }


def _validate_created_report(report: dict[str, Any], expected_name: str) -> None:
    if not isinstance(report, dict) or set(report) != {"id", "name", "created_time", "select"}:
        raise ValueError("Notion report create returned the wrong shape")
    if not isinstance(report["id"], str) or not report["id"]:
        raise ValueError("Notion report create returned an invalid id")
    if report["name"] != expected_name or report["select"] != REPORT_SELECT:
        raise ValueError("Notion report create returned mismatched metadata")
    if not isinstance(report["created_time"], str) or not report["created_time"]:
        raise ValueError("Notion report create returned an invalid creation time")


def main() -> None:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Weekly Report service input must be a JSON object")
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
