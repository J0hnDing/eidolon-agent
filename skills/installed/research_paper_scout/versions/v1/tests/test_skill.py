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


def _summary(paper_id: str, *, title: str | None = None, upvotes: int = 10):
    return {
        "paper_id": paper_id,
        "title": title or f"Paper {paper_id}",
        "authors": ["Ada Researcher", "Grace Scientist"],
        "abstract": f"Abstract for {paper_id}.",
        "url": f"https://huggingface.co/papers/{paper_id}",
        "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
        "published_at": "2026-09-02T00:00:00Z",
        "upvotes": upvotes,
    }


def _paper(paper_id: str, *, content: str | None = "Full paper text", truncated: bool = False):
    return {
        **_summary(paper_id),
        "content": content,
        "content_source": "arxiv_html" if content is not None else None,
        "content_truncated": truncated,
    }


def _guide():
    return {
        "problem": "The problem the work addresses.",
        "core_idea": "The central insight.",
        "method": "The method used by the authors.",
        "novelty": "What differs from prior work.",
        "important_results": "The main reported results.",
        "limitations": "The limits of the evidence.",
        "prerequisites": ["Basic machine learning"],
        "recommended_reading_order": ["Abstract", "Figures", "Method", "Results"],
        "sections_to_skip_initially": ["Appendix proofs"],
        "key_questions": ["Does the evaluation support the claim?"],
        "expected_takeaways": ["Understand the core tradeoff"],
    }


def _codex_response(value):
    return {"response": json.dumps(value), "model": "test-model", "internet_access": False}


def test_run_discovers_two_months_deduplicates_excludes_and_returns_grounded_analysis(monkeypatch):
    current = [_summary("2609.00001", upvotes=90), _summary("2609.00002", upvotes=80)]
    previous = [_summary("2609.00002", upvotes=70), _summary("2608.00003", upvotes=60)]
    integration_calls = []

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == (100 if operation == skill.GET_PAPER_OPERATION else 30)
        integration_calls.append({"operation": operation, "input": input})
        if operation == skill.LIST_PAPERS_OPERATION:
            return current if input["period"] == "2026-09" else previous
        if operation == skill.ATLAS_GOAL_OPERATION:
            return {"goals": [{"title": "Learn model evaluation"}], "progressions": []}
        if operation == skill.ATLAS_INTEREST_OPERATION:
            return {"hobbies": [{"title": "AI research"}], "preferences": []}
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration_call)
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        if len(codex_calls) == 1:
            return _codex_response({"paper_ids": ["2608.00003", "2609.00002"]})
        return _codex_response(
            {
                "papers": [
                    {"paper_id": "2608.00003", "rank": 2, "analysis": "A useful older candidate."},
                    {"paper_id": "2609.00002", "rank": 1, "analysis": "The strongest current candidate."},
                ],
                "paper_of_the_week": {"paper_id": "2609.00002", "reading_guide": _guide()},
            }
        )

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    result = skill.run(
        {"seen_papers": ["2609.00001v2"]},
        now=datetime(2026, 9, 7, 8, 0, tzinfo=ZoneInfo("America/Toronto")),
    )

    assert integration_calls[:2] == [
        {
            "operation": skill.LIST_PAPERS_OPERATION,
            "input": {"period": "2026-09", "sort": "trending", "limit": 15},
        },
        {
            "operation": skill.LIST_PAPERS_OPERATION,
            "input": {"period": "2026-08", "sort": "trending", "limit": 15},
        },
    ]
    assert [call["operation"] for call in integration_calls] == [
        skill.LIST_PAPERS_OPERATION,
        skill.LIST_PAPERS_OPERATION,
        skill.ATLAS_GOAL_OPERATION,
        skill.ATLAS_INTEREST_OPERATION,
        skill.GET_PAPER_OPERATION,
        skill.GET_PAPER_OPERATION,
    ]
    assert [call["input"] for call in integration_calls[-2:]] == [
        {"paper_id": "2608.00003", "include_content": True},
        {"paper_id": "2609.00002", "include_content": True},
    ]
    assert codex_calls[0]["context"] == {
        "candidate_papers": [
            {
                "paper_id": "2609.00002",
                "title": "Paper 2609.00002",
                "authors": ["Ada Researcher", "Grace Scientist"],
                "abstract": "Abstract for 2609.00002.",
            },
            {
                "paper_id": "2608.00003",
                "title": "Paper 2608.00003",
                "authors": ["Ada Researcher", "Grace Scientist"],
                "abstract": "Abstract for 2608.00003.",
            },
        ],
        "atlas_goals": [{"title": "Learn model evaluation"}],
        "atlas_interests": {"hobbies": [{"title": "AI research"}], "preferences": []},
    }
    assert codex_calls[0]["response_schema"] == skill.SELECTION_RESPONSE_SCHEMA
    assert codex_calls[1]["response_schema"] == skill.ANALYSIS_RESPONSE_SCHEMA
    assert codex_calls[1]["context"]["atlas_goals"] == [{"title": "Learn model evaluation"}]
    assert codex_calls[1]["context"]["atlas_interests"] == {
        "hobbies": [{"title": "AI research"}],
        "preferences": [],
    }
    assert all(call["internet_access"] is False for call in codex_calls)
    assert result["seen_papers"] == ["2609.00002", "2608.00003"]
    assert [paper["paper_id"] for paper in result["selected_papers"]] == ["2609.00002", "2608.00003"]
    assert [paper["rank"] for paper in result["selected_papers"]] == [1, 2]
    assert result["selected_papers"][0]["upvotes"] == 80
    assert result["selected_papers"][0]["analysis"] == "The strongest current candidate."
    assert result["selected_papers"][0]["content_source"] == "arxiv_html"
    assert result["selected_papers"][0]["source_content_truncated"] is False
    assert result["paper_of_the_week"] == {
        "paper_id": "2609.00002",
        "title": "Paper 2609.00002",
        "reading_guide": _guide(),
    }


def test_empty_candidates_skip_atlas_codex_and_full_paper_fetch(monkeypatch):
    calls = []

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == 30
        calls.append((operation, input))
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary("2601.00001")]
        raise AssertionError("No contextual or content fetch should run")

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration_call)
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: pytest.fail("Codex must not run without candidates"),
    )

    assert skill.run(
        {"seen_papers": ["2601.00001"]},
        now=datetime(2026, 1, 2, tzinfo=ZoneInfo("America/Toronto")),
    ) == {"selected_papers": [], "paper_of_the_week": None, "seen_papers": []}
    assert calls == [
        (
            skill.LIST_PAPERS_OPERATION,
            {"period": "2026-01", "sort": "trending", "limit": 15},
        ),
        (
            skill.LIST_PAPERS_OPERATION,
            {"period": "2025-12", "sort": "trending", "limit": 15},
        ),
    ]


def test_zero_selection_returns_new_history_without_full_fetch_or_second_codex(monkeypatch):
    integration_calls = []

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == 30
        integration_calls.append(operation)
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary("2609.00001")] if len(integration_calls) == 1 else []
        if operation == skill.ATLAS_GOAL_OPERATION:
            return {"goals": [], "progressions": []}
        if operation == skill.ATLAS_INTEREST_OPERATION:
            return {"hobbies": [], "preferences": []}
        raise AssertionError("Full paper fetch must not run for an empty selection")

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration_call)
    codex_calls = []
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **kwargs: codex_calls.append(kwargs) or _codex_response({"paper_ids": []}),
    )

    assert skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto"))) == {
        "selected_papers": [],
        "paper_of_the_week": None,
        "seen_papers": [],
    }
    assert len(codex_calls) == 1
    assert skill.GET_PAPER_OPERATION not in integration_calls


def test_seen_history_contains_selected_papers_only(monkeypatch):
    paper_ids = [f"2609.{index:05d}" for index in range(1, 4)]
    list_calls = 0

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        nonlocal list_calls
        if operation == skill.LIST_PAPERS_OPERATION:
            list_calls += 1
            return [_summary(paper_id) for paper_id in paper_ids] if list_calls == 1 else []
        if operation == skill.ATLAS_GOAL_OPERATION:
            return {"goals": [], "progressions": []}
        if operation == skill.ATLAS_INTEREST_OPERATION:
            return {"hobbies": [], "preferences": []}
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration_call)
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        if len(codex_calls) == 1:
            return _codex_response({"paper_ids": [paper_ids[1]]})
        return _codex_response(
            {
                "papers": [{"paper_id": paper_ids[1], "rank": 1, "analysis": "Worth reading."}],
                "paper_of_the_week": {"paper_id": paper_ids[1], "reading_guide": _guide()},
            }
        )

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    result = skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))

    assert [paper["paper_id"] for paper in result["selected_papers"]] == [paper_ids[1]]
    assert result["seen_papers"] == [paper_ids[1]]


def test_full_content_is_bounded_and_both_truncation_signals_are_retained(monkeypatch):
    paper_id = "2609.00001"
    content = "A" * (skill.MAX_CONTENT_PER_PAPER + 100)
    call_count = 0

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == (100 if operation == skill.GET_PAPER_OPERATION else 30)
        nonlocal call_count
        if operation == skill.LIST_PAPERS_OPERATION:
            call_count += 1
            return [_summary(paper_id)] if call_count == 1 else []
        if operation == skill.ATLAS_GOAL_OPERATION:
            return {"goals": [], "progressions": []}
        if operation == skill.ATLAS_INTEREST_OPERATION:
            return {"hobbies": [], "preferences": []}
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(paper_id, content=content, truncated=False)
        raise AssertionError(operation)

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration_call)
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        if len(codex_calls) == 1:
            return _codex_response({"paper_ids": [paper_id]})
        return _codex_response(
            {
                "papers": [{"paper_id": paper_id, "rank": 1, "analysis": "A grounded explanation."}],
                "paper_of_the_week": {"paper_id": paper_id, "reading_guide": _guide()},
            }
        )

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    result = skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))

    analyzed = codex_calls[1]["context"]["selected_papers"][0]
    assert len(analyzed["content"]) == skill.MAX_CONTENT_PER_PAPER
    assert "Middle of paper omitted" in analyzed["content"]
    assert analyzed["source_content_truncated"] is False
    assert analyzed["analysis_content_truncated"] is True
    assert result["selected_papers"][0]["source_content_truncated"] is False
    assert result["selected_papers"][0]["analysis_content_truncated"] is True


@pytest.mark.parametrize(
    "first_response, second_response, match",
    [
        ({"paper_ids": ["2609.99999"]}, None, "unknown or duplicate"),
        (
            {"paper_ids": ["2609.00001", "2609.00002"]},
            {
                "papers": [
                    {"paper_id": "2609.00001", "rank": 1, "analysis": "First."},
                    {"paper_id": "2609.00002", "rank": 1, "analysis": "Second."},
                ],
                "paper_of_the_week": {"paper_id": "2609.00001", "reading_guide": _guide()},
            },
            "ranks",
        ),
        (
            {"paper_ids": ["2609.00001", "2609.00002"]},
            {
                "papers": [
                    {"paper_id": "2609.00001", "rank": 1, "analysis": "First."},
                    {"paper_id": "2609.00002", "rank": 2, "analysis": "Second."},
                ],
                "paper_of_the_week": {"paper_id": "2609.99999", "reading_guide": _guide()},
            },
            "unknown paper_of_the_week",
        ),
        (
            {"paper_ids": ["2609.00001", "2609.00002"]},
            {
                "papers": [
                    {"paper_id": "2609.00001", "rank": 1, "analysis": "First."},
                    {"paper_id": "2609.00002", "rank": 2, "analysis": "Second."},
                ],
                "paper_of_the_week": {"paper_id": "2609.00002", "reading_guide": _guide()},
            },
            "rank-1 paper",
        ),
    ],
)
def test_rejects_hallucinated_ids_duplicate_ranks_and_unknown_winner(
    monkeypatch, first_response, second_response, match
):
    papers = [_summary("2609.00001"), _summary("2609.00002")]
    list_calls = 0

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == (100 if operation == skill.GET_PAPER_OPERATION else 30)
        nonlocal list_calls
        if operation == skill.LIST_PAPERS_OPERATION:
            list_calls += 1
            return papers if list_calls == 1 else []
        if operation == skill.ATLAS_GOAL_OPERATION:
            return {"goals": [], "progressions": []}
        if operation == skill.ATLAS_INTEREST_OPERATION:
            return {"hobbies": [], "preferences": []}
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration_call)
    responses = [_codex_response(first_response)]
    if second_response is not None:
        responses.append(_codex_response(second_response))
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", lambda **_kwargs: responses.pop(0))

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


def test_missing_full_content_fails_before_detailed_analysis(monkeypatch):
    paper_id = "2609.00001"
    list_calls = 0

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        assert timeout_seconds == (100 if operation == skill.GET_PAPER_OPERATION else 30)
        nonlocal list_calls
        if operation == skill.LIST_PAPERS_OPERATION:
            list_calls += 1
            return [_summary(paper_id)] if list_calls == 1 else []
        if operation == skill.ATLAS_GOAL_OPERATION:
            return {"goals": [], "progressions": []}
        if operation == skill.ATLAS_INTEREST_OPERATION:
            return {"hobbies": [], "preferences": []}
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(paper_id, content=None)
        raise AssertionError(operation)

    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", integration_call)
    codex_calls = []
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: codex_calls.append(True) or _codex_response({"paper_ids": [paper_id]}),
    )

    with pytest.raises(ValueError, match="full paper content"):
        skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))
    assert codex_calls == [True]
