"""Exercise the user-owned scout/service interface with real package schemas."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def _package(name, folder):
    spec = importlib.util.spec_from_file_location(name, folder / "skill.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    return module, manifest


@pytest.mark.parametrize("selected", [False, True])
def test_scout_output_flows_into_weekly_report_without_schema_drift(tmp_path, monkeypatch, selected):
    scout_folder = ROOT / "skills/proposed/research_paper_scout"
    if not scout_folder.exists():
        scout_folder = ROOT / "skills/installed/research_paper_scout/versions/v1"
    scout, scout_manifest = _package("research_contract_scout", scout_folder)
    service, service_manifest = _package(
        "research_contract_service", ROOT / "skills/installed/weekly_report_service/versions/v2",
    )
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    paper = {
        "paper_id": "2609.00001",
        "title": "A" * 2000,
        "authors": [],
        "abstract": "A study of a useful method.",
        "url": "https://huggingface.co/papers/2609.00001",
        "pdf_url": "https://arxiv.org/pdf/2609.00001",
        "published_at": None,
        "upvotes": 10,
    }
    guide = {
        "problem": "Problem " * 500,
        "core_idea": "Core idea",
        "method": "Method",
        "novelty": "Novelty",
        "important_results": "Results",
        "limitations": "Limitations",
        "prerequisites": [],
        "recommended_reading_order": ["Introduction", "Method"],
        "sections_to_skip_initially": [],
        "key_questions": ["What evidence supports the claims?"],
        "expected_takeaways": ["Understand the method."],
    }
    responses = [{"paper_ids": [paper["paper_id"]] if selected else []}]
    if selected:
        responses.append({
            "papers": [{"paper_id": paper["paper_id"], "rank": 1, "analysis": "A useful analysis."}],
            "paper_of_the_week": {"paper_id": paper["paper_id"], "reading_guide": guide},
        })

    def integration(*, operation, input, **kwargs):
        if operation == "huggingface.list_papers":
            return [paper]
        if operation == "atlas.goal.list":
            return {"goals": []}
        if operation == "atlas.interest.list":
            return {"interests": []}
        if operation == "huggingface.get_paper":
            return {**paper, "content": "Full original paper text. " * 100,
                    "content_source": "arxiv_html", "content_truncated": False}
        raise AssertionError(operation)

    def codex(**kwargs):
        response = responses.pop(0)
        Draft202012Validator(kwargs["response_schema"]).validate(response)
        return {"response": json.dumps(response)}

    monkeypatch.setattr(scout.integration_runtime_capabilities, "call", integration)
    monkeypatch.setattr(scout.function_runtime_capabilities, "call_codex", codex)
    output = scout.run({}, now=datetime(2026, 9, 7, 12, tzinfo=UTC))
    Draft202012Validator(scout_manifest["output_schema"]).validate(output)
    assert responses == []
    assert output["seen_papers"] == ([paper["paper_id"]] if selected else [])
    reports = []

    def create_report(*, operation, input):
        if operation == "telegram.notification.send":
            return {"sent": True, "message_id": 1}
        assert operation == "notion.report.create"
        reports.append(input)
        return {"id": str(len(reports)), "name": input["name"], "select": input["select"],
                "created_time": "2026-09-07T12:00:00Z"}

    def child(name, payload):
        if name == "github_repo_scout":
            return {"repositories": [], "seen_repo": []}
        assert name == "research_paper_scout"
        Draft202012Validator(scout_manifest["input_schema"]).validate(payload)
        return output

    monkeypatch.setattr(service.integration_runtime_capabilities, "call", create_report)
    monkeypatch.setattr(service.function_runtime_capabilities, "call_function", child)
    result = service.run({}, now=datetime(2026, 9, 7, 12, tzinfo=UTC))
    Draft202012Validator(service_manifest["output_schema"]).validate(result)
    assert result["paper_count"] == int(selected)
    assert [report["select"] for report in reports] == ["GitHub Projects", "AI Research"]
    assert len(reports[1]["children"]) <= 100
    seen_path = tmp_path / "seen_papers.json"
    assert (json.loads(seen_path.read_text()) if seen_path.exists() else []) == (
        [paper["paper_id"]] if selected else []
    )
