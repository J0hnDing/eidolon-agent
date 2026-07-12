"""Hidden weekly automation for GitHub Trending insights.

The module uses only the Python standard library, has no import-time side
effects, and communicates through one JSON object on stdin/stdout.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen


GITHUB_ORIGIN = "https://github.com"
USER_AGENT = "github-trending-insights/1.0"
SOURCE_TIMEOUT_SECONDS = 25
CODEX_TIMEOUT_SECONDS = 45
MAX_REPOSITORIES = 10


def _class_has(attrs: dict[str, str], value: str) -> bool:
    return value in attrs.get("class", "").split()


def _parse_count(value: str) -> Optional[int]:
    cleaned = value.strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([km]?)", cleaned)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
    return int(number * multiplier)


class TrendingParser(HTMLParser):
    """Tolerant parser for the repository article elements on Trending."""

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.projects: list[dict[str, Any]] = []
        self.entry_failures: list[dict[str, Any]] = []
        self._article_depth = 0
        self._article: Optional[dict[str, Any]] = None
        self._capture: Optional[str] = None
        self._capture_depth = 0
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, Optional[str]]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        if tag == "article" and _class_has(attrs, "Box-row") and self._article is None:
            self._article_depth = 1
            self._article = {
                "identifier": None,
                "url": None,
                "description": None,
                "primary_language": None,
                "stars": None,
                "forks": None,
                "recent_stars": None,
            }
            return
        if self._article is None:
            return
        self._article_depth += 1
        if self._capture is not None:
            self._capture_depth += 1
            return

        href = attrs.get("href", "")
        if tag == "a" and re.fullmatch(r"/[^/\s]+/[^/\s]+", href):
            self._article["identifier"] = href.strip("/")
            self._article["url"] = urljoin(GITHUB_ORIGIN, href)
        elif tag == "a" and href.endswith("/stargazers"):
            self._begin_capture("stars")
        elif tag == "a" and (href.endswith("/forks") or href.endswith("/network/members")):
            self._begin_capture("forks")
        elif tag == "p":
            self._begin_capture("description")
        elif tag == "span" and attrs.get("itemprop") == "programmingLanguage":
            self._begin_capture("primary_language")
        elif tag == "span" and "float-sm-right" in attrs.get("class", ""):
            self._begin_capture("recent_stars")

    def _begin_capture(self, field: str) -> None:
        self._capture = field
        self._capture_depth = 1
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._article is None:
            return
        if self._capture is not None:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                value = html.unescape(" ".join("".join(self._text).split()))
                if self._capture in {"stars", "forks", "recent_stars"}:
                    self._article[self._capture] = _parse_count(value)
                else:
                    self._article[self._capture] = value or None
                self._capture = None
                self._text = []
        self._article_depth -= 1
        if tag == "article" and self._article_depth == 0:
            self._finish_article()

    def _finish_article(self) -> None:
        article = self._article or {}
        self._article = None
        if len(self.projects) >= self.limit:
            return
        identifier = article.get("identifier")
        if not identifier:
            self.entry_failures.append(
                {"stage": "parsing", "entry_position": len(self.projects) + 1,
                 "message": "Repository identifier was unavailable; entry skipped."}
            )
            return
        if any(project["identifier"].lower() == identifier.lower() for project in self.projects):
            return
        article["rank"] = len(self.projects) + 1
        article["metadata_missing"] = [
            field for field in ("description", "primary_language", "stars", "forks", "recent_stars")
            if article.get(field) is None
        ]
        self.projects.append(article)


def _trending_url(language: str, since: str) -> str:
    path = "/trending"
    if language:
        path += "/" + quote(language.strip(), safe="")
    return f"{GITHUB_ORIGIN}{path}?since={quote(since, safe='')}"


def _retrieve(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("Source URL is outside the approved github.com domain.")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urlopen(request, timeout=SOURCE_TIMEOUT_SECONDS) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def _codex_prompt() -> str:
    return (
        "Analyze every repository in the supplied context. Return JSON only with this exact shape: "
        '{"analyses":[{"identifier":"owner/repo","rank":1,"purpose":"...",'
        '"notable_qualities":["..."],"likely_use_cases":["..."],'
        '"potential_limitations":["..."]}]}. '
        "Include exactly one concise, separately identifiable analysis per input repository. "
        "Copy identifier and rank exactly. Do not use internet access or invent missing metadata."
    )


def _extract_json(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            return json.loads(candidate[start:end + 1])
        raise


def _call_codex(projects: list[dict[str, Any]]) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    skill_id = os.environ.get("PERSONAL_AGENT_SKILL_ID", "").strip()
    backend_url = os.environ.get("PERSONAL_AGENT_BACKEND_URL", "").strip().rstrip("/")
    if not skill_id or not backend_url:
        return {}, [{"stage": "codex", "message": "Required backend runtime environment is unavailable."}]

    endpoint = f"{backend_url}/skills/{quote(skill_id, safe='')}/codex"
    payload = {
        "prompt": _codex_prompt(),
        "context": {"repositories": projects},
        "codex_permissions": {"call_response": True, "internet_access": False},
    }
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=CODEX_TIMEOUT_SECONDS) as response:
            envelope = json.loads(response.read().decode("utf-8"))
        parsed = _extract_json(envelope.get("response", ""))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, [{"stage": "codex", "message": f"Batched analysis request failed: {type(exc).__name__}."}]

    items = parsed.get("analyses") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return {}, [{"stage": "codex", "message": "Batched analysis response did not contain an analyses array."}]

    expected = {(p["identifier"], p["rank"]) for p in projects}
    mapped: dict[tuple[str, int], dict[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append({"stage": "codex", "analysis_position": index + 1, "message": "Malformed analysis was ignored."})
            continue
        key = (item.get("identifier"), item.get("rank"))
        required = ("purpose", "notable_qualities", "likely_use_cases", "potential_limitations")
        if key not in expected:
            failures.append({"stage": "codex", "analysis_position": index + 1, "message": "Unmatched analysis was ignored."})
        elif key in mapped:
            failures.append({"stage": "codex", "repository": key[0], "rank": key[1], "message": "Duplicate analysis was ignored."})
        elif not all(field in item for field in required):
            failures.append({"stage": "codex", "repository": key[0], "rank": key[1], "message": "Partial analysis was ignored."})
        else:
            mapped[key] = {field: item[field] for field in required}
    return mapped, failures


def run(input_data: Any) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    if not isinstance(input_data, dict):
        return {"status": "failed", "source": None, "projects": [], "failures": [
            {"stage": "input", "message": "Input must be a JSON object."}
        ]}
    language = input_data.get("language", "")
    since = input_data.get("since", "weekly")
    limit = input_data.get("limit", MAX_REPOSITORIES)
    if not isinstance(language, str) or since not in {"daily", "weekly", "monthly"} or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10:
        return {"status": "failed", "source": None, "projects": [], "failures": [
            {"stage": "input", "message": "Expected language string, since daily/weekly/monthly, and limit from 1 to 10."}
        ]}

    url = _trending_url(language, since)
    try:
        page = _retrieve(url)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        return {"status": "failed", "source": {"url": url, "since": since, "language": language or None},
                "projects": [], "failures": [{"stage": "retrieval", "message": f"GitHub Trending retrieval failed: {type(exc).__name__}."}]}

    parser = TrendingParser(limit)
    try:
        parser.feed(page)
        parser.close()
    except Exception as exc:  # HTMLParser extensions can fail on unusually malformed pages.
        failures.append({"stage": "parsing", "message": f"Page parsing was incomplete: {type(exc).__name__}."})
    failures.extend(parser.entry_failures)
    projects = parser.projects
    if not projects:
        failures.append({"stage": "parsing", "message": "No repository entries could be parsed."})
    else:
        analyses, codex_failures = _call_codex(projects)
        failures.extend(codex_failures)
        for project in projects:
            key = (project["identifier"], project["rank"])
            project["analysis"] = analyses.get(key)
            if key not in analyses:
                failures.append({"stage": "codex", "repository": key[0], "rank": key[1], "message": "Analysis is unavailable for this repository."})

    complete = bool(projects) and not failures
    status = "complete" if complete else ("partial" if projects else "failed")
    return {
        "status": status,
        "source": {"url": url, "since": since, "language": language or None},
        "projects": projects,
        "failures": failures,
    }


def main() -> None:
    try:
        raw = sys.stdin.read()
        input_data = json.loads(raw) if raw.strip() else {}
        output = run(input_data)
    except json.JSONDecodeError:
        output = {"status": "failed", "source": None, "projects": [], "failures": [
            {"stage": "input", "message": "stdin did not contain valid JSON."}
        ]}
    except Exception as exc:
        output = {"status": "failed", "source": None, "projects": [], "failures": [
            {"stage": "internal", "message": f"Unexpected failure: {type(exc).__name__}."}
        ]}
    sys.stdout.write(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
