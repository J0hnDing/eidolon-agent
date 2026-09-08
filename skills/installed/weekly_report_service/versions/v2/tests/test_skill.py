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


@pytest.fixture(autouse=True)
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    return tmp_path


def _repository(**overrides):
    value = {
        "name": "example/project",
        "url": "https://github.com/example/project",
        "stars": 1234,
        "description": "A useful project.",
        "analysis": "A concise grounded analysis with a useful local lesson.",
    }
    value.update(overrides)
    return value


def _paper(rank: int = 1, paper_id: str = "2608.12345", **overrides):
    value = {
        "rank": rank,
        "paper_id": paper_id,
        "title": f"Useful paper {rank}",
        "authors": ["Ada Example", "Grace Example"],
        "abstract": "A clear abstract.",
        "url": f"https://huggingface.co/papers/{paper_id}",
        "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
        "published_at": "2026-08-20T00:00:00Z",
        "upvotes": 42,
        "analysis": "This paper is useful because it provides a concrete new method.",
        "content_source": "arxiv_html",
        "source_content_truncated": False,
        "analysis_content_truncated": False,
    }
    value.update(overrides)
    return value


def _guide(**overrides):
    value = {
        "problem": "The problem.",
        "core_idea": "The core idea.",
        "method": "The method.",
        "novelty": "The novelty.",
        "important_results": "The important results.",
        "limitations": "The limitations.",
        "prerequisites": ["Linear algebra"],
        "recommended_reading_order": ["Abstract", "Method", "Results"],
        "sections_to_skip_initially": ["Appendix B"],
        "key_questions": ["Does the evidence support the main claim?"],
        "expected_takeaways": ["Know when to use the method."],
    }
    value.update(overrides)
    return value


def _paper_output(*, papers=None, seen=None, paper_of_the_week="default"):
    papers = [_paper()] if papers is None else papers
    if seen is None:
        seen = [paper["paper_id"] for paper in papers]
    if paper_of_the_week == "default":
        paper_of_the_week = (
            {
                "paper_id": papers[0]["paper_id"],
                "title": papers[0]["title"],
                "reading_guide": _guide(),
            }
            if papers
            else None
        )
    return {
        "selected_papers": papers,
        "paper_of_the_week": paper_of_the_week,
        "seen_papers": seen,
    }


def _created_report(name: str, select: str):
    return {
        "id": f"{select.lower().replace(' ', '-')}-report-id",
        "name": name,
        "created_time": "2026-08-31T12:00:00Z",
        "select": select,
    }


def _successful_integrations(calls):
    def invoke(**kwargs):
        calls.append(kwargs)
        if kwargs["operation"] == skill.REPORT_CREATE_OPERATION:
            value = kwargs["input"]
            return _created_report(value["name"], value["select"])
        return {"sent": True, "message_id": 1}

    return invoke


def test_run_creates_both_reports_advances_separate_histories_and_notifies(cache_dir, monkeypatch):
    (cache_dir / skill.SEEN_REPOSITORIES_FILENAME).write_text(
        json.dumps(["old/repository"]), encoding="utf-8"
    )
    (cache_dir / skill.SEEN_PAPERS_FILENAME).write_text(json.dumps(["2501.00001v2"]), encoding="utf-8")
    function_calls = []

    def call_function(name, input_json):
        function_calls.append((name, input_json))
        if name == skill.SCOUT_FUNCTION:
            return {
                "repositories": [_repository()],
                "seen_repo": ["example/project", "example/other"],
            }
        return _paper_output(seen=["2608.12345"])

    integration_calls = []
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_function", call_function)
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        _successful_integrations(integration_calls),
    )

    result = skill.run({}, now=datetime(2026, 8, 31, 8, 0, tzinfo=ZoneInfo("America/Toronto")))

    assert function_calls == [
        (
            skill.SCOUT_FUNCTION,
            {"limit": 25, "period": "weekly", "excluded_repo": ["old/repository"]},
        ),
        (skill.PAPER_SCOUT_FUNCTION, {"seen_papers": ["2501.00001v2"]}),
    ]
    github_create, research_create, notification = integration_calls
    assert github_create["input"]["select"] == "GitHub Projects"
    assert research_create["input"]["select"] == "AI Research"
    research_blocks = research_create["input"]["children"]
    headings = [
        block[block["type"]]["rich_text"][0]["text"]["content"]
        for block in research_blocks
        if block["type"].startswith("heading_")
    ]
    assert "#1 — Useful paper 1" in headings
    assert "Detailed reading guide" in headings
    assert "Expected takeaways" in headings
    assert notification == {
        "operation": skill.NOTIFICATION_OPERATION,
        "input": {
            "title": "Your weekly reports are ready",
            "description": (
                "• Weekly GitHub Projects Report — 2026-08-31\n"
                "• Weekly AI Research Report — 2026-08-31"
            ),
            "alert": False,
        },
    }
    assert result == {
        "report": _created_report("Weekly GitHub Projects Report — 2026-08-31", "GitHub Projects"),
        "repository_count": 1,
        "research_report": _created_report("Weekly AI Research Report — 2026-08-31", "AI Research"),
        "paper_count": 1,
    }
    assert json.loads((cache_dir / skill.SEEN_REPOSITORIES_FILENAME).read_text(encoding="utf-8")) == [
        "old/repository",
        "example/project",
        "example/other",
    ]
    assert json.loads((cache_dir / skill.SEEN_PAPERS_FILENAME).read_text(encoding="utf-8")) == [
        "2501.00001v2",
        "2608.12345",
    ]


def test_empty_scout_results_create_explicit_reports_without_cache_files(cache_dir, monkeypatch):
    def call_function(name, _input):
        if name == skill.SCOUT_FUNCTION:
            return {"repositories": [], "seen_repo": []}
        return _paper_output(papers=[], seen=[])

    integration_calls = []
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_function", call_function)
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        _successful_integrations(integration_calls),
    )

    result = skill.run({}, now=datetime(2026, 8, 31, tzinfo=ZoneInfo("America/Toronto")))

    assert result["repository_count"] == 0
    assert result["paper_count"] == 0
    assert "no repositories" in integration_calls[0]["input"]["children"][-1]["paragraph"]["rich_text"][0][
        "text"
    ]["content"]
    assert "no papers valuable enough" in integration_calls[1]["input"]["children"][-1]["paragraph"][
        "rich_text"
    ][0]["text"]["content"]
    assert not (cache_dir / skill.SEEN_REPOSITORIES_FILENAME).exists()
    assert not (cache_dir / skill.SEEN_PAPERS_FILENAME).exists()


@pytest.mark.parametrize(
    "output",
    [
        {},
        _paper_output(papers=[_paper(rank=2)]),
        _paper_output(papers=[_paper()], seen=[]),
        _paper_output(papers=[_paper()], seen=["2608.12345", "2608.12345"]),
        _paper_output(
            papers=[_paper()],
            paper_of_the_week={
                "paper_id": "other",
                "title": "Other",
                "reading_guide": _guide(),
            },
        ),
        _paper_output(papers=[_paper(upvotes=True)]),
        _paper_output(papers=[_paper(content_source="pdf")]),
    ],
)
def test_invalid_paper_output_fails_before_research_report(monkeypatch, output):
    def call_function(name, _input):
        if name == skill.SCOUT_FUNCTION:
            return {"repositories": [], "seen_repo": []}
        return output

    integration_calls = []
    monkeypatch.setattr(skill.function_runtime_capabilities, "call_function", call_function)
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        _successful_integrations(integration_calls),
    )

    with pytest.raises(ValueError):
        skill.run({})

    assert [call["operation"] for call in integration_calls] == [
        skill.REPORT_CREATE_OPERATION,
        skill.NOTIFICATION_OPERATION,
    ]
    assert integration_calls[-1]["input"]["description"].startswith("paper_scout_invalid_output:")


def test_research_notion_failure_keeps_github_history_but_not_paper_history(cache_dir, monkeypatch):
    def call_function(name, _input):
        if name == skill.SCOUT_FUNCTION:
            return {"repositories": [_repository()], "seen_repo": ["example/project"]}
        return _paper_output()

    integration_calls = []

    def invoke(**kwargs):
        integration_calls.append(kwargs)
        if kwargs["operation"] == skill.REPORT_CREATE_OPERATION:
            value = kwargs["input"]
            if value["select"] == skill.RESEARCH_REPORT_SELECT:
                raise skill.integration_runtime_capabilities.IntegrationRuntimeCapabilityError(
                    "rate_limited", "Notion asked Eidolon to retry later"
                )
            return _created_report(value["name"], value["select"])
        return {"sent": True, "message_id": 1}

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_function", call_function)
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", invoke)

    with pytest.raises(RuntimeError, match="retry later"):
        skill.run({})

    assert json.loads((cache_dir / skill.SEEN_REPOSITORIES_FILENAME).read_text(encoding="utf-8")) == [
        "example/project"
    ]
    assert not (cache_dir / skill.SEEN_PAPERS_FILENAME).exists()
    assert integration_calls[-1]["input"] == {
        "title": "Weekly report failed",
        "description": "rate_limited: Notion asked Eidolon to retry later",
        "alert": True,
    }


def test_invalid_research_report_response_does_not_advance_paper_history(cache_dir, monkeypatch):
    def call_function(name, _input):
        if name == skill.SCOUT_FUNCTION:
            return {"repositories": [], "seen_repo": []}
        return _paper_output()

    def invoke(**kwargs):
        if kwargs["operation"] == skill.NOTIFICATION_OPERATION:
            return {"sent": True, "message_id": 1}
        value = kwargs["input"]
        if value["select"] == skill.RESEARCH_REPORT_SELECT:
            return _created_report(value["name"], "AI News")
        return _created_report(value["name"], value["select"])

    monkeypatch.setattr(skill.function_runtime_capabilities, "call_function", call_function)
    monkeypatch.setattr(skill.integration_runtime_capabilities, "call", invoke)

    with pytest.raises(ValueError, match="mismatched metadata"):
        skill.run({})

    assert not (cache_dir / skill.SEEN_PAPERS_FILENAME).exists()


def test_research_blocks_preserve_full_guide_with_bounded_notion_shape():
    papers = [
        _paper(
            rank=index,
            paper_id=f"2608.{index:05d}",
            abstract="a" * skill.MAX_ABSTRACT,
            analysis="b" * skill.MAX_ANALYSIS,
            source_content_truncated=True,
            analysis_content_truncated=True,
        )
        for index in range(1, 6)
    ]
    guide = _guide(
        **{field: "g" * skill.MAX_GUIDE_TEXT for field in skill.READING_GUIDE_SCALAR_FIELDS},
        **{
            field: ["x" * skill.MAX_NOTION_TEXT for _ in range(skill.MAX_GUIDE_ITEMS)]
            for field in skill.READING_GUIDE_LIST_FIELDS
        },
    )
    paper_of_the_week = {
        "paper_id": papers[0]["paper_id"],
        "title": papers[0]["title"],
        "reading_guide": guide,
    }

    blocks = skill._research_report_blocks(papers, paper_of_the_week, "2026-08-31")

    assert len(blocks) <= skill.MAX_NOTION_BLOCKS
    contents = []
    for block in blocks:
        body = block.get(block["type"], {})
        assert len(body.get("rich_text", [])) <= skill.MAX_NOTION_RICH_TEXT_ITEMS
        contents.extend(item["text"]["content"] for item in body.get("rich_text", []))
    assert all(len(content) <= skill.MAX_NOTION_TEXT for content in contents)
    problem_heading = next(
        index
        for index, block in enumerate(blocks)
        if block["type"] == "heading_3"
        and block["heading_3"]["rich_text"][0]["text"]["content"] == "Problem"
    )
    rendered_problem = "".join(
        item["text"]["content"] for item in blocks[problem_heading + 1]["paragraph"]["rich_text"]
    )
    assert rendered_problem == guide["problem"]


def test_service_rejects_input_and_has_no_direct_codex_call_surface(monkeypatch):
    assert not hasattr(skill, "call_codex")
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        lambda **_kwargs: {"sent": True, "message_id": 1},
    )
    with pytest.raises(ValueError, match="empty object"):
        skill.run({"unexpected": True})


def test_github_scout_failure_propagates_without_report_fallback(monkeypatch):
    monkeypatch.setattr(
        skill.function_runtime_capabilities,
        "call_function",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("scout failed")),
    )
    integration_calls = []
    monkeypatch.setattr(
        skill.integration_runtime_capabilities,
        "call",
        lambda **kwargs: integration_calls.append(kwargs) or {"sent": True, "message_id": 1},
    )

    with pytest.raises(RuntimeError, match="scout failed"):
        skill.run({})

    assert integration_calls == [
        {
            "operation": skill.NOTIFICATION_OPERATION,
            "input": {
                "title": "Weekly report failed",
                "description": "github_scout_failed: scout failed",
                "alert": True,
            },
        }
    ]
