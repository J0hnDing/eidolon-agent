from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.integration_registry import IntegrationOperation

GITHUB_API_BASE = "https://api.github.com"
MAX_FILE_BYTES = 262_144


class IntegrationProviderError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class GitHubProviderAdapter(Protocol):
    def validate_credential(self, credential: str) -> dict[str, str]: ...

    def execute(
        self,
        operation: IntegrationOperation,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


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
        operation: IntegrationOperation,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
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
        operation: IntegrationOperation,
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
        operation: IntegrationOperation,
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
        operation: IntegrationOperation,
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
        operation: IntegrationOperation,
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
        operation: IntegrationOperation,
        value: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        lookback_days = 30
        start = (datetime.now(UTC) - timedelta(days=lookback_days)).date().isoformat()
        language = value.get("language")
        query = f"created:>={start} is:public fork:false archived:false"
        if isinstance(language, str) and language.strip():
            query += f" language:{language.strip()}"
        limit = min(int(value.get("limit", 10)), operation.max_results)
        payload = self._request(
            f"/search/repositories?{urlencode({'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': limit})}",
            {},
            credential,
            timeout=operation.timeout_seconds,
            max_bytes=operation.max_provider_response_bytes,
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid search response")
        items = [self._repository(item) for item in raw_items if isinstance(item, dict)]
        items.sort(key=lambda item: (-item["stars"], -item["forks"], item["full_name"].lower()))
        return {
            "ranking": "stars_desc_forks_desc_full_name_asc",
            "lookback_days": lookback_days,
            "language": language.strip() if isinstance(language, str) and language.strip() else None,
            "repositories": items[:limit],
            "truncated": int(payload.get("total_count") or 0) > limit,
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
        request = Request(
            f"{GITHUB_API_BASE}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {credential}",
                "User-Agent": "eidolon-github-integration",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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
        if len(raw) > max_bytes:
            raise IntegrationProviderError("response_too_large", "GitHub response exceeded the size limit")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationProviderError("provider_unavailable", "GitHub returned an invalid response") from exc

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
        operation: IntegrationOperation,
        input_json: dict[str, Any],
        credential: str,
    ) -> dict[str, Any]:
        del credential
        self.calls.append((operation.operation_id, dict(input_json)))
        if self.error_type:
            raise IntegrationProviderError(self.error_type, f"Fake {self.error_type}")
        repository = (
            f"{input_json['owner'].lower()}/{input_json['repository'].lower()}"
            if operation.resource_scope == "repository"
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
                "ranking": "stars_desc_forks_desc_full_name_asc",
                "lookback_days": 30,
                "language": input_json.get("language"),
                "repositories": [base_repository],
                "truncated": False,
            }
        raise IntegrationProviderError("internal_failure", "Fake integration operation is unsupported")
