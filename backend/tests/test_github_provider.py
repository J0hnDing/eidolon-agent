import base64
import io
import json
from urllib.error import HTTPError

import pytest

from app.services.github_provider import IntegrationProviderError, UrllibGitHubProviderAdapter
from app.services.integration_registry import OPERATIONS


def test_tree_depth_entry_limit_and_provider_truncation_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGitHubProviderAdapter()
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *_args, **_kwargs: {
            "tree": [
                {"path": "src", "type": "tree"},
                {"path": "src/app.py", "type": "blob", "size": 10},
                {"path": "src/deep/value.py", "type": "blob", "size": 20},
                {"path": "README.md", "type": "blob", "size": 5},
            ],
            "truncated": False,
        },
    )
    output = adapter.execute(
        OPERATIONS["github.repository.tree.list"],
        {"owner": "octo", "repository": "demo", "path": "src", "depth": 1, "max_entries": 1},
        "unused",
    )
    assert output["entries"] == [{"path": "src/app.py", "type": "blob", "size": 10}]
    assert output["truncated"] is True


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"type": "file", "encoding": "base64", "size": 4, "content": base64.b64encode(b"a\x00b").decode(), "sha": "x"},
            "unsupported_file_type",
        ),
        (
            {"type": "file", "encoding": "base64", "size": 262_145, "content": "", "sha": "x"},
            "response_too_large",
        ),
        ({"type": "dir", "encoding": None, "size": 0, "content": None, "sha": "x"}, "unsupported_file_type"),
    ],
)
def test_file_read_rejects_binary_oversized_and_non_file_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    expected: str,
) -> None:
    adapter = UrllibGitHubProviderAdapter()
    monkeypatch.setattr(adapter, "_request", lambda *_args, **_kwargs: payload)
    with pytest.raises(IntegrationProviderError) as exc_info:
        adapter.execute(
            OPERATIONS["github.repository.file.read"],
            {"owner": "octo", "repository": "demo", "path": "asset.bin"},
            "unused",
        )
    assert exc_info.value.error_type == expected


def test_file_read_accepts_bounded_utf8_with_provider_base64_line_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibGitHubProviderAdapter()
    encoded = base64.b64encode(b"hello world").decode()
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *_args, **_kwargs: {
            "type": "file",
            "encoding": "base64",
            "size": 11,
            "content": f"{encoded[:8]}\n{encoded[8:]}",
            "sha": "abc",
        },
    )
    output = adapter.execute(
        OPERATIONS["github.repository.file.read"],
        {"owner": "octo", "repository": "demo", "path": "README.md"},
        "unused",
    )
    assert output["text"] == "hello world"


def test_redirect_and_oversized_provider_response_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibGitHubProviderAdapter()

    class RedirectOpener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("https://api.github.com/user", 302, "Found", {}, io.BytesIO(b""))

    monkeypatch.setattr("app.services.github_provider.build_opener", lambda *_args: RedirectOpener())
    with pytest.raises(IntegrationProviderError) as redirect:
        adapter.validate_credential("unused")
    assert redirect.value.error_type == "provider_unavailable"

    class LargeResponse:
        status = 200

        def read(self, limit):
            return b"{" + (b" " * limit)

        def close(self):
            return None

    class LargeOpener:
        def open(self, *_args, **_kwargs):
            return LargeResponse()

    monkeypatch.setattr("app.services.github_provider.build_opener", lambda *_args: LargeOpener())
    with pytest.raises(IntegrationProviderError) as large:
        adapter.validate_credential("unused")
    assert large.value.error_type == "response_too_large"


def test_trending_ranking_is_deterministic_and_not_live_order_dependent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibGitHubProviderAdapter()
    payload = {
        "total_count": 3,
        "items": [
            {"full_name": "z/repo", "stargazers_count": 10, "forks_count": 1},
            {"full_name": "a/repo", "stargazers_count": 10, "forks_count": 2},
            {"full_name": "b/repo", "stargazers_count": 10, "forks_count": 2},
        ],
    }
    monkeypatch.setattr(adapter, "_request", lambda *_args, **_kwargs: json.loads(json.dumps(payload)))
    output = adapter.execute(
        OPERATIONS["github.repository.trending.list"],
        {"lookback_days": 30, "limit": 3},
        "unused",
    )
    assert [item["full_name"] for item in output["repositories"]] == ["a/repo", "b/repo", "z/repo"]
    assert output["ranking"] == "stars_desc_forks_desc_full_name_asc"
    assert output["lookback_days"] == 30
