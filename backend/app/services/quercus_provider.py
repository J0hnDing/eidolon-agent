from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

QUERCUS_ORIGIN = "https://q.utoronto.ca"
QUERCUS_API_ROOT = f"{QUERCUS_ORIGIN}/api/v1"
MAX_JSON_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 45


class QuercusProviderError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        if not _is_public_https_url(newurl):
            raise URLError("unsafe Quercus download redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if urlparse(newurl).netloc.casefold() != urlparse(req.full_url).netloc.casefold():
            redirected.remove_header("Authorization")
        return redirected


@dataclass
class QuercusProvider:
    token: str
    opener: Any = None

    def __post_init__(self) -> None:
        if self.opener is None:
            self.opener = build_opener(_SafeRedirectHandler())

    def profile(self) -> dict[str, Any]:
        value = self.get_json("/users/self/profile")
        if not isinstance(value, dict) or not str(value.get("id", "")).strip():
            raise QuercusProviderError("provider_unavailable", "Quercus returned an invalid user profile")
        return value

    def courses(self) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for state in ("active", "invited_or_pending", "completed"):
            for course in self.get_pages(
                "/courses",
                {
                    "enrollment_state": state,
                    "include[]": ["syllabus_body", "term", "total_scores", "current_grading_period_scores"],
                    "per_page": 100,
                },
            ):
                if not isinstance(course, dict) or not str(course.get("id", "")).strip():
                    continue
                normalized = dict(course)
                normalized["_enrollment_state"] = state
                found[str(course["id"])] = normalized
        return list(found.values())

    def modules(self, course_id: str) -> list[dict[str, Any]]:
        modules = self.get_pages(
            f"/courses/{course_id}/modules",
            {"include[]": ["items", "content_details"], "per_page": 100},
        )
        for module in modules:
            if not isinstance(module, dict) or isinstance(module.get("items"), list):
                continue
            module["items"] = self.get_pages(
                f"/courses/{course_id}/modules/{module['id']}/items",
                {"include[]": ["content_details"], "per_page": 100},
            )
        return modules

    def assignments(self, course_id: str) -> list[dict[str, Any]]:
        assignments = self.assignment_index(course_id)
        for assignment in assignments:
            assignment_id = assignment.get("id") if isinstance(assignment, dict) else None
            if assignment_id is None:
                continue
            submission = self._optional_json(
                f"/courses/{course_id}/assignments/{assignment_id}/submissions/self"
                "?include[]=submission_comments&include[]=rubric_assessment"
            )
            if isinstance(submission, dict):
                assignment["submission"] = submission
        return assignments

    def assignment_index(self, course_id: str) -> list[dict[str, Any]]:
        return self.get_pages(
            f"/courses/{course_id}/assignments",
            {"include[]": ["submission"], "order_by": "position", "per_page": 100},
        )

    def pages(self, course_id: str) -> list[dict[str, Any]]:
        return self.get_pages(
            f"/courses/{course_id}/pages",
            {"include[]": ["body"], "sort": "title", "per_page": 100},
        )

    def announcements(self, course_id: str) -> list[dict[str, Any]]:
        return self.get_pages(
            f"/courses/{course_id}/discussion_topics",
            {"only_announcements": "true", "order_by": "recent_activity", "per_page": 100},
        )

    def classic_quizzes(self, course_id: str) -> list[dict[str, Any]]:
        quizzes = self.get_pages(f"/courses/{course_id}/quizzes", {"per_page": 100})
        for quiz in quizzes:
            if not isinstance(quiz, dict) or not quiz.get("id"):
                continue
            quiz_id = str(quiz["id"])
            quiz["_questions"] = self._optional_pages(
                f"/courses/{course_id}/quizzes/{quiz_id}/questions", {"per_page": 100}
            )
            submission = self._optional_json(f"/courses/{course_id}/quizzes/{quiz_id}/submission")
            quiz["_submission"] = submission
            submissions = submission.get("quiz_submissions") if isinstance(submission, dict) else None
            if isinstance(submissions, list) and submissions and isinstance(submissions[0], dict):
                submission_id = submissions[0].get("id")
                if submission_id is not None:
                    quiz["_submission_questions"] = self._optional_pages(
                        f"/quiz_submissions/{submission_id}/questions", {"per_page": 100}
                    )
        return quizzes

    def new_quizzes(self, course_id: str) -> list[dict[str, Any]]:
        quizzes = self._optional_pages(
            f"/courses/{course_id}/quizzes",
            {"per_page": 100},
            api_root=f"{QUERCUS_ORIGIN}/api/quiz/v1",
        )
        for quiz in quizzes:
            assignment_id = quiz.get("id") if isinstance(quiz, dict) else None
            if assignment_id is None:
                continue
            quiz["_items"] = self._optional_pages(
                f"/courses/{course_id}/quizzes/{assignment_id}/items",
                {"per_page": 100},
                api_root=f"{QUERCUS_ORIGIN}/api/quiz/v1",
            )
            submission = self._optional_json(
                f"/courses/{course_id}/assignments/{assignment_id}/submissions/self"
                "?include[]=submission_comments&include[]=rubric_assessment"
            )
            if isinstance(submission, dict):
                quiz["_assignment_submission"] = submission
        return quizzes

    def folders(self, course_id: str) -> list[dict[str, Any]]:
        return self.get_pages(f"/courses/{course_id}/folders", {"per_page": 100})

    def files(self, course_id: str) -> list[dict[str, Any]]:
        return self.get_pages(f"/courses/{course_id}/files", {"per_page": 100})

    def file(self, file_id: str, reference_url: str | None = None) -> dict[str, Any]:
        safe_id = quote(file_id, safe="")
        params: dict[str, str] | None = None
        if reference_url:
            parsed = urlparse(reference_url)
            if (not parsed.netloc or parsed.hostname == "q.utoronto.ca") and re.fullmatch(
                rf".*?/files/{re.escape(file_id)}(?:/[^/]*)?", parsed.path
            ):
                verifier = parse_qs(parsed.query).get("verifier")
                if verifier and verifier[0]:
                    params = {"verifier": verifier[0]}
        value = self.get_json(f"/files/{safe_id}", params)
        if not isinstance(value, dict) or str(value.get("id") or "") != file_id:
            raise QuercusProviderError("provider_unavailable", "Quercus returned invalid file metadata")
        return value

    def open_file(self, file_id: str, download_url: str | None):
        url = download_url if self._is_quercus_https(download_url) else f"{QUERCUS_ORIGIN}/files/{file_id}/download"
        request = Request(url, headers=self._headers())
        try:
            return self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
        except HTTPError as exc:
            raise self._http_error(exc) from None
        except (URLError, TimeoutError, socket.timeout):
            raise QuercusProviderError("provider_unavailable", "Quercus file download is unavailable") from None

    def get_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        api_root: str = QUERCUS_API_ROOT,
    ) -> list[dict[str, Any]]:
        url = self._url(path, params, api_root=api_root)
        results: list[dict[str, Any]] = []
        pages = 0
        while url:
            pages += 1
            if pages > 500:
                raise QuercusProviderError("response_too_large", "Quercus pagination exceeded its safety limit")
            payload, headers = self._request_json(url)
            if not isinstance(payload, list):
                raise QuercusProviderError("provider_unavailable", "Quercus returned an invalid paginated response")
            results.extend(item for item in payload if isinstance(item, dict))
            url = self._next_link(headers.get("Link"))
        return results

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload, _headers = self._request_json(self._url(path, params))
        return payload

    def _request_json(self, url: str) -> tuple[Any, Any]:
        if not self._is_allowed_api_url(url):
            raise QuercusProviderError("provider_unavailable", "Quercus returned an unsafe API URL")
        request = Request(url, headers=self._headers())
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                if not self._is_allowed_api_url(final_url):
                    raise QuercusProviderError("provider_unavailable", "Quercus redirected an API request unsafely")
                body = response.read(MAX_JSON_BYTES + 1)
                if len(body) > MAX_JSON_BYTES:
                    raise QuercusProviderError("response_too_large", "Quercus response exceeded the safety limit")
                return json.loads(body.decode("utf-8")), response.headers
        except HTTPError as exc:
            raise self._http_error(exc) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise QuercusProviderError("provider_unavailable", "Quercus returned invalid JSON") from None
        except (URLError, TimeoutError, socket.timeout):
            raise QuercusProviderError("provider_unavailable", "Quercus is unavailable") from None

    def _optional_json(self, path: str) -> Any:
        try:
            return self.get_json(path)
        except QuercusProviderError as exc:
            if exc.error_type in {"provider_forbidden", "not_found"}:
                return None
            raise

    def _optional_pages(
        self, path: str, params: dict[str, Any], *, api_root: str = QUERCUS_API_ROOT
    ) -> list[dict[str, Any]]:
        try:
            return self.get_pages(path, params, api_root=api_root)
        except QuercusProviderError as exc:
            if exc.error_type in {"provider_forbidden", "not_found"}:
                return []
            raise

    def _url(self, path: str, params: dict[str, Any] | None, *, api_root: str = QUERCUS_API_ROOT) -> str:
        url = urljoin(f"{api_root}/", path.lstrip("/"))
        return f"{url}?{urlencode(params or {}, doseq=True)}" if params else url

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json+canvas-string-ids",
            "User-Agent": "Eidolon-Quercus-Sync/1",
        }

    @staticmethod
    def _next_link(value: str | None) -> str | None:
        if not value:
            return None
        for item in value.split(","):
            target, separator, parameters = item.strip().partition(";")
            if not separator or 'rel="next"' not in parameters:
                continue
            if target.startswith("<") and target.endswith(">"):
                return target[1:-1]
        return None

    @staticmethod
    def _is_quercus_https(url: str | None) -> bool:
        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname == "q.utoronto.ca"

    @staticmethod
    def _is_allowed_api_url(url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "q.utoronto.ca"
            and (parsed.path.startswith("/api/v1/") or parsed.path.startswith("/api/quiz/v1/"))
        )

    @staticmethod
    def _http_error(exc: HTTPError) -> QuercusProviderError:
        error_type = {
            401: "invalid_credential",
            403: "provider_forbidden",
            404: "not_found",
            413: "response_too_large",
            429: "rate_limited",
        }.get(exc.code, "provider_unavailable")
        return QuercusProviderError(error_type, f"Quercus request failed ({exc.code})")


def _is_public_https_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_global for item in addresses)
