from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

import integration_runtime_capabilities
import web_runtime_capabilities
from app.execution.context import InvocationContext
from app.integrations.authorization import _issue_authorized_integration_invocation
from app.integrations.providers.huggingface import HuggingFaceProviderAdapter
from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.integrations.runtime import IntegrationRuntime, IntegrationRuntimeError
from app.routers import integrations, web_apps
from app.schemas.integration import IntegrationInvocationRequest
from app.services.github_provider import IntegrationProviderError
from app.services.huggingface_provider import (
    MAX_CONTENT_CHARS,
    FakeHuggingFaceProviderAdapter,
    UrllibHuggingFaceProviderAdapter,
    _ArxivArticleParser,
)
from app.services.integration_service import IntegrationService


def _provider_payload(paper_id: str = "2602.08025") -> dict:
    return {
        "paper": {
            "id": paper_id,
            "authors": [{"name": "Ada Example"}, {"name": "Lin Example"}],
            "publishedAt": "2026-02-08T15:57:23.000Z",
            "title": "  A useful paper  ",
            "summary": "A useful abstract.",
            "upvotes": 9,
        }
    }


def test_list_and_search_use_official_endpoints_and_return_raw_arrays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibHuggingFaceProviderAdapter()
    calls: list[str] = []

    def fake_request(url: str, **_kwargs):
        calls.append(url)
        return [_provider_payload()]

    monkeypatch.setattr(adapter, "_request_json", fake_request)
    listed = adapter.execute(
        DEFAULT_INTEGRATION_REGISTRY.get("huggingface.list_papers"),
        {"period": "2026-W07", "sort": "trending", "limit": 15},
    )
    searched = adapter.execute(
        DEFAULT_INTEGRATION_REGISTRY.get("huggingface.search_papers"),
        {"query": "world models", "limit": 15},
    )

    assert (
        listed
        == searched
        == [
            {
                "paper_id": "2602.08025",
                "title": "A useful paper",
                "authors": ["Ada Example", "Lin Example"],
                "abstract": "A useful abstract.",
                "url": "https://huggingface.co/papers/2602.08025",
                "pdf_url": "https://arxiv.org/pdf/2602.08025",
                "published_at": "2026-02-08T15:57:23.000Z",
                "upvotes": 9,
            }
        ]
    )
    assert calls[0].startswith("https://huggingface.co/api/daily_papers?")
    assert "week=2026-W07" in calls[0]
    assert "sort=trending" in calls[0]
    assert calls[1].startswith("https://huggingface.co/api/papers/search?")
    assert "q=world+models" in calls[1]


@pytest.mark.parametrize("period", ["2026-13", "2026-W54", "2026-02-30", "not-a-period"])
def test_list_rejects_invalid_calendar_periods(period: str) -> None:
    adapter = UrllibHuggingFaceProviderAdapter()
    with pytest.raises(IntegrationProviderError) as exc_info:
        adapter.execute(
            DEFAULT_INTEGRATION_REGISTRY.get("huggingface.list_papers"),
            {"period": period},
        )
    assert exc_info.value.error_type == "invalid_input"


def test_get_paper_returns_metadata_without_claiming_abstract_is_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibHuggingFaceProviderAdapter()
    monkeypatch.setattr(adapter, "_request_json", lambda *_args, **_kwargs: _provider_payload()["paper"])

    output = adapter.execute(
        DEFAULT_INTEGRATION_REGISTRY.get("huggingface.get_paper"),
        {"paper_id": "2602.08025", "include_content": False},
    )

    assert output["abstract"] == "A useful abstract."
    assert output["content"] is None
    assert output["content_source"] is None
    assert output["content_truncated"] is False


def test_arxiv_html_parser_stops_at_article_after_void_elements() -> None:
    parser = _ArxivArticleParser()
    parser.feed(
        '<main><article class="ltx_document"><h1>Paper</h1><p>Core idea<br>Result<img src="x"></p>'
        "</article><footer>Hugging Face abstract fallback</footer></main>"
    )
    assert parser.text() == "Paper\nCore idea\nResult"


def test_content_falls_back_to_bounded_pdf_text(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibHuggingFaceProviderAdapter()
    calls: list[str] = []

    def fake_request(url: str, **_kwargs):
        calls.append(url)
        if "/html/" in url:
            raise IntegrationProviderError("not_found", "No HTML")
        return b"fake-pdf"

    class Page:
        def __init__(self, text: str) -> None:
            self.value = text

        def extract_text(self) -> str:
            return self.value

    monkeypatch.setattr(adapter, "_request_bytes", fake_request)
    monkeypatch.setattr(
        "app.services.huggingface_provider.PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[Page("A" * (MAX_CONTENT_CHARS + 10)), Page("must not be read")]
        ),
    )

    content, source, truncated = adapter._fetch_content("2602.08025", timeout=30)

    assert content == "A" * MAX_CONTENT_CHARS
    assert source == "arxiv_pdf"
    assert truncated is True
    assert calls == [
        "https://arxiv.org/html/2602.08025",
        "https://arxiv.org/pdf/2602.08025",
    ]


def test_pdf_truncation_reads_past_an_exactly_full_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UrllibHuggingFaceProviderAdapter()

    class Page:
        def __init__(self, text: str) -> None:
            self.value = text

        def extract_text(self) -> str:
            return self.value

    monkeypatch.setattr(
        adapter,
        "_request_bytes",
        lambda url, **_kwargs: (
            (_ for _ in ()).throw(IntegrationProviderError("not_found", "No HTML"))
            if "/html/" in url
            else b"fake-pdf"
        ),
    )
    monkeypatch.setattr(
        "app.services.huggingface_provider.PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[Page("A" * MAX_CONTENT_CHARS), Page("second page")]
        ),
    )

    content, source, truncated = adapter._fetch_content("2602.08025", timeout=30)

    assert content == "A" * MAX_CONTENT_CHARS
    assert source == "arxiv_pdf"
    assert truncated is True


def test_redirects_and_untrusted_urls_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = UrllibHuggingFaceProviderAdapter()

    class RedirectOpener:
        def open(self, *_args, **_kwargs):
            raise HTTPError("https://huggingface.co", 302, "Found", {}, None)

    monkeypatch.setattr("app.services.huggingface_provider.build_opener", lambda *_args: RedirectOpener())
    with pytest.raises(IntegrationProviderError) as redirect:
        adapter._request_bytes(
            "https://huggingface.co/api/papers/2602.08025",
            accept="application/json",
            timeout=10,
            max_bytes=100,
        )
    assert redirect.value.error_type == "provider_unavailable"

    with pytest.raises(IntegrationProviderError) as boundary:
        adapter._request_bytes(
            "https://example.com/api/papers/2602.08025",
            accept="application/json",
            timeout=10,
            max_bytes=100,
        )
    assert boundary.value.error_type == "internal_failure"


def test_public_provider_is_available_and_runtime_validates_array_items() -> None:
    assert IntegrationService.provider_connected(SimpleNamespace(), "huggingface") is True
    operation = DEFAULT_INTEGRATION_REGISTRY.get("huggingface.list_papers")
    assert operation is not None
    context = InvocationContext(principal_kind="user", origin="http")
    invocation = _issue_authorized_integration_invocation(
        context=context,
        operation=operation,
        input_json={"period": "2026-02"},
        provider_id="huggingface",
        provider_account_id=None,
        resource=None,
        authorization_id=None,
    )
    runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=[HuggingFaceProviderAdapter(SimpleNamespace(huggingface=FakeHuggingFaceProviderAdapter()))],
    )
    assert runtime.execute(invocation).output[0]["paper_id"] == "2601.00001"

    invalid = FakeHuggingFaceProviderAdapter()
    invalid.execute = lambda *_args, **_kwargs: [{"paper_id": "invalid"}]
    invalid_runtime = IntegrationRuntime(
        registry=DEFAULT_INTEGRATION_REGISTRY,
        adapters=[HuggingFaceProviderAdapter(SimpleNamespace(huggingface=invalid))],
    )
    with pytest.raises(IntegrationRuntimeError) as exc_info:
        invalid_runtime.execute(invocation)
    assert exc_info.value.error_type == "internal_failure"


class _Response:
    def __init__(self, output: object) -> None:
        self.output = output

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps({"output": self.output}).encode()


def test_runtime_helpers_preserve_array_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://relay")
    monkeypatch.setenv("PERSONAL_AGENT_FUNCTION_CAPABILITY", "function-capability")
    monkeypatch.setenv("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "web-capability")
    monkeypatch.setattr(integration_runtime_capabilities, "urlopen", lambda *_args, **_kwargs: _Response([]))
    monkeypatch.setattr(web_runtime_capabilities, "urlopen", lambda *_args, **_kwargs: _Response([]))

    assert integration_runtime_capabilities.call(operation="huggingface.list_papers", input={}) == []
    assert web_runtime_capabilities.call_integration(operation="huggingface.list_papers", input={}) == []


@pytest.mark.parametrize("output", [None, "text", 3, True])
def test_runtime_helpers_reject_non_container_json_outputs(
    monkeypatch: pytest.MonkeyPatch,
    output: object,
) -> None:
    monkeypatch.setenv("PERSONAL_AGENT_BACKEND_URL", "http://relay")
    monkeypatch.setenv("PERSONAL_AGENT_FUNCTION_CAPABILITY", "function-capability")
    monkeypatch.setenv("PERSONAL_AGENT_WEB_INSTANCE_TOKEN", "web-capability")
    monkeypatch.setattr(
        integration_runtime_capabilities,
        "urlopen",
        lambda *_args, **_kwargs: _Response(output),
    )
    monkeypatch.setattr(
        web_runtime_capabilities,
        "urlopen",
        lambda *_args, **_kwargs: _Response(output),
    )

    with pytest.raises(integration_runtime_capabilities.IntegrationRuntimeCapabilityError):
        integration_runtime_capabilities.call(operation="huggingface.list_papers", input={})
    with pytest.raises(web_runtime_capabilities.WebRuntimeIntegrationError):
        web_runtime_capabilities.call_integration(operation="huggingface.list_papers", input={})


def test_capability_routes_preserve_empty_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = SimpleNamespace(output=[])
    monkeypatch.setattr(
        integrations,
        "InvocationContextFactory",
        lambda *_args, **_kwargs: SimpleNamespace(from_runtime_capability=lambda *_args, **_kwargs: object()),
    )
    monkeypatch.setattr(
        integrations,
        "InvocationExecutor",
        lambda *_args, **_kwargs: SimpleNamespace(execute=lambda *_args, **_kwargs: outcome),
    )
    response = integrations.invoke_function_integration(
        IntegrationInvocationRequest(operation="huggingface.list_papers", input={}),
        authorization="Bearer capability",
        db=object(),
    )
    assert response.output == []

    monkeypatch.setattr(web_apps, "WebAppRuntimeService", lambda *_args, **_kwargs: SimpleNamespace(project_root=None))
    monkeypatch.setattr(
        web_apps,
        "InvocationContextFactory",
        lambda *_args, **_kwargs: SimpleNamespace(from_web_app_capability=lambda *_args, **_kwargs: object()),
    )
    monkeypatch.setattr(
        web_apps,
        "InvocationExecutor",
        lambda *_args, **_kwargs: SimpleNamespace(execute=lambda *_args, **_kwargs: outcome),
    )
    response = web_apps.web_app_integration_capability(
        IntegrationInvocationRequest(operation="huggingface.list_papers", input={}),
        authorization="Bearer capability",
        db=object(),
    )
    assert response.output == []
