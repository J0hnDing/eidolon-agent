"""Create deterministic weekly GitHub and AI research Notion reports."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import function_runtime_capabilities
import integration_runtime_capabilities

SCOUT_FUNCTION = "github_repo_scout"
PAPER_SCOUT_FUNCTION = "research_paper_scout"
REPORT_CREATE_OPERATION = "notion.report.create"
NOTIFICATION_OPERATION = "telegram.notification.send"
REPORT_SELECT = "GitHub Projects"
RESEARCH_REPORT_SELECT = "AI Research"
REPORT_TIMEZONE = ZoneInfo("America/Toronto")
SCOUT_INPUT = {"limit": 25, "period": "weekly"}
SEEN_REPOSITORIES_FILENAME = "seen_repositories.json"
SEEN_PAPERS_FILENAME = "seen_papers.json"

REPOSITORY_FIELDS = {"name", "url", "stars", "description", "analysis"}
PAPER_FIELDS = {
    "rank",
    "paper_id",
    "title",
    "authors",
    "abstract",
    "url",
    "pdf_url",
    "published_at",
    "organization",
    "upvotes",
    "analysis",
    "content_source",
    "source_content_truncated",
    "analysis_content_truncated",
}
MAX_REPOSITORIES = 6
MAX_SEEN_REPOSITORIES = 15
MAX_SELECTED_PAPERS = 6
MAX_SEEN_PAPERS = 6
MAX_AUTHORS = 100
MAX_NOTION_TEXT = 2_000
MAX_NOTION_BLOCKS = 100
MAX_NOTION_RICH_TEXT_ITEMS = 100
MAX_NOTION_PAYLOAD_BYTES = 500_000
MAX_ABSTRACT = 50_000
MAX_ANALYSIS = 2_000
MAX_URL = 4_000
MAX_NOTIFICATION_DESCRIPTION = 800
SUCCESS_TITLE = "Your weekly reports are ready"
FAILURE_TITLE = "Weekly report failed"


def run(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Create both weekly reports and notify Telegram of the terminal outcome."""
    failure_code = "invalid_input"
    try:
        if payload:
            raise ValueError("Weekly Report service input must be an empty object")
        report_date = (
            (now or datetime.now(REPORT_TIMEZONE))
            .astimezone(REPORT_TIMEZONE)
            .date()
            .isoformat()
        )

        failure_code = "seen_repositories_load_failed"
        seen_repositories = _load_seen_values(
            SEEN_REPOSITORIES_FILENAME, label="repository", validator=_is_full_name
        )
        failure_code = "github_scout_failed"
        scout_output = function_runtime_capabilities.call_function(
            SCOUT_FUNCTION,
            {**SCOUT_INPUT, "excluded_repo": seen_repositories},
        )
        failure_code = "github_scout_invalid_output"
        repositories, newly_seen_repositories = _validated_scout_output(scout_output)
        report_name = f"Weekly GitHub Projects Report — {report_date}"
        failure_code = "notion_github_report_create_failed"
        report = _create_report(
            report_name, REPORT_SELECT, _report_blocks(repositories, report_date)
        )
        failure_code = "notion_github_report_invalid_response"
        _validate_created_report(report, report_name, REPORT_SELECT)
        failure_code = "seen_repositories_persist_failed"
        _persist_seen_values(
            SEEN_REPOSITORIES_FILENAME,
            seen_repositories,
            newly_seen_repositories,
            label="repository",
        )

        failure_code = "seen_papers_load_failed"
        seen_papers = _load_seen_values(
            SEEN_PAPERS_FILENAME,
            label="paper",
            validator=_is_paper_input_id,
        )
        failure_code = "paper_scout_failed"
        paper_output = function_runtime_capabilities.call_function(
            PAPER_SCOUT_FUNCTION,
            {"seen_papers": seen_papers},
        )
        failure_code = "paper_scout_invalid_output"
        selected_papers, newly_seen_papers = _validated_paper_scout_output(
            paper_output,
            excluded_papers=seen_papers,
        )
        research_report_name = f"Weekly AI Research Report — {report_date}"
        failure_code = "research_report_blocks_invalid"
        research_blocks = _research_report_blocks(selected_papers, report_date)
        failure_code = "notion_research_report_create_failed"
        research_report = _create_report(
            research_report_name,
            RESEARCH_REPORT_SELECT,
            research_blocks,
        )
        failure_code = "notion_research_report_invalid_response"
        _validate_created_report(
            research_report, research_report_name, RESEARCH_REPORT_SELECT
        )
        failure_code = "seen_papers_persist_failed"
        _persist_seen_values(
            SEEN_PAPERS_FILENAME,
            seen_papers,
            newly_seen_papers,
            label="paper",
        )

        failure_code = "telegram_notification_failed"
        _send_notification(
            title=SUCCESS_TITLE,
            description=f"• {report_name}\n• {research_report_name}",
        )
        return {
            "report": report,
            "repository_count": len(repositories),
            "research_report": research_report,
            "paper_count": len(selected_papers),
        }
    except Exception as exc:
        _try_send_failure_alert(exc, fallback_code=failure_code)
        raise


def _create_report(
    name: str, select: str, children: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(children) > MAX_NOTION_BLOCKS:
        raise ValueError("Weekly Report service produced too many Notion blocks")
    report_input = {"name": name, "select": select, "children": children}
    if len(json.dumps(report_input).encode("utf-8")) > MAX_NOTION_PAYLOAD_BYTES:
        raise ValueError(
            "Weekly Report service produced a Notion request larger than 500 KB"
        )
    return integration_runtime_capabilities.call(
        operation=REPORT_CREATE_OPERATION,
        input=report_input,
    )


def _send_notification(
    *, title: str, description: str, alert: bool = False
) -> dict[str, Any]:
    return integration_runtime_capabilities.call(
        operation=NOTIFICATION_OPERATION,
        input={"title": title, "description": description, "alert": alert},
    )


def _try_send_failure_alert(exc: Exception, *, fallback_code: str) -> None:
    error_type = getattr(exc, "error_type", None)
    code = (
        error_type
        if isinstance(error_type, str) and error_type.strip()
        else fallback_code
    )
    message = (
        " ".join(str(exc).split())
        or "The weekly report service failed without an error message."
    )
    try:
        _send_notification(
            title=FAILURE_TITLE,
            description=f"{code}: {message}"[:MAX_NOTIFICATION_DESCRIPTION],
            alert=True,
        )
    except Exception:
        pass


def _validated_scout_output(
    output: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(output, dict) or set(output) != {"repositories", "seen_repo"}:
        raise ValueError("GitHub Scout output has the wrong top-level shape")
    repositories = output["repositories"]
    if not isinstance(repositories, list) or len(repositories) > MAX_REPOSITORIES:
        raise ValueError("GitHub Scout returned an invalid repository list")
    seen_repo = _validated_unique_ids(
        output["seen_repo"],
        maximum=MAX_SEEN_REPOSITORIES,
        label="seen repository",
        validator=_is_full_name,
    )
    validated: list[dict[str, Any]] = []
    selected_names: set[str] = set()
    for repository in repositories:
        if not isinstance(repository, dict) or set(repository) != REPOSITORY_FIELDS:
            raise ValueError("GitHub Scout returned an invalid repository object")
        name = repository["name"]
        if not _is_full_name(name) or len(name) > MAX_NOTION_TEXT:
            raise ValueError("GitHub Scout returned an invalid repository name")
        if name.casefold() in selected_names:
            raise ValueError("GitHub Scout returned duplicate repositories")
        selected_names.add(name.casefold())
        if not _is_https_url(repository["url"], host="github.com"):
            raise ValueError("GitHub Scout returned an invalid repository URL")
        stars = repository["stars"]
        if not isinstance(stars, int) or isinstance(stars, bool) or stars < 0:
            raise ValueError("GitHub Scout returned an invalid star count")
        description = repository["description"]
        if description is not None and not _is_string(
            description, MAX_NOTION_TEXT, allow_empty=True
        ):
            raise ValueError("GitHub Scout returned an invalid repository description")
        if not _is_string(repository["analysis"], MAX_NOTION_TEXT):
            raise ValueError("GitHub Scout returned an invalid repository analysis")
        validated.append(repository)
    return validated, seen_repo


def _validated_paper_scout_output(
    output: dict[str, Any],
    *,
    excluded_papers: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(output, dict) or set(output) != {
        "selected_papers",
        "seen_papers",
    }:
        raise ValueError("Research Paper Scout output has the wrong top-level shape")
    newly_seen = _validated_unique_ids(
        output["seen_papers"],
        maximum=MAX_SEEN_PAPERS,
        label="seen paper",
        validator=_is_paper_id,
    )
    excluded_keys = {_paper_id_key(paper_id) for paper_id in excluded_papers}
    if any(_paper_id_key(paper_id) in excluded_keys for paper_id in newly_seen):
        raise ValueError(
            "Research Paper Scout returned an excluded paper as newly seen"
        )

    selected = output["selected_papers"]
    if not isinstance(selected, list) or len(selected) > MAX_SELECTED_PAPERS:
        raise ValueError("Research Paper Scout returned an invalid selected paper list")
    selected_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for expected_rank, paper in enumerate(selected, start=1):
        if not isinstance(paper, dict) or set(paper) != PAPER_FIELDS:
            raise ValueError("Research Paper Scout returned an invalid selected paper")
        paper_id = paper["paper_id"]
        paper_key = paper_id.casefold() if _is_paper_id(paper_id) else ""
        if not paper_key or paper_key in selected_ids:
            raise ValueError("Research Paper Scout selected a duplicate paper")
        selected_ids.add(paper_key)
        rank = paper["rank"]
        if not isinstance(rank, int) or isinstance(rank, bool) or rank != expected_rank:
            raise ValueError("Research Paper Scout returned non-contiguous paper ranks")
        if not _is_string(paper["title"], MAX_NOTION_TEXT):
            raise ValueError("Research Paper Scout returned an invalid paper title")
        authors = paper["authors"]
        if (
            not isinstance(authors, list)
            or len(authors) > MAX_AUTHORS
            or any(not _is_string(author, 500) for author in authors)
        ):
            raise ValueError("Research Paper Scout returned invalid paper authors")
        if not _is_string(paper["abstract"], MAX_ABSTRACT, allow_empty=True):
            raise ValueError("Research Paper Scout returned an invalid paper abstract")
        if not _is_https_url(paper["url"], maximum=MAX_URL) or not _is_https_url(
            paper["pdf_url"], maximum=MAX_URL
        ):
            raise ValueError("Research Paper Scout returned an invalid paper URL")
        published_at = paper["published_at"]
        if published_at is not None and not _is_string(published_at, 64):
            raise ValueError(
                "Research Paper Scout returned an invalid publication date"
            )
        organization = paper["organization"]
        if organization is not None and not _is_string(organization, 500):
            raise ValueError("Research Paper Scout returned an invalid organization")
        upvotes = paper["upvotes"]
        if not isinstance(upvotes, int) or isinstance(upvotes, bool) or upvotes < 0:
            raise ValueError("Research Paper Scout returned invalid upvotes")
        if not _is_string(paper["analysis"], MAX_ANALYSIS):
            raise ValueError("Research Paper Scout returned an invalid paper analysis")
        if paper["content_source"] not in {"arxiv_html", "arxiv_pdf"}:
            raise ValueError("Research Paper Scout returned an invalid content source")
        if not isinstance(paper["source_content_truncated"], bool) or not isinstance(
            paper["analysis_content_truncated"], bool
        ):
            raise ValueError(
                "Research Paper Scout returned invalid truncation metadata"
            )
        validated.append(paper)

    if not validated:
        if newly_seen:
            raise ValueError(
                "Research Paper Scout returned seen papers without selections"
            )
        return validated, newly_seen
    selected_keys = {_paper_id_key(paper["paper_id"]) for paper in validated}
    returned_seen_keys = {_paper_id_key(paper_id) for paper_id in newly_seen}
    if returned_seen_keys != selected_keys:
        raise ValueError(
            "Research Paper Scout seen_papers must contain exactly the selected paper IDs"
        )
    return validated, newly_seen


def _cache_path(filename: str) -> Path:
    configured = os.environ.get("PERSONAL_AGENT_SKILL_CACHE_DIR")
    return (Path(configured) if configured else Path("cache")) / filename


def _load_seen_values(
    filename: str, *, label: str, validator: Callable[[Any], bool]
) -> list[str]:
    path = _cache_path(filename)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Weekly Report service cache contains invalid seen {label} history"
        ) from exc
    if not isinstance(value, list) or any(not validator(item) for item in value):
        raise ValueError(
            f"Weekly Report service cache contains invalid seen {label} history"
        )
    return _deduplicate(value)


def _persist_seen_values(
    filename: str,
    previous: list[str],
    newly_seen: list[str],
    *,
    label: str,
) -> None:
    values = _deduplicate([*previous, *newly_seen])
    if values == previous:
        return
    path = _cache_path(filename)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        temporary_path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        temporary_path.replace(path)
    except OSError as exc:
        raise ValueError(
            f"Weekly Report service could not persist seen {label} history"
        ) from exc


def _validated_unique_ids(
    value: Any,
    *,
    maximum: int,
    label: str,
    validator: Callable[[Any], bool],
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or any(not validator(item) for item in value)
    ):
        raise ValueError(f"Scout returned an invalid {label} list")
    if len(_deduplicate(value)) != len(value):
        raise ValueError(f"Scout returned duplicate {label} ids")
    return value


def _deduplicate(values: list[str]) -> list[str]:
    result: list[str] = []
    keys: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in keys:
            keys.add(key)
            result.append(value)
    return result


def _is_full_name(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    owner, separator, repository = value.partition("/")
    return bool(
        separator
        and owner
        and repository
        and value.count("/") == 1
        and not any(char.isspace() for char in value)
    )


def _is_paper_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}", value) is not None
    )


def _is_paper_input_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}(?:v[1-9][0-9]*)?", value) is not None
    )


def _paper_id_key(value: str) -> str:
    return re.sub(r"v[1-9][0-9]*$", "", value).casefold()


def _is_string(value: Any, maximum: int, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and (allow_empty or bool(value.strip()))
    )


def _is_https_url(
    value: Any,
    *,
    host: str | None = None,
    maximum: int = MAX_NOTION_TEXT,
) -> bool:
    if not isinstance(value, str) or len(value) > maximum:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and (host is None or parsed.netloc == host)
    )


def _report_blocks(
    repositories: list[dict[str, Any]], report_date: str
) -> list[dict[str, Any]]:
    blocks = [
        _heading("heading_1", "GitHub Projects Weekly Report"),
        {
            "type": "callout",
            "callout": {
                "rich_text": [
                    _text(
                        f"Generated deterministically on {report_date} from GitHub Scout output."
                    )
                ],
                "icon": {"type": "emoji", "emoji": "📊"},
                "color": "blue_background",
            },
        },
        {"type": "divider", "divider": {}},
    ]
    if not repositories:
        blocks.append(
            _paragraph(
                [_text("GitHub Scout returned no repositories for this weekly period.")]
            )
        )
        return blocks
    for repository in repositories:
        blocks.extend(
            [
                _heading("heading_2", repository["name"], link=repository["url"]),
                _paragraph(
                    [
                        _text(f"★ {repository['stars']:,} stars", bold=True),
                        _text(" · "),
                        _text(
                            repository["description"]
                            or "No GitHub description provided."
                        ),
                    ]
                ),
                _paragraph([_text(repository["analysis"])]),
                _bookmark(repository["url"]),
                {"type": "divider", "divider": {}},
            ]
        )
    return blocks


def _research_report_blocks(
    selected_papers: list[dict[str, Any]],
    report_date: str,
) -> list[dict[str, Any]]:
    blocks = [
        _heading("heading_1", "AI Research Weekly Report"),
        {
            "type": "callout",
            "callout": {
                "rich_text": [
                    _text(
                        f"Generated deterministically on {report_date} from Research Paper Scout output."
                    )
                ],
                "icon": {"type": "emoji", "emoji": "🔬"},
                "color": "purple_background",
            },
        },
        {"type": "divider", "divider": {}},
    ]
    if not selected_papers:
        blocks.append(
            _paragraph(
                [
                    _text(
                        "Research Paper Scout found no papers valuable enough to recommend this week."
                    )
                ]
            )
        )
        return blocks

    blocks.append(_heading("heading_2", "Ranked papers"))
    for paper in selected_papers:
        blocks.append(_heading("heading_2", paper["title"], link=paper["url"]))
        metadata = []
        if paper["organization"] is not None:
            metadata.extend(
                [_text(f"Organization: {paper['organization']}"), _text(" · ")]
            )
        metadata.append(_text(f"Upvotes: {paper['upvotes']:,}", bold=True))
        blocks.append(_paragraph(metadata))
        blocks.append(_heading("heading_3", "Analysis"))
        blocks.extend(_paragraph_blocks(paper["analysis"]))
        blocks.append({"type": "divider", "divider": {}})

    if len(blocks) > MAX_NOTION_BLOCKS:
        raise ValueError("Research report exceeds Notion's 100-block create limit")
    return blocks


def _paragraph_blocks(value: str) -> list[dict[str, Any]]:
    return [_paragraph([_text(chunk) for chunk in _report_chunks(value)])]


def _report_chunks(value: str) -> list[str]:
    return [
        value[index : index + MAX_NOTION_TEXT]
        for index in range(0, len(value), MAX_NOTION_TEXT)
    ] or [""]


def _heading(block_type: str, value: str, *, link: str | None = None) -> dict[str, Any]:
    if len(value) > MAX_NOTION_TEXT:
        value = value[: MAX_NOTION_TEXT - 1] + "…"
    return {
        "type": block_type,
        block_type: {
            "rich_text": [_text(value, link=link)],
            "color": "default",
            "is_toggleable": False,
        },
    }


def _paragraph(rich_text: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rich_text) > MAX_NOTION_RICH_TEXT_ITEMS:
        raise ValueError(
            "Weekly Report service produced too many rich text items in one Notion block"
        )
    return {
        "type": "paragraph",
        "paragraph": {"rich_text": rich_text, "color": "default"},
    }


def _bookmark(url: str) -> dict[str, Any]:
    if not _is_https_url(url, maximum=MAX_NOTION_TEXT):
        raise ValueError(
            "Weekly Report service produced an invalid Notion bookmark URL"
        )
    return {"type": "bookmark", "bookmark": {"url": url}}


def _text(
    value: str,
    *,
    bold: bool = False,
    italic: bool = False,
    link: str | None = None,
) -> dict[str, Any]:
    if len(value) > MAX_NOTION_TEXT:
        raise ValueError("Weekly Report service produced overlong Notion rich text")
    if link is not None and not _is_https_url(link, maximum=MAX_NOTION_TEXT):
        raise ValueError(
            "Weekly Report service produced an invalid Notion rich text URL"
        )
    return {
        "type": "text",
        "text": {"content": value, "link": {"url": link} if link else None},
        "annotations": {
            "bold": bold,
            "italic": italic,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": "default",
        },
    }


def _validate_created_report(
    report: dict[str, Any], expected_name: str, expected_select: str
) -> None:
    if not isinstance(report, dict) or set(report) != {
        "id",
        "name",
        "created_time",
        "select",
    }:
        raise ValueError("Notion report create returned the wrong shape")
    if not _is_string(report["id"], 128):
        raise ValueError("Notion report create returned an invalid id")
    if report["name"] != expected_name or report["select"] != expected_select:
        raise ValueError("Notion report create returned mismatched metadata")
    if not _is_string(report["created_time"], 64):
        raise ValueError("Notion report create returned an invalid creation time")


def main() -> None:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("Weekly Report service input must be a JSON object")
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
