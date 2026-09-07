from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.integrations.types import IntegrationOperationSpec

GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"
MAX_FILE_BYTES = 262_144
MAX_TRENDING_README_BYTES = 12_000
HTML_VOID_ELEMENTS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class IntegrationProviderError(RuntimeError):
    def __init__(self, error_type: str, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retry_after_seconds = retry_after_seconds


class GitHubProviderAdapter(Protocol):
    def validate_credential(self, credential: str) -> dict[str, str]: ...

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GitHubTransportOperation:
    """Provider-private transport limits; never part of the semantic registry."""

    operation_id: str
    timeout_seconds: float
    max_results: int
    max_provider_response_bytes: int
    fake_behavior: str
    repository_scoped: bool


_TRANSPORT_OPERATIONS = {
    value.operation_id: value
    for value in (
        GitHubTransportOperation("github.repository.get", 10, 1, 1_000_000, "repository_metadata", True),
        GitHubTransportOperation("github.repository.tree.list", 15, 500, 5_000_000, "repository_tree", True),
        GitHubTransportOperation("github.repository.file.read", 10, 1, 1_000_000, "repository_file", True),
        GitHubTransportOperation("github.issue.list", 10, 100, 3_000_000, "issue_list", True),
        GitHubTransportOperation("github.pull_request.list", 10, 100, 3_000_000, "pull_request_list", True),
        GitHubTransportOperation(
            "github.repository.trending.list", 15, 25, 5_000_000, "trending_repositories", False
        ),
    )
}


def _transport_operation(operation: IntegrationOperationSpec) -> GitHubTransportOperation:
    operation_id = operation.id
    try:
        return _TRANSPORT_OPERATIONS[operation_id]
    except KeyError:
        raise IntegrationProviderError("internal_failure", "Integration operation is unsupported") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _class_names(attributes: dict[str, str | None]) -> set[str]:
    return set((attributes.get("class") or "").split())


def _compact_text(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


def _count(value: str) -> int:
    compact = value.strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([km]?)", compact)
    if match is None:
        raise ValueError("GitHub Trending count is missing")
    number = float(match.group(1))
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
    return int(number * multiplier)


class _TrendingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.repositories: list[dict[str, Any]] = []
        self._article_depth = 0
        self._current: dict[str, Any] | None = None
        self._inside_heading = False
        self._capture: str | None = None
        self._capture_depth = 0
        self._capture_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._current is None:
            if tag == "article" and "Box-row" in _class_names(attributes):
                self._current = {}
                self._article_depth = 1
            return

        is_void = tag in HTML_VOID_ELEMENTS
        if not is_void:
            self._article_depth += 1
        if tag == "h2":
            self._inside_heading = True
        if self._capture is not None:
            if not is_void:
                self._capture_depth += 1
            return

        href = attributes.get("href") or ""
        if (
            tag == "a"
            and self._inside_heading
            and re.fullmatch(r"/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", href)
        ):
            self._current.setdefault("full_name", href.removeprefix("/"))
            self._current.setdefault("html_url", f"{GITHUB_WEB_BASE}{href}")
        elif tag == "p" and "description" not in self._current:
            self._begin_capture("description")
        elif attributes.get("itemprop") == "programmingLanguage":
            self._begin_capture("language")
        elif tag == "a" and href.endswith("/stargazers"):
            self._begin_capture("stars")
        elif tag == "a" and href.endswith("/forks"):
            self._begin_capture("forks")
        elif tag == "span" and "float-sm-right" in _class_names(attributes):
            self._begin_capture("stars_gained")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in HTML_VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if self._capture is not None:
            self._capture_depth -= 1
            if self._capture_depth == 0:
                self._finish_capture()
        if tag == "h2":
            self._inside_heading = False
        self._article_depth -= 1
        if self._article_depth == 0 and tag == "article":
            self.repositories.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_parts.append(data)

    def _begin_capture(self, name: str) -> None:
        self._capture = name
        self._capture_depth = 1
        self._capture_parts = []

    def _finish_capture(self) -> None:
        assert self._current is not None
        assert self._capture is not None
        text = _compact_text(self._capture_parts)
        self._current[self._capture] = text
        self._capture = None
        self._capture_parts = []


class UrllibGitHubProviderAdapter:
    def validate_credential(self, credential: str) -> dict[str, str]:
        payload = self._request("/user", {}, credential, timeout=10, max_bytes=1_000_000)
        login = payload.get("login")
        account_id = payload.get("id")
        if not isinstance(login, str) or not login or not isinstance(account_id, int):
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid account identity")
        return {"login": login, "id": str(account_id)}

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        operation = _transport_operation(operation)
        operation_id = operation.operation_id
        if operation_id == "github.repository.get":
            payload = self._request(
                self._repo_path(input_json),
                {},
                credential,
                timeout=operation.timeout_seconds,
                max_bytes=operation.max_provider_response_bytes,
            )
            return self._repository(payload)
        if operation_id == "github.repository.tree.list":
            return self._tree(operation, input_json, credential)
        if operation_id == "github.repository.file.read":
            return self._file(operation, input_json, credential)
        if operation_id == "github.issue.list":
            return self._issues(operation, input_json, credential)
        if operation_id == "github.pull_request.list":
            return self._pull_requests(operation, input_json, credential)
        if operation_id == "github.repository.trending.list":
            return self._trending(operation, input_json, credential)
        raise IntegrationProviderError("internal_failure", "Integration operation is unsupported")

    def _tree(
        self,
        operation: GitHubTransportOperation,
        value: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        ref = str(value.get("ref") or "HEAD")
        payload = self._request(
            f"{self._repo_path(value)}/git/trees/{quote(ref, safe='')}?recursive=1",
            {},
            credential,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        raw_entries = payload.get("tree")
        if not isinstance(raw_entries, list):
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid tree response")
        prefix = str(value.get("path") or "").strip("/")
        depth = int(value.get("depth", 2))
        limit = min(int(value.get("max_entries", 200)), operation.max_results)
        entries = []
        bounded_out = False
        for raw in raw_entries:
            if not isinstance(raw, dict) or raw.get("type") not in {"blob", "tree"}:
                continue
            path = str(raw.get("path") or "")
            if prefix and path != prefix and not path.startswith(f"{prefix}/"):
                continue
            relative = path[len(prefix) :].lstrip("/") if prefix else path
            if not relative or len(relative.split("/")) > depth:
                if relative:
                    bounded_out = True
                continue
            entries.append(
                {
                    "path": path,
                    "type": raw["type"],
                    "size": raw.get("size") if isinstance(raw.get("size"), int) else None,
                }
            )
        entries.sort(key=lambda item: (item["path"], item["type"]))
        provider_truncated = bool(payload.get("truncated"))
        truncated = provider_truncated or bounded_out or len(entries) > limit
        return {
            "repository": self._repository_name(value),
            "ref": ref,
            "entries": entries[:limit],
            "truncated": truncated,
        }

    def _file(
        self,
        operation: GitHubTransportOperation,
        value: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        path = str(value["path"])
        ref = str(value.get("ref") or "HEAD")
        payload = self._request(
            f"{self._repo_path(value)}/contents/{quote(path, safe='/')}?{urlencode({'ref': ref})}",
            {},
            credential,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            raise IntegrationProviderError("unsupported_file_type", "The requested GitHub resource is not a text file")
        size = payload.get("size")
        if not isinstance(size, int) or size > MAX_FILE_BYTES:
            raise IntegrationProviderError("response_too_large", "The requested GitHub file exceeds the text size limit")
        content = payload.get("content")
        if not isinstance(content, str):
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid file response")
        try:
            raw = base64.b64decode("".join(content.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid file response") from exc
        if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
            error_type = "response_too_large" if len(raw) > MAX_FILE_BYTES else "unsupported_file_type"
            raise IntegrationProviderError(error_type, "The requested GitHub file is not supported as bounded text")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrationProviderError("unsupported_file_type", "The requested GitHub file is not UTF-8 text") from exc
        return {
            "repository": self._repository_name(value),
            "path": path,
            "ref": ref,
            "text": text,
            "size": len(raw),
            "sha": str(payload.get("sha") or ""),
        }

    def _issues(
        self,
        operation: GitHubTransportOperation,
        value: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        limit = min(int(value.get("limit", 30)), operation.max_results)
        payload = self._request(
            f"{self._repo_path(value)}/issues?{urlencode({'state': value.get('state', 'open'), 'per_page': limit})}",
            {},
            credential,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        if not isinstance(payload, list):
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid issue response")
        issues = [self._issue(item) for item in payload if isinstance(item, dict) and "pull_request" not in item]
        return {
            "repository": self._repository_name(value),
            "issues": issues[:limit],
            "truncated": len(issues) > limit or len(payload) >= limit,
        }

    def _pull_requests(
        self,
        operation: GitHubTransportOperation,
        value: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        limit = min(int(value.get("limit", 30)), operation.max_results)
        payload = self._request(
            f"{self._repo_path(value)}/pulls?{urlencode({'state': value.get('state', 'open'), 'per_page': limit})}",
            {},
            credential,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        if not isinstance(payload, list):
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid pull request response")
        items = [self._pull_request(item) for item in payload if isinstance(item, dict)]
        return {
            "repository": self._repository_name(value),
            "pull_requests": items[:limit],
            "truncated": len(items) >= limit,
        }

    def _trending(
        self,
        operation: GitHubTransportOperation,
        value: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        period = str(value.get("period") or "weekly")
        language = value.get("language")
        selected_language = language.strip() if isinstance(language, str) and language.strip() else None
        language_path = f"/{quote(selected_language.lower(), safe='')}" if selected_language else ""
        limit = min(int(value.get("limit", 10)), operation.max_results)
        html, _ = self._request_text(
            f"{GITHUB_WEB_BASE}/trending{language_path}?{urlencode({'since': period})}",
            credential=None,
            accept="text/html",
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        parser = _TrendingHTMLParser()
        try:
            parser.feed(html)
            raw_items = parser.repositories
            if not raw_items:
                raise ValueError("GitHub Trending did not contain repository entries")
            items = [self._normalize_trending(item, rank) for rank, item in enumerate(raw_items, start=1)]
        except (AssertionError, ValueError) as exc:
            raise IntegrationProviderError(
                "provider_unavailable", "GitHub Trending returned unsupported markup"
            ) from exc

        selected = items[:limit]
        for item in selected:
            readme, readme_truncated = self._read_trending_readme(
                item["full_name"],
                credential,
                timeout=operation.timeout_seconds,
            )
            item["readme"] = readme
            item["readme_truncated"] = readme_truncated
        return {
            "ranking": "github_trending",
            "period": period,
            "language": selected_language,
            "repositories": selected,
            "truncated": len(items) > limit,
        }

    def _read_trending_readme(
        self,
        full_name: str,
        credential: str,
        *,
        timeout: float,
    ) -> tuple[str | None, bool]:
        owner, repository = full_name.split("/", 1)
        try:
            return self._request_text(
                f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/readme",
                credential=credential,
                accept="application/vnd.github.raw+json",
                timeout=timeout,
                max_bytes=MAX_TRENDING_README_BYTES,
                truncate=True,
            )
        except IntegrationProviderError as exc:
            if exc.error_type in {"not_found", "response_too_large", "unsupported_file_type"}:
                return None, False
            raise

    @staticmethod
    def _normalize_trending(value: dict[str, Any], rank: int) -> dict[str, Any]:
        full_name = value.get("full_name")
        html_url = value.get("html_url")
        if not isinstance(full_name, str) or full_name.count("/") != 1 or not isinstance(html_url, str):
            raise ValueError("GitHub Trending repository identity is missing")
        gain_text = str(value.get("stars_gained") or "")
        if "star" not in gain_text.lower():
            raise ValueError("GitHub Trending period stars are missing")
        return {
            "rank": rank,
            "full_name": full_name,
            "description": str(value["description"]) if value.get("description") else None,
            "language": str(value["language"]) if value.get("language") else None,
            "html_url": html_url,
            "stars": _count(str(value.get("stars") or "")),
            "forks": _count(str(value.get("forks") or "")),
            "stars_gained": _count(gain_text),
        }

    def _request(
        self,
        path: str,
        _query: dict[str, Any],
        credential: str,
        *,
        timeout: float,
        max_bytes: int,
    ) -> Any:
        raw, _ = self._request_bytes(
            f"{GITHUB_API_BASE}{path}",
            credential=credential,
            accept="application/vnd.github+json",
            timeout=timeout,
            max_bytes=max_bytes,
        )
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid response") from exc

    def _request_text(
        self,
        url: str,
        *,
        credential: str | None,
        accept: str,
        timeout: float,
        max_bytes: int,
        truncate: bool = False,
    ) -> tuple[str, bool]:
        raw, truncated = self._request_bytes(
            url,
            credential=credential,
            accept=accept,
            timeout=timeout,
            max_bytes=max_bytes,
            truncate=truncate,
        )
        try:
            return raw.decode("utf-8", errors="ignore" if truncated else "strict"), truncated
        except UnicodeDecodeError as exc:
            raise IntegrationProviderError("unsupported_file_type", "GitHub returned non-UTF-8 text") from exc

    def _request_bytes(
        self,
        url: str,
        *,
        credential: str | None,
        accept: str,
        timeout: float,
        max_bytes: int,
        truncate: bool = False,
    ) -> tuple[bytes, bool]:
        parsed_url = urlsplit(url)
        allowed = (
            parsed_url.scheme == "https"
            and (
                (parsed_url.netloc == "api.github.com" and parsed_url.path.startswith("/"))
                or (parsed_url.netloc == "github.com" and parsed_url.path.startswith("/trending"))
            )
        )
        if not allowed:
            raise IntegrationProviderError("internal_failure", "GitHub provider URL is outside the trusted boundary")
        headers = {
            "Accept": accept,
            "User-Agent": "eidolon-github-integration",
        }
        if parsed_url.netloc == "api.github.com":
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        request = Request(
            url,
            headers=headers,
            method="GET",
        )
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=timeout)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise IntegrationProviderError("provider_unavailable", "GitHub redirects are not accepted") from None
            rate_limited = exc.code in {403, 429} and (
                exc.code == 429 or str(exc.headers.get("X-RateLimit-Remaining", "")) == "0"
            )
            mapping = {
                401: ("invalid_credential", "The GitHub credential is invalid or revoked"),
                403: ("provider_forbidden", "GitHub denied the requested read operation"),
                404: ("not_found", "The requested GitHub resource was not found"),
                429: ("rate_limited", "GitHub rate limited the integration request"),
            }
            error_type, message = (
                ("rate_limited", "GitHub rate limited the integration request")
                if rate_limited
                else mapping.get(
                    exc.code,
                    ("provider_unavailable", "GitHub could not complete the integration request"),
                )
            )
            raise IntegrationProviderError(error_type, message) from None
        except TimeoutError:
            raise IntegrationProviderError("provider_timeout", "GitHub did not respond before the timeout") from None
        except (OSError, URLError):
            raise IntegrationProviderError("provider_unavailable", "GitHub is unavailable") from None
        try:
            if 300 <= int(response.status) < 400:
                raise IntegrationProviderError("provider_unavailable", "GitHub redirects are not accepted")
            raw = response.read(max_bytes + 1)
        finally:
            response.close()
        was_truncated = len(raw) > max_bytes
        if was_truncated and not truncate:
            raise IntegrationProviderError("response_too_large", "GitHub response exceeded the size limit")
        return raw[:max_bytes], was_truncated

    @staticmethod
    def _repo_path(value: dict[str, Any]) -> str:
        return f"/repos/{quote(str(value['owner']), safe='')}/{quote(str(value['repository']), safe='')}"

    @staticmethod
    def _repository_name(value: dict[str, Any]) -> str:
        return f"{str(value['owner']).lower()}/{str(value['repository']).lower()}"

    @staticmethod
    def _repository(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "full_name": str(value.get("full_name") or ""),
            "description": value.get("description") if isinstance(value.get("description"), str) else None,
            "private": bool(value.get("private")),
            "default_branch": str(value.get("default_branch") or ""),
            "html_url": str(value.get("html_url") or ""),
            "stars": int(value.get("stargazers_count") or value.get("stars") or 0),
            "forks": int(value.get("forks_count") or value.get("forks") or 0),
            "open_issues": int(value.get("open_issues_count") or value.get("open_issues") or 0),
            "updated_at": str(value.get("updated_at") or ""),
        }

    @staticmethod
    def _issue(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": int(value.get("number") or 0),
            "title": str(value.get("title") or ""),
            "state": str(value.get("state") or ""),
            "html_url": str(value.get("html_url") or ""),
            "author": str((value.get("user") or {}).get("login") or ""),
            "labels": [
                str(item.get("name"))
                for item in value.get("labels", [])
                if isinstance(item, dict) and item.get("name")
            ][:20],
            "created_at": str(value.get("created_at") or ""),
            "updated_at": str(value.get("updated_at") or ""),
        }

    @staticmethod
    def _pull_request(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": int(value.get("number") or 0),
            "title": str(value.get("title") or ""),
            "state": str(value.get("state") or ""),
            "draft": bool(value.get("draft")),
            "html_url": str(value.get("html_url") or ""),
            "author": str((value.get("user") or {}).get("login") or ""),
            "head": str((value.get("head") or {}).get("ref") or ""),
            "base": str((value.get("base") or {}).get("ref") or ""),
            "created_at": str(value.get("created_at") or ""),
            "updated_at": str(value.get("updated_at") or ""),
        }


class FakeGitHubProviderAdapter:
    def __init__(self, *, login: str = "fake-octocat", account_id: str = "1001") -> None:
        self.login = login
        self.account_id = account_id
        self.error_type: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def validate_credential(self, credential: str) -> dict[str, str]:
        if self.error_type:
            raise IntegrationProviderError(self.error_type, "Fake GitHub validation failed")
        if credential.startswith("invalid"):
            raise IntegrationProviderError("invalid_credential", "The GitHub credential is invalid or revoked")
        return {"login": self.login, "id": self.account_id}

    def execute(
        self,
        operation: IntegrationOperationSpec,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        del credential
        operation = _transport_operation(operation)
        self.calls.append((operation.operation_id, dict(input_json)))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, f"Fake {self.error_type}")
        repository = (
            f"{input_json['owner'].lower()}/{input_json['repository'].lower()}"
            if operation.repository_scoped
            else None
        )
        base_repository = {
            "full_name": repository or "sample/trending",
            "description": "Deterministic fake repository",
            "private": False,
            "default_branch": "main",
            "html_url": f"https://github.com/{repository or 'sample/trending'}",
            "stars": 42,
            "forks": 7,
            "open_issues": 3,
            "updated_at": "2026-01-01T00:00:00Z",
        }
        if operation.fake_behavior == "repository_metadata":
            return base_repository
        if operation.fake_behavior == "repository_tree":
            return {
                "repository": repository,
                "ref": str(input_json.get("ref") or "HEAD"),
                "entries": [{"path": "README.md", "type": "blob", "size": 12}],
                "truncated": False,
            }
        if operation.fake_behavior == "repository_file":
            return {
                "repository": repository,
                "path": input_json["path"],
                "ref": str(input_json.get("ref") or "HEAD"),
                "text": "# Fake\n",
                "size": 7,
                "sha": "fake-sha",
            }
        if operation.fake_behavior == "issue_list":
            return {
                "repository": repository,
                "issues": [
                    {
                        "number": 1,
                        "title": "Fake issue",
                        "state": "open",
                        "html_url": f"https://github.com/{repository}/issues/1",
                        "author": "fake-octocat",
                        "labels": ["fake"],
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "truncated": False,
            }
        if operation.fake_behavior == "pull_request_list":
            return {
                "repository": repository,
                "pull_requests": [
                    {
                        "number": 2,
                        "title": "Fake PR",
                        "state": "open",
                        "draft": False,
                        "html_url": f"https://github.com/{repository}/pull/2",
                        "author": "fake-octocat",
                        "head": "feature",
                        "base": "main",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "truncated": False,
            }
        if operation.fake_behavior == "trending_repositories":
            return {
                "ranking": "github_trending",
                "period": input_json.get("period", "weekly"),
                "language": input_json.get("language"),
                "repositories": [
                    {
                        "rank": 1,
                        "full_name": base_repository["full_name"],
                        "description": base_repository["description"],
                        "language": input_json.get("language") or "Python",
                        "html_url": base_repository["html_url"],
                        "stars": base_repository["stars"],
                        "forks": base_repository["forks"],
                        "stars_gained": 5,
                        "readme": "# Deterministic fake repository\n",
                        "readme_truncated": False,
                    }
                ],
                "truncated": False,
            }
        raise IntegrationProviderError("internal_failure", "Fake integration operation is unsupported")
