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


@pytest.mark.parametrize("paper_count", [0, 1, 3])
def test_two_stage_scout_output_flows_into_weekly_report_without_schema_drift(
    tmp_path, monkeypatch, paper_count
):
    scout, scout_manifest = _package(
        "research_contract_scout",
        ROOT / "skills/installed/research_paper_scout/versions/v1",
    )
    service, service_manifest = _package(
        "research_contract_service",
        ROOT / "skills/installed/weekly_report_service/versions/v2",
    )
    monkeypatch.setenv("PERSONAL_AGENT_SKILL_CACHE_DIR", str(tmp_path))
    papers = [
        {
            "paper_id": f"2609.{index:05d}",
            "title": f"Paper title {index}",
            "authors": [],
            "abstract": "A study of a useful method.",
            "url": f"https://huggingface.co/papers/2609.{index:05d}",
            "pdf_url": f"https://arxiv.org/pdf/2609.{index:05d}",
            "published_at": None,
            "organization": "Example Research",
            "upvotes": 10 - index,
        }
        for index in range(1, paper_count + 1)
    ]
    paper_ids = [paper["paper_id"] for paper in papers]
    responses = []
    if paper_count:
        responses.extend(
            [
                {"selected_indices": list(range(paper_count))},
                {
                    "papers": [
                        {"paper_id": paper_id, "analysis": "A useful analysis."}
                        for paper_id in paper_ids
                    ]
                },
            ]
        )

    def integration(*, operation, input, **kwargs):
        if operation == "huggingface.list_papers":
            assert input["limit"] == 15
            assert "excluded_paper_ids" not in input
            return papers if input["period"] == "2026-09" else []
        if operation == "huggingface.get_paper":
            paper = next(paper for paper in papers if paper["paper_id"] == input["paper_id"])
            return {
                **paper,
                "content": "Full original paper text. " * 100,
                "content_source": "arxiv_html",
                "content_truncated": False,
            }
        if operation == "atlas.goal.list":
            return {"goals": [], "graph": {"nodes": [], "edges": []}}
        if operation == "atlas.project.list":
            return {"projects": []}
        if operation == "atlas.interest.list":
            return {"interests": []}
        raise AssertionError(operation)

    def codex(**kwargs):
        response = responses.pop(0)
        Draft202012Validator(kwargs["response_schema"]).validate(response)
        return {"response": json.dumps(response)}

    monkeypatch.setattr(scout.integration_runtime_capabilities, "call", integration)
    monkeypatch.setattr(scout.function_runtime_capabilities, "call_codex", codex)
    output = scout.run({"excluded_papers": []}, now=datetime(2026, 9, 7, 12, tzinfo=UTC))
    Draft202012Validator(scout_manifest["output_schema"]).validate(output)
    assert responses == []
    assert set(output) == {"selected_papers"}
    assert len(output["selected_papers"]) <= 3
    assert scout_manifest["input_schema"]["properties"]["excluded_papers"]["maxItems"] == 5000
    assert scout_manifest["integration_requirements"][1]["provider"] == "atlas"
    reports = []

    def create_report(*, operation, input):
        if operation == "telegram.notification.send":
            return {"sent": True, "message_id": 1}
        assert operation == "notion.report.create"
        reports.append(input)
        return {
            "id": str(len(reports)),
            "name": input["name"],
            "select": input["select"],
            "created_time": "2026-09-07T12:00:00Z",
        }

    def child(name, payload):
        if name == "github_repo_scout":
            return {"repositories": [], "seen_repo": []}
        if name == "macro_geopolitical_news_scout":
            return {
                "report": {"macro_picture": "", "items": [], "upcoming_catalysts": []},
                "seen": [],
                "candidate_count": 0,
                "source_failures": [],
                "last_fetch_at": "2026-09-07T12:00:00+00:00",
            }
        assert name == "research_paper_scout"
        Draft202012Validator(scout_manifest["input_schema"]).validate(payload)
        assert payload == {"excluded_papers": ["2501.00001v2"]}
        return output

    monkeypatch.setattr(service.integration_runtime_capabilities, "call", create_report)
    monkeypatch.setattr(service.function_runtime_capabilities, "call_function", child)
    (tmp_path / "seen_papers.json").write_text(
        json.dumps(["2501.00001v2"]), encoding="utf-8"
    )
    result = service.run({}, now=datetime(2026, 9, 7, 12, tzinfo=UTC))
    Draft202012Validator(service_manifest["output_schema"]).validate(result)
    assert result["paper_count"] == paper_count
    assert service_manifest["output_schema"]["properties"]["paper_count"]["maximum"] == 3
    assert [report["select"] for report in reports] == ["GitHub Projects", "AI Research", "Macro"]
    assert len(reports[1]["children"]) <= 100
    seen_path = tmp_path / "seen_papers.json"
    assert json.loads(seen_path.read_text(encoding="utf-8")) == [
        "2501.00001v2",
        *paper_ids,
    ]
