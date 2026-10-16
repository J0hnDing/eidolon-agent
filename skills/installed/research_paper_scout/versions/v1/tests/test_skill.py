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


def _atlas_result(operation: str):
    if operation == skill.ATLAS_GOAL_OPERATION:
        return {"goals": [{"title": "Build useful local AI tools"}]}
    if operation == skill.ATLAS_PROJECT_OPERATION:
        return {"projects": [{"title": "Research notebook"}]}
    if operation == skill.ATLAS_INTEREST_OPERATION:
        return {"hobbies": ["robotics"], "preferences": ["plain language"]}
    raise AssertionError(operation)


def test_selects_from_both_month_top_15_lists_and_filters_reported_candidates_before_codex(monkeypatch):
    current_ids = [f"2601.{index:05d}" for index in range(1, 16)]
    previous_ids = ["2601.00001v2", *[f"2512.{index:05d}" for index in range(16, 30)]]
    duplicate_summary = _summary(previous_ids[0], upvotes=80)
    duplicate_summary["title"] = _summary(current_ids[0])["title"]
    papers_by_period = {
        "2026-01": [_summary(paper_id, upvotes=100 - index) for index, paper_id in enumerate(current_ids)],
        "2025-12": [duplicate_summary, *[_summary(paper_id, upvotes=80 - index) for index, paper_id in enumerate(previous_ids[1:], start=1)]],
    }
    selected_indices = [10, 0, 26]
    selected_ids = [current_ids[11], current_ids[0], previous_ids[-1]]
    integration_calls = []

    def integration_call(*, operation, input, timeout_seconds=30):  # noqa: A002
        integration_calls.append((operation, input, timeout_seconds))
        if operation == skill.LIST_PAPERS_OPERATION:
            assert input["sort"] == "upvotes"
            assert input["limit"] == 15
            assert "excluded_paper_ids" not in input
            return papers_by_period[input["period"]]
        if operation in {
            skill.ATLAS_GOAL_OPERATION,
            skill.ATLAS_PROJECT_OPERATION,
            skill.ATLAS_INTEREST_OPERATION,
        }:
            return _atlas_result(operation)
        if operation == skill.GET_PAPER_OPERATION:
            assert timeout_seconds == 100
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        if len(codex_calls) == 1:
            return _codex_response({"selected_indices": selected_indices})
        return _codex_response(
            {
                "papers": [
                    {"paper_id": paper_id, "analysis": f"Analysis for {paper_id}."}
                    for paper_id in reversed(selected_ids)
                ]
            }
        )

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    result = skill.run(
        {"excluded_papers": ["2601.00005v2", "2512.00028"]},
        now=datetime(2026, 1, 7, 8, 0, tzinfo=ZoneInfo("America/Toronto")),
    )

    paper_list_calls = [
        (operation, value)
        for operation, value, _timeout in integration_calls
        if operation == skill.LIST_PAPERS_OPERATION
    ]
    assert paper_list_calls == [
        (
            skill.LIST_PAPERS_OPERATION,
            {"period": "2026-01", "sort": "upvotes", "limit": 15},
        ),
        (
            skill.LIST_PAPERS_OPERATION,
            {"period": "2025-12", "sort": "upvotes", "limit": 15},
        ),
    ]
    atlas_calls = [
        (operation, value)
        for operation, value, _timeout in integration_calls
        if operation in {
            skill.ATLAS_GOAL_OPERATION,
            skill.ATLAS_PROJECT_OPERATION,
            skill.ATLAS_INTEREST_OPERATION,
        }
    ]
    assert atlas_calls == [
        (skill.ATLAS_GOAL_OPERATION, {"limit": 100}),
        (skill.ATLAS_PROJECT_OPERATION, {"limit": 100}),
        (skill.ATLAS_INTEREST_OPERATION, {}),
    ]
    selection_context = codex_calls[0]["context"]
    assert len(selection_context["papers"]) == 27
    assert selection_context["papers"][0] == {"index": 0, "title": "Paper 2601.00001"}
    assert selection_context["papers"][13] == {"index": 13, "title": "Paper 2601.00015"}
    assert selection_context["papers"][14] == {"index": 14, "title": "Paper 2512.00016"}
    assert selection_context["papers"][-1] == {"index": 26, "title": "Paper 2512.00029"}
    candidate_titles = [paper["title"] for paper in selection_context["papers"]]
    assert "Paper 2601.00005" not in candidate_titles
    assert "Paper 2512.00028" not in candidate_titles
    assert candidate_titles.count("Paper 2601.00001") == 1
    assert all(set(paper) == {"index", "title"} for paper in selection_context["papers"])
    assert selection_context["atlas_goals"] == [{"title": "Build useful local AI tools"}]
    assert selection_context["atlas_projects"] == [{"title": "Research notebook"}]
    assert selection_context["atlas_interests"] == {
        "hobbies": ["robotics"],
        "preferences": ["plain language"],
    }
    selection_schema = codex_calls[0]["response_schema"]["properties"]["selected_indices"]
    assert selection_schema["maxItems"] == 3
    assert selection_schema["items"]["maximum"] == 26
    assert codex_calls[0]["internet_access"] is False
    assert "paper titles" in codex_calls[0]["prompt"]

    fetched_ids = [
        value["paper_id"]
        for operation, value, _timeout in integration_calls
        if operation == skill.GET_PAPER_OPERATION
    ]
    assert fetched_ids == selected_ids
    assert len(codex_calls) == 2
    assert [paper["paper_id"] for paper in codex_calls[1]["context"]["papers"]] == selected_ids
    assert all(paper["content"] == "Full paper text" for paper in codex_calls[1]["context"]["papers"])
    assert codex_calls[1]["internet_access"] is False
    assert result.keys() == {"selected_papers"}
    assert [paper["paper_id"] for paper in result["selected_papers"]] == selected_ids
    assert [paper["rank"] for paper in result["selected_papers"]] == [1, 2, 3]
    assert result["selected_papers"][0]["analysis"] == f"Analysis for {selected_ids[0]}."


def test_no_monthly_candidates_skips_atlas_and_both_codex_stages(monkeypatch):
    calls = []

    def integration_call(*, operation, input, **_kwargs):  # noqa: A002
        calls.append((operation, input))
        if operation == skill.LIST_PAPERS_OPERATION:
            return []
        raise AssertionError("Atlas or full-paper fetch should not run without candidates")

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: pytest.fail("Codex must not run without candidates"),
    )

    assert skill.run(
        {}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto"))
    ) == {"selected_papers": []}
    assert calls == [
        (
            skill.LIST_PAPERS_OPERATION,
            {"period": "2026-09", "sort": "upvotes", "limit": 15},
        ),
        (
            skill.LIST_PAPERS_OPERATION,
            {"period": "2026-08", "sort": "upvotes", "limit": 15},
        ),
    ]


def test_no_selection_skips_full_paper_fetch_and_analysis(monkeypatch):
    def integration_call(*, operation, input, **_kwargs):  # noqa: A002
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary("2609.00001")] if input["period"] == "2026-09" else []
        if operation in {
            skill.ATLAS_GOAL_OPERATION,
            skill.ATLAS_PROJECT_OPERATION,
            skill.ATLAS_INTEREST_OPERATION,
        }:
            return _atlas_result(operation)
        pytest.fail("No full-paper fetch should run after an empty selection")

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    codex_calls = []
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **kwargs: codex_calls.append(kwargs)
        or _codex_response({"selected_indices": []}),
    )

    assert skill.run(
        {}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto"))
    ) == {"selected_papers": []}
    assert len(codex_calls) == 1


def test_unreported_candidates_survive_while_reported_and_duplicate_ids_are_filtered_before_codex(monkeypatch):
    first = _summary("2609.00001")
    duplicate = {**_summary("2609.00001v2"), "title": first["title"]}
    second = _summary("2609.00002")
    third = _summary("2608.00003")
    full_fetches = []

    def integration_call(*, operation, input, **_kwargs):  # noqa: A002
        if operation == skill.LIST_PAPERS_OPERATION:
            return [first, second] if input["period"] == "2026-09" else [duplicate, third]
        if operation in {
            skill.ATLAS_GOAL_OPERATION,
            skill.ATLAS_PROJECT_OPERATION,
            skill.ATLAS_INTEREST_OPERATION,
        }:
            return _atlas_result(operation)
        if operation == skill.GET_PAPER_OPERATION:
            full_fetches.append(input["paper_id"])
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        if len(codex_calls) == 1:
            assert [paper["title"] for paper in kwargs["context"]["papers"]] == [
                first["title"],
                third["title"],
            ]
            return _codex_response({"selected_indices": [1, 0]})
        return _codex_response(
            {
                "papers": [
                    {"paper_id": "2608.00003", "analysis": "Third paper."},
                    {"paper_id": "2609.00001", "analysis": "Grounded."},
                ]
            }
        )

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    result = skill.run(
        {"excluded_papers": ["2609.00002v3"]},
        now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")),
    )

    assert full_fetches == ["2608.00003", "2609.00001"]
    assert len(codex_calls) == 2
    assert [paper["paper_id"] for paper in result["selected_papers"]] == [
        "2608.00003",
        "2609.00001",
    ]


def test_full_content_is_bounded_for_selected_papers_and_truncation_is_retained(
    monkeypatch,
):
    paper_id = "2609.00001"
    content = "A" * (skill.MAX_CONTENT_PER_PAPER + 100)

    def integration_call(*, operation, input, **_kwargs):  # noqa: A002
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary(paper_id)] if input["period"] == "2026-09" else []
        if operation in {
            skill.ATLAS_GOAL_OPERATION,
            skill.ATLAS_PROJECT_OPERATION,
            skill.ATLAS_INTEREST_OPERATION,
        }:
            return _atlas_result(operation)
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(paper_id, content=content)
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        if len(codex_calls) == 1:
            return _codex_response({"selected_indices": [0]})
        return _codex_response(
            {"papers": [{"paper_id": paper_id, "analysis": "Grounded analysis."}]}
        )

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    result = skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))

    analyzed = codex_calls[1]["context"]["papers"][0]
    assert len(analyzed["content"]) == skill.MAX_CONTENT_PER_PAPER
    assert "Middle of paper omitted" in analyzed["content"]
    assert analyzed["source_content_truncated"] is False
    assert analyzed["analysis_content_truncated"] is True
    assert result["selected_papers"][0]["analysis_content_truncated"] is True


@pytest.mark.parametrize(
    "response, candidate_count",
    [
        ({"selected_indices": [3]}, 3),
        ({"selected_indices": [0, 0]}, 2),
        ({"selected_indices": [True]}, 1),
        ({"selected_indices": [0, 1, 2, 3]}, 4),
        ({"selected_indices": [0], "extra": True}, 1),
    ],
)
def test_rejects_invalid_selection(response, candidate_count):
    with pytest.raises(ValueError):
        skill._parse_selection(json.dumps(response), candidate_count)


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
            return [_summary(paper_id) for paper_id in paper_ids] if input["period"] == "2026-09" else []
        if operation in {
            skill.ATLAS_GOAL_OPERATION,
            skill.ATLAS_PROJECT_OPERATION,
            skill.ATLAS_INTEREST_OPERATION,
        }:
            return _atlas_result(operation)
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(input["paper_id"])
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    responses = [
        _codex_response({"selected_indices": list(range(len(paper_ids)))}),
        _codex_response(response),
    ]
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_codex",
        lambda **_kwargs: responses.pop(0),
    )

    with pytest.raises(ValueError, match=match):
        skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": True},
        {"seen_papers": ["2609.00001"]},
        {"excluded_papers": "2609.00001"},
        {"excluded_papers": ["not-an-arxiv-id"]},
    ],
)
def test_rejects_nonempty_input_before_provider_calls(monkeypatch, payload):
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        lambda **_kwargs: pytest.fail("Provider must not run for invalid input"),
    )
    with pytest.raises(ValueError):
        skill.run(payload)


def test_missing_full_content_fails_before_analysis_call(monkeypatch):
    paper_id = "2609.00001"

    def integration_call(*, operation, input, **_kwargs):  # noqa: A002
        if operation == skill.LIST_PAPERS_OPERATION:
            return [_summary(paper_id)] if input["period"] == "2026-09" else []
        if operation in {
            skill.ATLAS_GOAL_OPERATION,
            skill.ATLAS_PROJECT_OPERATION,
            skill.ATLAS_INTEREST_OPERATION,
        }:
            return _atlas_result(operation)
        if operation == skill.GET_PAPER_OPERATION:
            return _paper(paper_id, content=None)
        raise AssertionError(operation)

    monkeypatch.setattr(
        skill.integration_runtime_capabilities, "call", integration_call
    )
    codex_calls = []

    def codex_call(**kwargs):
        codex_calls.append(kwargs)
        return _codex_response({"selected_indices": [0]})

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_codex", codex_call)

    with pytest.raises(ValueError, match="full paper content"):
        skill.run({}, now=datetime(2026, 9, 7, tzinfo=ZoneInfo("America/Toronto")))
    assert len(codex_calls) == 1


def test_selection_and_analysis_prompts_have_distinct_roles():
    selection_prompt = skill._selection_prompt()
    analysis_prompt = skill._analysis_prompt()

    assert "zero to three papers" in selection_prompt
    assert "paper titles" in selection_prompt
    assert "Atlas goals, projects, and interests" in selection_prompt
    assert "most to least interesting" in selection_prompt
    assert "Analyze every supplied selected paper" in analysis_prompt
    assert "Do not select additional papers, omit papers, or reorder them" in analysis_prompt
    assert "exactly two short paragraphs" in analysis_prompt
    assert "simple everyday words" in analysis_prompt
    assert "Do not invent sizes" in analysis_prompt
    assert "excluded_papers" not in selection_prompt


def test_monthly_papers_rejects_provider_results_over_per_month_limit():
    papers = [_summary(f"2609.{index:05d}") for index in range(1, 17)]

    with pytest.raises(ValueError, match="invalid monthly paper list"):
        skill._monthly_papers(papers)


def test_previous_month_handles_year_boundary():
    assert skill._current_and_previous_months(
        datetime(2026, 1, 1, tzinfo=ZoneInfo("America/Toronto"))
    ) == ("2026-01", "2025-12")
