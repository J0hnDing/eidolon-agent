from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.integrations.types import IntegrationOperationSpec
from app.services.github_provider import IntegrationProviderError

HUGGINGFACE_BASE = "https://huggingface.co"
ARXIV_BASE = "https://arxiv.org"
MAX_METADATA_RESPONSE_BYTES = 5_000_000
MAX_HTML_RESPONSE_BYTES = 8_000_000
MAX_PDF_RESPONSE_BYTES = 20_000_000
MAX_CONTENT_CHARS = 500_000
_PAPER_ID = re.compile(r"^(?P<base>\d{4}\.\d{4,5})(?:v\d+)?$")
_MONTH = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})$")
_WEEK = re.compile(r"^(?P<year>\d{4})-W(?P<week>\d{2})$")


class HuggingFaceProviderAdapter(Protocol):
    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
    ) -> dict[str, Any] | list[dict[str, Any]]: ...


@dataclass(frozen=True)
class HuggingFaceTransportOperation:
    operation_id: str
    timeout_seconds: float
    max_results: int


_TRANSPORT_OPERATIONS = {
    value.operation_id: value
    for value in (
        HuggingFaceTransportOperation("huggingface.list_papers", 20, 100),
        HuggingFaceTransportOperation("huggingface.search_papers", 20, 120),
        HuggingFaceTransportOperation("huggingface.get_paper", 30, 1),
    )
}


def _transport_operation(operation: IntegrationOperationSpec) -> HuggingFaceTransportOperation:
    try:
        return _TRANSPORT_OPERATIONS[operation.id]
    except KeyError:
        raise IntegrationProviderError(
            "internal_failure", "Hugging Face integration operation is unsupported"
        ) from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


class _ArxivArticleParser(HTMLParser):
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    _BLOCK_TAGS = {
        "article",
        "blockquote",
        "br",
        "caption",
        "div",
        "figcaption",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._article_depth = 0
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._article_depth == 0:
            classes = set((attributes.get("class") or "").split())
            if tag == "article" and "ltx_document" in classes:
                self._article_depth = 1
            return
        if tag in self._VOID_TAGS:
            if not self._ignored_depth and tag in self._BLOCK_TAGS:
                self.parts.append("\n")
            return
        self._article_depth += 1
        if self._ignored_depth:
            self._ignored_depth += 1
            return
        if tag in {"script", "style", "svg"}:
            self._ignored_depth = 1
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._article_depth == 0:
            return
        if self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        self._article_depth -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._article_depth and not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


class UrllibHuggingFaceProviderAdapter:
    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
    ) -> dict[str, Any] | list[dict[str, Any]]:
        transport = _transport_operation(operation)
        if transport.operation_id == "huggingface.list_papers":
            return self._list_papers(transport, input_json)
        if transport.operation_id == "huggingface.search_papers":
            return self._search_papers(transport, input_json)
        if transport.operation_id == "huggingface.get_paper":
            return self._get_paper(transport, input_json)
        raise IntegrationProviderError("internal_failure", "Hugging Face integration operation is unsupported")

    def _list_papers(
        self,
        operation: HuggingFaceTransportOperation,
        value: dict[str, Any],
    ) -> list[dict[str, Any]]:
        period_key, period = _period_query(str(value["period"]))
        limit = min(int(value.get("limit", 15)), operation.max_results)
        query = {period_key: period, "sort": value.get("sort", "trending"), "limit": limit, "p": 0}
        payload = self._request_json(
            f"{HUGGINGFACE_BASE}/api/daily_papers?{urlencode(query)}",
            timeout=operation.timeout_seconds,
        )
        return _normalize_paper_list(payload, limit)

    def _search_papers(
        self,
        operation: HuggingFaceTransportOperation,
        value: dict[str, Any],
    ) -> list[dict[str, Any]]:
        limit = min(int(value.get("limit", 15)), operation.max_results)
        payload = self._request_json(
            f"{HUGGINGFACE_BASE}/api/papers/search?{urlencode({'q': value['query'], 'limit': limit})}",
            timeout=operation.timeout_seconds,
        )
        return _normalize_paper_list(payload, limit)

    def _get_paper(
        self,
        operation: HuggingFaceTransportOperation,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        requested_id = str(value["paper_id"])
        match = _PAPER_ID.fullmatch(requested_id)
        if match is None:
            raise IntegrationProviderError("invalid_input", "paper_id must be a stable arXiv identifier")
        base_id = match.group("base")
        payload = self._request_json(
            f"{HUGGINGFACE_BASE}/api/papers/{quote(base_id, safe='')}",
            timeout=operation.timeout_seconds,
        )
        paper = _normalize_paper(payload)
        if paper["paper_id"] != base_id:
            raise IntegrationProviderError("provider_unavailable", "Hugging Face returned a different paper identity")
        paper.update({"content": None, "content_source": None, "content_truncated": False})
        if value.get("include_content", False):
            content, source, truncated = self._fetch_content(
                requested_id,
                timeout=operation.timeout_seconds,
            )
            paper.update(
                {
                    "content": content,
                    "content_source": source,
                    "content_truncated": truncated,
                }
            )
        return paper

    def _fetch_content(self, paper_id: str, *, timeout: float) -> tuple[str, str, bool]:
        try:
            raw = self._request_bytes(
                f"{ARXIV_BASE}/html/{quote(paper_id, safe='')}",
                accept="text/html",
                timeout=timeout,
                max_bytes=MAX_HTML_RESPONSE_BYTES,
            )
            parser = _ArxivArticleParser()
            parser.feed(raw.decode("utf-8"))
            content = parser.text()
            if len(content) < 500:
                raise IntegrationProviderError("unsupported_file_type", "arXiv HTML did not contain the full paper")
            return _bounded_content(content, "arxiv_html")
        except (UnicodeDecodeError, IntegrationProviderError) as exc:
            if isinstance(exc, IntegrationProviderError) and exc.error_type not in {
                "not_found",
                "provider_unavailable",
                "unsupported_file_type",
            }:
                raise

        raw_pdf = self._request_bytes(
            f"{ARXIV_BASE}/pdf/{quote(paper_id, safe='')}",
            accept="application/pdf",
            timeout=timeout,
            max_bytes=MAX_PDF_RESPONSE_BYTES,
        )
        try:
            reader = PdfReader(io.BytesIO(raw_pdf))
            parts: list[str] = []
            size = 0
            for page in reader.pages:
                text = (page.extract_text() or "").strip()
                if not text:
                    continue
                size += (2 if parts else 0) + len(text)
                parts.append(text)
                if size > MAX_CONTENT_CHARS:
                    break
            content = "\n\n".join(parts)
        except (PdfReadError, ValueError, OSError) as exc:
            raise IntegrationProviderError("unsupported_file_type", "The arXiv PDF could not be read as text") from exc
        if len(content) < 500:
            raise IntegrationProviderError(
                "unsupported_file_type", "The arXiv PDF did not contain readable full-paper text"
            )
        return _bounded_content(content, "arxiv_pdf")

    def _request_json(self, url: str, *, timeout: float) -> Any:
        raw = self._request_bytes(
            url,
            accept="application/json",
            timeout=timeout,
            max_bytes=MAX_METADATA_RESPONSE_BYTES,
        )
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationProviderError("provider_unavailable", "Hugging Face returned an invalid response") from exc

    def _request_bytes(
        self,
        url: str,
        *,
        accept: str,
        timeout: float,
        max_bytes: int,
    ) -> bytes:
        parsed = urlsplit(url)
        allowed = parsed.scheme == "https" and (
            (
                parsed.netloc == "huggingface.co"
                and (
                    parsed.path == "/api/daily_papers"
                    or parsed.path == "/api/papers/search"
                    or parsed.path.startswith("/api/papers/")
                )
            )
            or (parsed.netloc == "arxiv.org" and (parsed.path.startswith("/html/") or parsed.path.startswith("/pdf/")))
        )
        if not allowed:
            raise IntegrationProviderError(
                "internal_failure", "Hugging Face provider URL is outside the trusted boundary"
            )
        request = Request(
            url,
            headers={"Accept": accept, "User-Agent": "eidolon-huggingface-integration"},
            method="GET",
        )
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=timeout)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise IntegrationProviderError("provider_unavailable", "Provider redirects are not accepted") from None
            mapping = {
                403: ("provider_forbidden", "The paper provider denied the request"),
                404: ("not_found", "The requested paper resource was not found"),
                429: ("rate_limited", "The paper provider rate limited the request"),
            }
            error_type, message = mapping.get(
                exc.code,
                ("provider_unavailable", "The paper provider could not complete the request"),
            )
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
            raise IntegrationProviderError(
                error_type,
                message,
                retry_after_seconds=retry_after,
            ) from None
        except TimeoutError:
            raise IntegrationProviderError(
                "provider_timeout", "The paper provider did not respond before the timeout"
            ) from None
        except (OSError, URLError):
            raise IntegrationProviderError("provider_unavailable", "The paper provider is unavailable") from None
        try:
            raw = response.read(max_bytes + 1)
        finally:
            response.close()
        if len(raw) > max_bytes:
            raise IntegrationProviderError("response_too_large", "The paper provider response exceeded the size limit")
        return raw


class FakeHuggingFaceProviderAdapter:
    def __init__(self) -> None:
        self.error_type: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
    ) -> dict[str, Any] | list[dict[str, Any]]:
        transport = _transport_operation(operation)
        self.calls.append((transport.operation_id, dict(input_json)))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, f"Fake {self.error_type}")
        paper = {
            "paper_id": "2601.00001",
            "title": "Deterministic fake paper",
            "authors": ["Ada Example", "Lin Example"],
            "abstract": "A deterministic abstract for provider tests.",
            "url": "https://huggingface.co/papers/2601.00001",
            "pdf_url": "https://arxiv.org/pdf/2601.00001",
            "published_at": "2026-01-01T00:00:00.000Z",
            "upvotes": 42,
        }
        if transport.operation_id in {
            "huggingface.list_papers",
            "huggingface.search_papers",
        }:
            return [paper]
        requested_id = str(input_json.get("paper_id") or paper["paper_id"])
        match = _PAPER_ID.fullmatch(requested_id)
        if match is None:
            raise IntegrationProviderError("invalid_input", "paper_id must be a stable arXiv identifier")
        paper_id = match.group("base")
        paper.update(
            {
                "paper_id": paper_id,
                "url": f"{HUGGINGFACE_BASE}/papers/{paper_id}",
                "pdf_url": f"{ARXIV_BASE}/pdf/{paper_id}",
            }
        )
        include_content = bool(input_json.get("include_content", False))
        return {
            **paper,
            "content": "Full deterministic paper content." if include_content else None,
            "content_source": "arxiv_html" if include_content else None,
            "content_truncated": False,
        }


def _period_query(period: str) -> tuple[str, str]:
    month_match = _MONTH.fullmatch(period)
    if month_match is not None:
        try:
            date(int(month_match.group("year")), int(month_match.group("month")), 1)
        except ValueError:
            raise IntegrationProviderError("invalid_input", "period month is invalid") from None
        return "month", period
    week_match = _WEEK.fullmatch(period)
    if week_match is not None:
        try:
            date.fromisocalendar(int(week_match.group("year")), int(week_match.group("week")), 1)
        except ValueError:
            raise IntegrationProviderError("invalid_input", "period week is invalid") from None
        return "week", period
    try:
        date.fromisoformat(period)
    except ValueError:
        raise IntegrationProviderError("invalid_input", "period must be an ISO month, week, or date") from None
    return "date", period


def _normalize_paper_list(payload: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise IntegrationProviderError("provider_unavailable", "Hugging Face returned an invalid paper list")
    return [_normalize_paper(item) for item in payload[:limit]]


def _normalize_paper(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise IntegrationProviderError("provider_unavailable", "Hugging Face returned an invalid paper")
    nested = payload.get("paper")
    paper = nested if isinstance(nested, dict) else payload
    paper_id = paper.get("id")
    title = paper.get("title") or payload.get("title")
    if not isinstance(paper_id, str) or _PAPER_ID.fullmatch(paper_id) is None:
        raise IntegrationProviderError("provider_unavailable", "Hugging Face returned a paper without a stable ID")
    if not isinstance(title, str) or not title.strip():
        raise IntegrationProviderError("provider_unavailable", "Hugging Face returned a paper without a title")
    authors_payload = paper.get("authors")
    authors = []
    if isinstance(authors_payload, list):
        authors = [
            str(author["name"]).strip()
            for author in authors_payload
            if isinstance(author, dict) and isinstance(author.get("name"), str) and str(author["name"]).strip()
        ]
    base_id = _PAPER_ID.fullmatch(paper_id).group("base")
    published_at = paper.get("publishedAt") or payload.get("publishedAt")
    upvotes = paper.get("upvotes")
    return {
        "paper_id": base_id,
        "title": " ".join(title.split()),
        "authors": authors,
        "abstract": str(paper.get("summary") or payload.get("summary") or "").strip(),
        "url": f"{HUGGINGFACE_BASE}/papers/{base_id}",
        "pdf_url": f"{ARXIV_BASE}/pdf/{base_id}",
        "published_at": published_at if isinstance(published_at, str) else None,
        "upvotes": upvotes if isinstance(upvotes, int) and not isinstance(upvotes, bool) else 0,
    }


def _bounded_content(content: str, source: str) -> tuple[str, str, bool]:
    truncated = len(content) > MAX_CONTENT_CHARS
    return content[:MAX_CONTENT_CHARS], source, truncated


def _retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


__all__ = [
    "FakeHuggingFaceProviderAdapter",
    "HuggingFaceProviderAdapter",
    "UrllibHuggingFaceProviderAdapter",
]
