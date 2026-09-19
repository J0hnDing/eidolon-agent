from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
for parent in Path(__file__).resolve().parents:
    backend_dir = parent / "backend"
    if (backend_dir / "function_runtime_capabilities.py").is_file():
        sys.path.insert(0, str(backend_dir))
        break
sys.path.insert(0, str(SKILL_DIR))

import skill  # noqa: E402


def _summary(paper_id: str, *, upvotes: int = 10):
    return {
        "paper_id": paper_id,
        "title": f"Paper {paper_id}",
        "authors": ["Ada Researcher", "Grace Scientist"],
        "abstract": f"Abstract for {paper_id}.",
        "url": f"https://huggingface.co/papers/{paper_id}",
        "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
        "published_at": "2026-09-02T00:00:00Z",
        "organization": "Example Research",
        "upvotes": upvotes,
    }


def _paper(
    paper_id: str, *, content: str | None = "Full paper text", truncated: bool = False
):
    return {
        **_summary(paper_id),
        "content": content,
        "content_source": "arxiv_html" if content is not None else None,
        "content_truncated": truncated,
    }


def _codex_response(value):
    return {
        "response": json.dumps(value),
        "model": "test-model",
        "internet_access": False,
    }


def test_run_analyzes_six_unseen_current_month_papers_in_hugging_face_order(
    monkeypatch,
):
    paper_ids = [f"2609.{index:05d}" for index in range(2, 8)]
    monthly = [
        _summary(paper_id, upvotes=100 - index)
        for index, paper_id in enumerate(paper_ids)
    ]
    integration_calls = []

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == (
            100 if operation == skill.GET_PAPER_OPERATION else 30
        )
        integration_calls.append({"operation": operation, "input": input})
        if operation == skill.LIST_PAPERS_OPERATION:
            return monthly
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        return _codex_response(
            {
                "papers": [
                    {"paper_id": paper_id, "analysis": f"Analysis for {paper_id}."}
                    for paper_id in reversed(paper_ids)
                ]
            }
        )

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    result = skill.run(
        {"seen_papers": ["2609.00001v2"]},
        now=datetime(2026, 9, 7, 8, 0, tzinfo=ZoneInfo("America/Toronto")),
    )

    assert integration_calls[0] == {
        "operation": skill.LIST_PAPERS_OPERATION,
        "input": {
            "period": "2026-09",
            "sort": "upvotes",
            "limit": 6,
            "excluded_paper_ids": ["2609.00001"],
        },
    }
    assert [call["operation"] for call in integration_calls] == [
        skill.LIST_PAPERS_OPERATION,
        *([skill.GET_PAPER_OPERATION] * 6),
    ]
    assert len(codex_calls) == 1
    assert [
        paper["paper_id"] for paper in codex_calls[0]["context"]["papers"]
    ] == paper_ids
    analysis_item = codex_calls[0]["response_schema"]["properties"]["papers"]["items"]
    assert analysis_item["properties"]["paper_id"]["enum"] == paper_ids
    assert set(analysis_item["properties"]) == {"paper_id", "analysis"}
    assert codex_calls[0]["internet_access"] is False
    assert [paper["paper_id"] for paper in result["selected_papers"]] == paper_ids
    assert [paper["rank"] for paper in result["selected_papers"]] == list(range(1, 7))
    assert result["selected_papers"][0]["analysis"] == "Analysis for 2609.00002."
    assert result["seen_papers"] == paper_ids


def test_no_unseen_monthly_papers_skips_content_and_codex(monkeypatch):
    calls = []

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == 30
        calls.append((operation, input))
        if operation == skill.LIST_PAPERS_OPERATION:
            return []
        raise AssertionError("No content fetch should run without unseen papers")

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: pytest.fail("Codex must not run without unseen papers"),
    )

    assert skill.run(
        {"seen_papers": ["2609.00001"]},
        now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")),
    ) == {"selected_papers": [], "seen_papers": []}
    assert calls == [
        (
            skill.LIST_PAPERS_OPERATION,
            {
                "period": "2026-09",
                "sort": "upvotes",
                "limit": 6,
                "excluded_paper_ids": ["2609.00001"],
            },
        )
    ]


def test_full_content_is_bounded_and_truncation_signals_are_retained(monkeypatch):
    paper_id = "2609.00001"
    content = "A" * (skill.MAX_CONTENT_PER_PAPER + 100)

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary(paper_id)]
        if operation == skill.GET_PAPER_OPERATION:
            assert timeout_seconds == 100
            return _paper(paper_id, content=content)
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    codex_calls = []
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **kwargs: (
            codex_calls.append(kwargs)
            or _codex_response(
                {"papers": [{"paper_id": paper_id, "analysis": "Grounded analysis."}]}
            )
        ),
    )

    result = skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))

    analyzed = codex_calls[0]["context"]["papers"][0]
    assert len(analyzed["content"]) == skill.MAX_CONTENT_PER_PAPER
    assert "Middle of paper omitted" in analyzed["content"]
    assert analyzed["source_content_truncated"] is False
    assert analyzed["analysis_content_truncated"] is True
    assert result["selected_papers"][0]["analysis_content_truncated"] is True


@pytest.mark.parametrize(
    "response, paper_ids, match",
    [
        (
            {"papers": [{"paper_id": "2609.99999", "analysis": "Unknown."}]},
            ["2609.00001"],
            "unknown or duplicate",
        ),
        (
            {
                "papers": [
                    {"paper_id": "2609.00001", "analysis": "First."},
                    {"paper_id": "2609.00001", "analysis": "Duplicate."},
                ]
            },
            ["2609.00001", "2609.00002"],
            "unknown or duplicate",
        ),
        (
            {"papers": [{"paper_id": "2609.00001", "rank": 1, "analysis": "Extra."}]},
            ["2609.00001"],
            "invalid paper analysis",
        ),
    ],
)
def test_rejects_invalid_analysis(monkeypatch, response, paper_ids, match):
    def integration_call(*, operation, input, **_kwargs):  # noqa: A002
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary(paper_id) for paper_id in paper_ids]
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: _codex_response(response),
    )

    with pytest.raises(ValueError, match=match):
        skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": True},
        {"seen_papers": "2609.00001"},
        {"seen_papers": ["not-an-arxiv-id"]},
    ],
)
def test_rejects_invalid_input_before_provider_calls(monkeypatch, payload):
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        lambda **_kwargs: pytest.fail("Provider must not run for invalid input"),
    )
    with pytest.raises(ValueError):
        skill.run(payload)


def test_missing_full_content_fails_before_codex(monkeypatch):
    paper_id = "2609.00001"

    def integration_call(*, operation, input, **_kwargs):  # noqa: A002
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary(paper_id)]
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(paper_id, content=None)
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: pytest.fail("Codex must not run without full content"),
    )

    with pytest.raises(ValueError, match="full paper content"):
        skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))


def test_analysis_prompt_requires_plain_two_paragraph_explanations_without_selection():
    prompt = skill._analysis_prompt()

    assert "Analyze every supplied paper" in prompt
    assert "Do not select, omit, or rank papers" in prompt
    assert "exactly two short paragraphs" in prompt
    assert "This is a 7B video generation model." in prompt
    assert "simple everyday words" in prompt
    assert "academic wording" in prompt
    assert "decorative adjectives" in prompt
    assert "Do not invent sizes" in prompt
    assert not hasattr(skill, "_selection_prompt")
    assert not hasattr(skill, "ATLAS_GOAL_OPERATION")
