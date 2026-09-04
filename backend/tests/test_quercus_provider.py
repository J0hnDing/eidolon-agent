from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from urllib.request import Request

import pytest

from app.services import quercus_provider
from app.services.quercus_provider import MAX_JSON_BYTES, QuercusProvider, QuercusProviderError


class JsonResponse(BytesIO):
    def __init__(self, payload: Any, *, url: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))
        self._url = url
        self.headers = headers or {}

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> JsonResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class OversizeResponse(BytesIO):
    headers: dict[str, str] = {}

    def __init__(self, url: str) -> None:
        super().__init__(b"x" * (MAX_JSON_BYTES + 1))
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> OversizeResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class QueueOpener:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: int) -> Any:
        self.requests.append(request)
        return self.responses.pop(0)


def test_opaque_canvas_link_pagination_and_string_id_accept_header() -> None:
    next_url = "https://q.utoronto.ca/api/v1/courses?page=opaque%2Bcursor"
    opener = QueueOpener(
        [
            JsonResponse(
                [{"id": "course-1"}],
                url="https://q.utoronto.ca/api/v1/courses?per_page=100",
                headers={"Link": f'<{next_url}>; rel="next", <https://example.invalid>; rel="last"'},
            ),
            JsonResponse([{"id": "course-2"}], url=next_url),
        ]
    )
    provider = QuercusProvider("secret-token", opener=opener)

    values = provider.get_pages("/courses", {"per_page": 100})

    assert [value["id"] for value in values] == ["course-1", "course-2"]
    assert opener.requests[1].full_url == next_url
    assert opener.requests[0].get_header("Authorization") == "Bearer secret-token"
    assert opener.requests[0].get_header("Accept") == "application/json+canvas-string-ids"


def test_api_transport_rejects_cross_origin_links_and_oversize_json() -> None:
    provider = QuercusProvider("token", opener=QueueOpener([]))
    with pytest.raises(QuercusProviderError, match="unsafe API URL"):
        provider._request_json("https://evil.example/api/v1/courses")

    url = "https://q.utoronto.ca/api/v1/courses"
    provider.opener = QueueOpener([OversizeResponse(url)])
    with pytest.raises(QuercusProviderError, match="safety limit"):
        provider._request_json(url)


def test_untrusted_download_url_never_receives_the_canvas_token() -> None:
    response = BytesIO(b"file")
    response.headers = {"Content-Length": "4"}  # type: ignore[attr-defined]
    opener = QueueOpener([response])
    provider = QuercusProvider("secret-token", opener=opener)

    provider.open_file("42", "https://files.example/private")

    assert opener.requests[0].full_url == "https://q.utoronto.ca/files/42/download"
    assert opener.requests[0].get_header("Authorization") == "Bearer secret-token"


def test_cross_origin_download_redirect_strips_canvas_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quercus_provider, "_is_public_https_url", lambda _url: True)
    request = Request(
        "https://q.utoronto.ca/files/42/download",
        headers={"Authorization": "Bearer secret-token"},
    )

    redirected = quercus_provider._SafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://cdn.example/file",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_individual_file_metadata_uses_only_a_same_course_reference_verifier() -> None:
    url = "https://q.utoronto.ca/api/v1/files/42?verifier=allowed"
    opener = QueueOpener([JsonResponse({"id": "42", "size": 4}, url=url)])
    provider = QuercusProvider("secret-token", opener=opener)

    value = provider.file(
        "42",
        "https://q.utoronto.ca/courses/10/files/42/download?verifier=allowed&ignored=value",
    )

    assert value["size"] == 4
    assert opener.requests[0].full_url == url
