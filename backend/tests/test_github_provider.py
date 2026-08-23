import base64
import io
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


def test_trending_uses_github_page_order_and_enriches_bounded_readmes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibGitHubProviderAdapter()
    html = """
    <html><body>
      <article class="Box-row">
        <a href="/sponsors/not-the-repository">Sponsor</a>
        <h2><a href="/z/repo">z / repo</a></h2>
        <p>A trending repository.</p>
        <span itemprop="programmingLanguage">Python</span>
        <a href="/z/repo/stargazers">10</a>
        <a href="/z/repo/forks">1</a>
        <img alt="contributor" src="avatar.png">
        <span class="d-inline-block float-sm-right">7 stars this week</span>
      </article>
      <article class="Box-row">
        <h2><a href="/a/repo">a / repo</a></h2>
        <a href="/a/repo/stargazers">2,000</a>
        <a href="/a/repo/forks">25</a>
        <span class="d-inline-block float-sm-right">3 stars this week</span>
      </article>
    </body></html>
    """
    calls = []

    def fake_request_text(url, **kwargs):
        calls.append((url, kwargs))
        if url.startswith("https://github.com/trending"):
            return html, False
        return "# README\nUseful details.", False

    monkeypatch.setattr(adapter, "_request_text", fake_request_text)
    output = adapter.execute(
        OPERATIONS["github.repository.trending.list"],
        {"period": "weekly", "language": "python", "limit": 2},
        "secret-token",
    )
    assert [item["full_name"] for item in output["repositories"]] == ["z/repo", "a/repo"]
    assert output["repositories"][0] == {
        "rank": 1,
        "full_name": "z/repo",
        "description": "A trending repository.",
        "language": "Python",
        "html_url": "https://github.com/z/repo",
        "stars": 10,
        "forks": 1,
        "stars_gained": 7,
        "readme": "# README\nUseful details.",
        "readme_truncated": False,
    }
    assert output["ranking"] == "github_trending"
    assert output["period"] == "weekly"
    assert output["truncated"] is False
    assert calls[0][0] == "https://github.com/trending/python?since=weekly"
    assert calls[0][1]["credential"] is None
    assert calls[1][0] == "https://api.github.com/repos/z/repo/readme"
    assert calls[1][1]["credential"] == "secret-token"


def test_trending_rejects_markup_without_repository_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibGitHubProviderAdapter()
    monkeypatch.setattr(adapter, "_request_text", lambda *_args, **_kwargs: ("<html></html>", False))

    with pytest.raises(IntegrationProviderError) as exc_info:
        adapter.execute(
            OPERATIONS["github.repository.trending.list"],
            {"period": "daily", "limit": 10},
            "unused",
        )

    assert exc_info.value.error_type == "provider_unavailable"
