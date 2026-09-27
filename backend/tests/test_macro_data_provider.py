from __future__ import annotations

from typing import Any

import pytest

from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.services.github_provider import IntegrationProviderError
from app.services.macro_data_provider import UrllibMacroDataProviderAdapter


def test_fred_uses_release_dates_and_normalizes_records(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []

    def fake_request(url: str, *, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        requests.append(url)
        assert method == "GET"
        assert body is None
        return {
            "release_dates": [
                {"release_id": 53, "release_name": "Employment Situation", "date": "2026-09-23"},
                {"release_id": 54, "release_name": "Old release", "date": "2026-09-01"},
            ]
        }

    monkeypatch.setattr(UrllibMacroDataProviderAdapter, "_request_json", staticmethod(fake_request))
    output = UrllibMacroDataProviderAdapter("fred").execute(
        DEFAULT_INTEGRATION_REGISTRY.get("fred.release.list"),
        {"since": "2026-09-20", "limit": 5},
        "FRED_SENTINEL",
    )

    assert len(requests) == 1
    assert "/fred/releases/dates?" in requests[0]
    assert "FRED_SENTINEL" in requests[0]
    assert output == {
        "items": [
            {
                "id": "fred:release:53:2026-09-23",
                "title": "Employment Situation",
                "url": "https://fred.stlouisfed.org/releases/53",
                "published_at": "2026-09-23T00:00:00Z",
                "summary": "FRED release: Employment Situation",
                "source": "FRED",
            }
        ]
    }


def test_bls_works_without_a_key_and_marks_observation_period_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(url: str, *, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        assert url.endswith("/publicAPI/v2/timeseries/data/")
        assert method == "POST"
        assert body is not None
        assert "registrationkey" not in body
        return {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": "LNS14000000",
                        "data": [{"year": "2026", "period": "M08", "periodName": "August", "value": "4.3"}],
                    }
                ]
            },
        }

    monkeypatch.setattr(UrllibMacroDataProviderAdapter, "_request_json", staticmethod(fake_request))
    output = UrllibMacroDataProviderAdapter("bls").execute(
        DEFAULT_INTEGRATION_REGISTRY.get("bls.series.latest"),
        {"since": "2026-08-01", "limit": 5},
        None,
    )

    item = output["items"][0]
    assert item["title"] == "Unemployment rate (August 2026)"
    assert item["published_at"] is None
    assert item["summary"] == "BLS observation for August 2026: 4.3"


def test_eia_uses_v2_series_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def fake_request(url: str, *, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        urls.append(url)
        assert method == "GET"
        assert body is None
        return {"response": {"description": "WTI", "unit": "dollars per barrel", "data": [{"period": "2026-09-22", "value": "70.5"}]}}

    monkeypatch.setattr(UrllibMacroDataProviderAdapter, "_request_json", staticmethod(fake_request))
    output = UrllibMacroDataProviderAdapter("eia").execute(
        DEFAULT_INTEGRATION_REGISTRY.get("eia.series.latest"),
        {"since": "2026-09-20", "limit": 5},
        "EIA_SENTINEL",
    )

    assert all("/v2/seriesid/" in url for url in urls)
    assert len(output["items"]) == 2
    assert all(item["published_at"] is None for item in output["items"])
    assert all("EIA_SENTINEL" not in str(item) for item in output["items"])


def test_required_key_providers_reject_missing_keys() -> None:
    operation = DEFAULT_INTEGRATION_REGISTRY.get("bea.series.latest")
    with pytest.raises(IntegrationProviderError, match="not configured"):
        UrllibMacroDataProviderAdapter("bea").execute(operation, {"since": "2026-09-20"}, None)
