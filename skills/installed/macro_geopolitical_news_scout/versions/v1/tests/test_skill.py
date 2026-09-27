from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SKILL_DIR = Path(__file__).resolve().parents[1]
for parent in Path(__file__).resolve().parents:
    backend_dir = parent / "backend"
    if (backend_dir / "function_runtime_capabilities.py").is_file():
        sys.path.insert(0, str(backend_dir))
        break
sys.path.insert(0, str(SKILL_DIR))

import skill  # noqa: E402

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
RSS = b"""<rss><channel><item><guid>release-1</guid><title>Policy decision</title>
<link>https://www.federalreserve.gov/release-1</link>
<pubDate>Tue, 22 Sep 2026 16:00:00 GMT</pubDate>
<description>Rates changed.</description></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>canada-1</id><title>Labour force survey</title>
<link href="https://www150.statcan.gc.ca/labour"/>
<updated>2026-09-22T15:00:00Z</updated><summary>Employment changed.</summary>
</entry></feed>"""
RDF = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>Canada rate decision</title><link>https://www.bankofcanada.ca/decision</link>
<description>Policy rate changed.</description><dc:date>2026-09-22T12:00:00Z</dc:date></item></rdf:RDF>"""


def _report(items=None):
    return {"macro_picture": "Policy remains restrictive.", "items": [] if items is None else items, "upcoming_catalysts": []}


def _integration_call(*, operation, input):
    if operation.startswith("atlas."):
        return {}
    return {"items": []}


def _state(**overrides):
    return {"last_fetch_at": None, "seen": [], "report": None, **overrides}


def test_function_manifest_has_no_schedule_notion_or_cache_permissions():
    manifest = json.loads((SKILL_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["runtime"] == "function"
    assert "schedule" not in manifest
    assert manifest["permissions"]["filesystem_read"] == []
    assert manifest["permissions"]["filesystem_write"] == []
    assert all(
        requirement["provider"] != "notion"
        for requirement in manifest["integration_requirements"]
    )


def test_rss_and_atom_parse_stable_ids_and_dates():
    rss = skill._parse_feed(RSS, "Federal Reserve")
    atom = skill._parse_feed(ATOM, "Statistics Canada")
    rdf = skill._parse_feed(RDF, "Bank of Canada")
    assert rss[0]["id"] == "release-1"
    assert rss[0]["published_at"] == "2026-09-22T16:00:00+00:00"
    assert atom[0]["url"] == "https://www150.statcan.gc.ca/labour"
    assert atom[0]["id"] == "canada-1"
    assert rdf[0]["published_at"] == "2026-09-22T12:00:00+00:00"


def test_one_codex_call_zero_items_and_caller_owned_history(monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr(skill, "FEEDS", (("Federal Reserve", "https://www.federalreserve.gov/test"),))
    monkeypatch.setattr(skill, "_fetch_feed", lambda _url: RSS)
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", _integration_call)

    def codex(**kwargs):
        calls.append(kwargs)
        return {"response": json.dumps(_report())}

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex)
    first = skill.run(_state(), now=NOW)
    second = skill.run(_state(
        last_fetch_at=first["last_fetch_at"],
        seen=first["seen"],
        report=first["report"],
    ), now=NOW)
    assert first["candidate_count"] == 1
    manifest = json.loads((SKILL_DIR / "manifest.json").read_text(encoding="utf-8"))
    Draft202012Validator(manifest["input_schema"]).validate(_state())
    Draft202012Validator(manifest["output_schema"]).validate(first)
    assert second["candidate_count"] == 0
    assert first["report"]["items"] == []
    assert len(calls) == 2
    assert calls[1]["context"]["previous_report"] == _report()
    assert first["last_fetch_at"] == NOW.isoformat()
    assert "release-1" in first["seen"]
    assert second["seen"] == []
    assert calls[0]["internet_access"] is True


def test_selected_web_source_is_returned_to_caller(monkeypatch: pytest.MonkeyPatch):
    source = "https://ofac.treasury.gov/recent-actions/example"
    item = {
        "what_happened": "Major sanctions announced",
        "why_it_matters": "Trade exposure changed.",
        "new_since_previous": "New designations.",
        "watch_next": "Implementation date.",
        "sources": [source],
    }
    monkeypatch.setattr(skill, "FEEDS", (("Federal Reserve", "https://www.federalreserve.gov/test"),))
    monkeypatch.setattr(skill, "_fetch_feed", lambda _url: RSS)

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", _integration_call)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **_kwargs: {"response": json.dumps(_report([item]))})
    result = skill.run(_state(), now=NOW)
    assert result["report"]["items"] == [item]
    assert source in result["seen"]


def test_codex_failure_does_not_return_history(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(skill, "FEEDS", (("Federal Reserve", "https://www.federalreserve.gov/test"),))
    monkeypatch.setattr(skill, "_fetch_feed", lambda _url: RSS)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Codex unavailable")))
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", _integration_call)
    with pytest.raises(RuntimeError, match="Codex unavailable"):
        skill.run(_state(), now=NOW)


def test_missing_optional_keys_do_not_count_as_source_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(skill, "FEEDS", (("Federal Reserve", "https://www.federalreserve.gov/test"),))
    monkeypatch.setattr(skill, "_fetch_feed", lambda _url: RSS)
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **_kwargs: {"response": json.dumps(_report())})

    def integration(*, operation, input):
        if operation in skill.KEYED_OPERATIONS:
            raise skill.integration_runtime_capabilities.IntegrationRuntimeCapabilityError(
                "connection_unavailable", "API key not configured"
            )
        return _integration_call(operation=operation, input=input)

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration)
    assert skill.run(_state(), now=NOW)["source_failures"] == []


def test_filters_old_duplicate_and_seen_candidates():
    base = skill._parse_feed(RSS, "Federal Reserve")[0]
    old = {**base, "id": "old", "url": "https://example.gov/old", "published_at": "2026-09-10T00:00:00+00:00"}
    result = skill._filter_candidates([base, base, old], NOW.replace(day=20), NOW, set())
    assert result == [base]
    assert skill._filter_candidates([base], NOW.replace(day=20), NOW, {base["id"]}) == []


def test_keyed_series_uses_observation_period_and_detects_value_change():
    raw = {"items": [{
        "id": "CPI:2026-08", "title": "CPI", "url": "https://data.bls.gov/timeseries/CPI",
        "published_at": None, "summary": "CPI: 123 (August 2026)", "source": "BLS",
    }]}
    first = skill._provider_items(raw, "bls.series.latest", fetched_at=NOW)[0]
    assert first["source_type"] == "series_observation"
    assert first["published_at"] is None
    assert skill._filter_candidates([first], NOW.replace(day=22), NOW, {first["url"]}) == [first]
    raw["items"][0]["summary"] = "CPI: 124 (August 2026)"
    revised = skill._provider_items(raw, "bls.series.latest", fetched_at=NOW)[0]
    assert revised["id"] != first["id"]
    assert skill._filter_candidates([revised], NOW.replace(day=22), NOW, {first["id"]}) == [revised]


def test_rejects_unbounded_or_unlinked_codex_output():
    item = {
        "what_happened": "Change",
        "why_it_matters": "Material effects",
        "new_since_previous": "New decision",
        "watch_next": "Next meeting",
        "sources": ["http://example.com"],
    }
    with pytest.raises(ValueError, match="source links"):
        skill._validated_report(_report([item]))
    with pytest.raises(ValueError, match="too many developments"):
        skill._validated_report(_report([item] * 9))
