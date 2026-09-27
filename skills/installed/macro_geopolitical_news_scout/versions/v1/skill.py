"""Bounded, selective macro and geopolitical release scout."""

from __future__ import annotations

import json
import re
import ssl
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

import function_runtime_capabilities
import integration_runtime_capabilities

MAX_CANDIDATES = 100
MAX_SEEN = 5000
MAX_ITEMS = 8
MAX_FEED_BYTES = 1_000_000
MAX_SUMMARY = 1800
FEEDS = (
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("BLS", "https://www.bls.gov/feed/empsit.rss"),
    ("BLS", "https://www.bls.gov/feed/cpi.rss"),
    ("BLS", "https://www.bls.gov/feed/jolts.rss"),
    ("BEA", "https://apps.bea.gov/rss/rss.xml"),
    ("Statistics Canada", "https://www150.statcan.gc.ca/n1/rss/dai-quo/36-eng.atom"),
    ("Statistics Canada", "https://www150.statcan.gc.ca/n1/rss/dai-quo/14-eng.atom"),
    ("Statistics Canada", "https://www150.statcan.gc.ca/n1/rss/dai-quo/18-eng.atom"),
    ("Statistics Canada", "https://www150.statcan.gc.ca/n1/rss/dai-quo/12-eng.atom"),
    ("Bank of Canada", "https://www.bankofcanada.ca/content_type/press-releases/feed/"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
    ("ECB", "https://www.ecb.europa.eu/rss/statpress.html"),
    ("EIA", "https://www.eia.gov/rss/press_rss.xml"),
    ("EIA", "https://www.eia.gov/rss/todayinenergy.xml"),
)
KEYED_OPERATIONS = (
    "fred.release.list",
    "bls.series.latest",
    "bea.series.latest",
    "eia.series.latest",
)
ATLAS_OPERATIONS = ("atlas.goal.list", "atlas.project.list", "atlas.interest.list")

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "macro_picture": {"type": "string", "maxLength": 700},
        "items": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": {
                "type": "object",
                "properties": {
                    "what_happened": {"type": "string", "minLength": 1, "maxLength": 450},
                    "why_it_matters": {"type": "string", "minLength": 1, "maxLength": 500},
                    "new_since_previous": {"type": "string", "minLength": 1, "maxLength": 350},
                    "watch_next": {"type": "string", "minLength": 1, "maxLength": 350},
                    "sources": {
                        "type": "array", "minItems": 1, "maxItems": 4,
                        "items": {"type": "string", "pattern": "^https://", "maxLength": 1800},
                    },
                },
                "required": ["what_happened", "why_it_matters", "new_since_previous", "watch_next", "sources"],
                "additionalProperties": False,
            },
        },
        "upcoming_catalysts": {
            "type": "array", "maxItems": 4,
            "items": {"type": "string", "minLength": 1, "maxLength": 250},
        },
    },
    "required": ["macro_picture", "items", "upcoming_catalysts"],
    "additionalProperties": False,
}


def run(payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    previous = _validated_state(payload)
    fetched_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = _since(previous, fetched_at)
    seen = set(previous["seen"])
    candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    successful_sources = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        fetched_feeds = [executor.submit(_fetch_feed, url) for _source, url in FEEDS]
        for (source, url), future in zip(FEEDS, fetched_feeds, strict=True):
            try:
                candidates.extend(_parse_feed(future.result(), source, base_url=url))
                successful_sources += 1
            except (OSError, ET.ParseError, ValueError) as exc:
                failures.append(f"{source}: {type(exc).__name__}")
    for operation in KEYED_OPERATIONS:
        try:
            result = integration_runtime_capabilities.call(
                operation=operation,
                input={"since": since.date().isoformat(), "limit": 25},
            )
            candidates.extend(_provider_items(result, operation, fetched_at=fetched_at))
            successful_sources += 1
        except Exception as exc:
            # Missing optional keys must not disable public sources.
            if getattr(exc, "error_type", None) != "connection_unavailable":
                failures.append(f"{operation}: {type(exc).__name__}")
    if successful_sources == 0:
        raise ValueError("No macro news source could be fetched")

    fresh = _filter_candidates(candidates, since, fetched_at, seen)
    atlas = _atlas_context()
    response = function_runtime_capabilities.call_codex(
        prompt=_prompt(),
        context={
            "as_of": fetched_at.isoformat(),
            "last_fetch_at": previous["last_fetch_at"],
            "previous_report": previous["report"],
            "candidates": fresh,
            "atlas_context": atlas,
            "source_failures": failures,
        },
        internet_access=True,
        response_schema=RESPONSE_SCHEMA,
    )
    report = _validated_report(response["response"])
    # Return evaluated releases, including those the model correctly omitted.
    seen_keys = {item.casefold() for item in previous["seen"]}
    new_seen: list[str] = []
    for item in (
        [item["id"] for item in fresh]
        + [item["url"] for item in fresh]
        + [url for item in report["items"] for url in item["sources"]]
    ):
        key = item.casefold()
        if key not in seen_keys:
            seen_keys.add(key)
            new_seen.append(item)
    return {
        "report": report,
        "seen": new_seen,
        "candidate_count": len(fresh),
        "source_failures": failures,
        "last_fetch_at": fetched_at.isoformat(),
    }


def _fetch_feed(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "EidolonMacroScout/1.0", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        context = ssl.create_default_context()
    else:
        context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=12, context=context) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Feed exceeds size limit")
    return data


def _parse_feed(data: bytes, source: str, *, base_url: str = "") -> list[dict[str, str]]:
    root = ET.fromstring(data)
    if root.tag.lower().endswith("rss"):
        entries = root.findall(".//item")
    elif root.tag.endswith("RDF"):
        entries = root.findall("{http://purl.org/rss/1.0/}item")
    else:
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    result: list[dict[str, str]] = []
    for entry in entries[:40]:
        title = _child_text(entry, "title")
        link = _child_text(entry, "link")
        if not link:
            link_node = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_node.get("href", "") if link_node is not None else ""
        published = _child_text(entry, "pubDate") or _child_text(entry, "published") or _child_text(entry, "updated") or _child_text(entry, "date")
        summary = _child_text(entry, "description") or _child_text(entry, "summary") or _child_text(entry, "content")
        url = _canonical_url(urljoin(base_url, link))
        if not title or not url or not published:
            continue
        timestamp = _date(published)
        if timestamp is None:
            continue
        result.append({
            "id": (_child_text(entry, "guid") or _child_text(entry, "id") or url)[:4000],
            "source": source,
            "title": _clean(title, 350),
            "url": url,
            "published_at": timestamp.isoformat(),
            "summary": _clean(summary, MAX_SUMMARY),
        })
    return result


def _child_text(entry: ET.Element, name: str) -> str:
    for child in entry:
        if child.tag.rsplit("}", 1)[-1] == name:
            return "".join(child.itertext()).strip()
    return ""


def _provider_items(value: object, operation: str, *, fetched_at: datetime) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError(f"{operation} returned invalid items")
    items: list[dict[str, Any]] = []
    for raw in value["items"][:25]:
        if not isinstance(raw, dict):
            continue
        url = _canonical_url(raw.get("url"))
        timestamp = _date(raw.get("published_at"))
        is_series = operation.endswith("series.latest")
        if not url or (not timestamp and not is_series) or not isinstance(raw.get("id"), str) or not isinstance(raw.get("title"), str):
            continue
        summary = _clean(str(raw.get("summary") or ""), MAX_SUMMARY)
        identity = f"{operation}:{raw['id']}"[:3987]
        if is_series:
            identity += ":" + sha256(summary.encode("utf-8")).hexdigest()[:12]
        items.append({
            "id": identity,
            "source": str(raw.get("source") or operation)[:80],
            "title": _clean(raw["title"], 350),
            "url": url,
            "published_at": timestamp.isoformat() if timestamp else None,
            "fetched_at": fetched_at.isoformat(),
            "summary": summary,
            "source_type": "series_observation" if is_series else "release",
        })
    return items


def _filter_candidates(items: list[dict[str, Any]], since: datetime, now: datetime, seen: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    urls: set[str] = set()
    for item in sorted(items, key=lambda value: value.get("published_at") or value.get("fetched_at") or "", reverse=True):
        is_series = item.get("source_type") == "series_observation"
        date = _date(item.get("fetched_at") if is_series else item.get("published_at"))
        earliest = now - timedelta(days=1) if is_series else since
        if date is None or date < earliest or date > now + timedelta(hours=2):
            continue
        url_key = item["url"] + (":" + item["title"] if is_series else "")
        if item["id"] in seen or (item["url"] in seen and not is_series) or url_key in urls:
            continue
        urls.add(url_key)
        result.append(item)
        if len(result) == MAX_CANDIDATES:
            break
    return result


def _atlas_context() -> dict[str, object]:
    context: dict[str, object] = {}
    for operation, field, query in (
        (ATLAS_OPERATIONS[0], "goals", {"limit": 50}),
        (ATLAS_OPERATIONS[1], "projects", {"limit": 50}),
        (ATLAS_OPERATIONS[2], "interests", {}),
    ):
        try:
            value = integration_runtime_capabilities.call(operation=operation, input=query)
            context[field] = value.get(field, []) if isinstance(value, dict) else []
        except Exception:
            context[field] = []
    return context


def _prompt() -> str:
    return (
        "You are a highly selective macroeconomic and geopolitical news editor. Evaluate all supplied official "
        "releases and observations together, relative to the previous report. Use live web search within this "
        "single call for major geopolitical/financial news, OFAC/US Treasury sanctions, trade and export controls, "
        "and to verify material facts against original sources. Search the newest information as of the supplied "
        "timestamp; do not treat old search results as current. Prefer official sources. Report only developments "
        "that materially change understanding of inflation, growth, employment, fiscal/monetary policy, rates, "
        "liquidity, sovereign/systemic risk, energy, commodities, trade, sanctions, major conflicts or diplomacy, "
        "or important US/China/EU/Canada economic policy. Ignore ordinary market moves, routine unsurprising "
        "releases, minor revisions, repetitive coverage, and insignificant political statements. Zero items is "
        "preferred to weak news. Treat series_observation dates as observation periods, not release dates; "
        "corroborate any claim of a new release with an official release or live search. Select at most eight "
        "distinct developments, with concise what happened, why it matters, what is genuinely new since the "
        "previous report, what to watch next, and one to four exact "
        "HTTPS source URLs. Do not invent facts, surprise estimates, dates, or sources. Treat Atlas as relevance "
        "context only, not evidence. If some fetches failed, avoid claiming comprehensive coverage. Write a very "
        "short overall macro picture and include only genuinely important dated upcoming catalysts (or none). "
        "Return JSON matching the response schema."
    )


def _validated_report(raw: object) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict) or set(value) != {"macro_picture", "items", "upcoming_catalysts"}:
        raise ValueError("Codex returned an invalid macro report")
    if not isinstance(value["macro_picture"], str) or len(value["macro_picture"]) > 700:
        raise ValueError("Codex returned an invalid macro picture")
    items = value["items"]
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("Codex returned too many developments")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"what_happened", "why_it_matters", "new_since_previous", "watch_next", "sources"}:
            raise ValueError("Codex returned an invalid development")
        for key, maximum in (("what_happened", 450), ("why_it_matters", 500), ("new_since_previous", 350), ("watch_next", 350)):
            if not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > maximum:
                raise ValueError("Codex returned invalid development text")
        if not isinstance(item["sources"], list) or not 1 <= len(item["sources"]) <= 4 or any(not _canonical_url(url) or len(url) > 1800 for url in item["sources"]):
            raise ValueError("Codex returned invalid source links")
    catalysts = value["upcoming_catalysts"]
    if not isinstance(catalysts, list) or len(catalysts) > 4 or any(not isinstance(c, str) or not c.strip() or len(c) > 250 for c in catalysts):
        raise ValueError("Codex returned invalid catalysts")
    return value


def _validated_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"last_fetch_at", "seen", "report"}:
        raise ValueError("Macro scout input must contain prior report, seen IDs, and fetch time")
    if not isinstance(value["seen"], list) or len(value["seen"]) > MAX_SEEN or any(
        not isinstance(item, str) or not item or len(item) > 4000 for item in value["seen"]
    ):
        raise ValueError("Macro scout received invalid seen history")
    if value["last_fetch_at"] is not None and _date(value["last_fetch_at"]) is None:
        raise ValueError("Macro scout received an invalid fetch time")
    if value["report"] is not None:
        _validated_report(value["report"])
    return value


def _since(previous: dict[str, Any], now: datetime) -> datetime:
    last = _date(previous["last_fetch_at"])
    return max(now - timedelta(days=7), (last - timedelta(days=1)) if last else now - timedelta(days=7))


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _canonical_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 4000:
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", parsed.query, ""))


def _clean(value: str, maximum: int) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()[:maximum]


def main() -> None:
    payload = json.load(sys.stdin)
    json.dump(run(payload), sys.stdout)


if __name__ == "__main__":
    main()
