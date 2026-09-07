import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.atlas_knowledge_service import AtlasKnowledgeError, AtlasKnowledgeService, codex_available
from app.services.atlas_provider import FakeAtlasProviderAdapter, UrllibAtlasProviderAdapter
from app.services.integration_registry import OPERATIONS


class CapturingCodex:
    def __init__(self, output: dict | None = None, *, returncode: int = 0) -> None:
        self.output = output or {
            "explanation": "A distributed system coordinates independent computers.",
            "terms": [{"id": "consensus", "label": "Consensus", "definition": "Agreement among participants."}],
            "children": ["Consensus algorithms"],
        }
        self.returncode = returncode
        self.prompts: list[str] = []
        self.plans: list[dict] = []

    def generate(self, prompt: str, output_dir: Path, plan: dict) -> subprocess.CompletedProcess[str]:
        self.prompts.append(prompt)
        self.plans.append(plan)
        return subprocess.CompletedProcess([], self.returncode, json.dumps(self.output), "failed")


def test_know_generates_only_from_bounded_context_and_enables_search(tmp_path: Path) -> None:
    provider = FakeAtlasProviderAdapter()
    codex = CapturingCodex()
    result = AtlasKnowledgeService(provider, adapter=codex, project_root=tmp_path).know(
        provider.node, None
    )

    assert result["node"]["status"] == "known"
    assert codex.plans[0]["permission_plan"]["build_time"]["internet_research"] is True
    prompt = codex.prompts[0]
    assert "Distributed systems" in prompt
    assert "ATLAS_KEY_SENTINEL" not in prompt
    assert "Experiences" not in prompt
    assert provider.calls[-1][1]["expected_revision"] == 3


def test_supplied_explanation_is_preserved_and_not_requested_from_codex(tmp_path: Path) -> None:
    explanation = "  My authoritative explanation.  "
    codex = CapturingCodex(
        {"terms": [{"id": "term", "label": "Term", "definition": "Definition"}], "children": ["Child"]}
    )
    provider = FakeAtlasProviderAdapter()

    AtlasKnowledgeService(provider, adapter=codex, project_root=tmp_path).know(
        provider.node, explanation
    )

    assert provider.calls[-1][1]["explanation"] == "My authoritative explanation."
    assert "Preserve user_explanation exactly" in codex.prompts[0]
    assert codex.plans[0]["codex_task"] == "atlas_knowledge_expand"


def test_already_known_rejected_before_codex(tmp_path: Path) -> None:
    provider = FakeAtlasProviderAdapter()
    provider.node["status"] = "known"
    codex = CapturingCodex()

    with pytest.raises(AtlasKnowledgeError, match="already known") as exc:
        AtlasKnowledgeService(provider, adapter=codex, project_root=tmp_path).know(provider.node, None)

    assert exc.value.error_type == "node_already_known"
    assert codex.prompts == []
    assert provider.calls == []


@pytest.mark.parametrize(
    "codex",
    [CapturingCodex(returncode=1), CapturingCodex({"explanation": "ok", "terms": [], "children": [""]})],
)
def test_codex_failure_or_invalid_output_does_not_mutate(tmp_path: Path, codex: CapturingCodex) -> None:
    provider = FakeAtlasProviderAdapter()

    with pytest.raises(AtlasKnowledgeError) as exc:
        AtlasKnowledgeService(provider, adapter=codex, project_root=tmp_path).know(provider.node, None)

    assert exc.value.error_type == "codex_failed"
    assert provider.calls == []


def test_default_knowledge_service_fails_closed_without_codex_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.codex_cli_service.codex_cli_service.resolve",
        lambda **_kwargs: SimpleNamespace(available=False, compatible=False, error="missing"),
    )

    assert codex_available() is False
    with pytest.raises(AtlasKnowledgeError, match="compatible Codex CLI") as exc:
        AtlasKnowledgeService(FakeAtlasProviderAdapter(), project_root=tmp_path)

    assert exc.value.error_type == "codex_unavailable"


def test_provider_derives_knowledge_functions_from_flat_primitive_nodes() -> None:
    adapter = UrllibAtlasProviderAdapter()
    nodes = {
        "nodes": [
            {
                "id": 1,
                "name": "Computer Science",
                "branch": "subjects",
                "parentId": None,
                "status": "known",
                "revision": 2,
                "understanding": "The study of computation.",
                "terms": [],
            },
            {
                "id": 2,
                "name": "Distributed Systems",
                "branch": "subjects",
                "parentId": 1,
                "status": "unassessed",
                "revision": 1,
                "understanding": None,
                "terms": [],
            },
        ]
    }

    search = adapter._normalize(  # noqa: SLF001 - focused adapter contract test
        "atlas.knowledge.search", nodes, {"keywords": "distributed"}
    )
    frontier = adapter._normalize(  # noqa: SLF001 - focused adapter contract test
        "atlas.knowledge.frontier.list", nodes, {}
    )
    inspected = adapter._normalize(  # noqa: SLF001 - focused adapter contract test
        "atlas.knowledge.node.get", nodes, {"node_id": 2}
    )

    assert search["nodes"][0]["node_id"] == 2
    assert frontier["nodes"][0]["path"] == ["Subjects", "Computer Science", "Distributed Systems"]
    assert inspected["node"]["parent"]["name"] == "Computer Science"


def test_provider_know_uses_only_primitive_patch_and_create_calls() -> None:
    adapter = UrllibAtlasProviderAdapter()
    nodes = [
        {
            "id": 1,
            "name": "Computer Science",
            "branch": "subjects",
            "parentId": None,
            "status": "known",
            "revision": 2,
            "understanding": "Computation.",
            "terms": [],
        },
        {
            "id": 2,
            "name": "Distributed Systems",
            "branch": "subjects",
            "parentId": 1,
            "status": "unknown",
            "revision": 1,
            "understanding": None,
            "terms": [],
        },
    ]
    calls: list[tuple[str, str]] = []

    def request(path, payload, *, timeout, max_bytes, method):  # noqa: ANN001
        del timeout, max_bytes
        calls.append((method, path))
        if method == "GET":
            return {"nodes": [dict(node) for node in nodes]}
        if method == "PATCH":
            nodes[1].update(status="known", revision=2, understanding=payload["understanding"], terms=payload["terms"])
            return {"node": dict(nodes[1])}
        child = {
            "id": 3,
            "name": payload["name"],
            "branch": payload["branch"],
            "parentId": payload["parentId"],
            "status": "unassessed",
            "revision": 1,
            "understanding": None,
            "terms": [],
        }
        nodes.append(child)
        return {"node": child}

    adapter._request = request  # type: ignore[method-assign]  # noqa: SLF001
    result = adapter.establish(
        {
            "node_id": 2,
            "expected_revision": 1,
            "explanation": "Independent computers coordinate.",
            "terms": [],
            "children": ["Consensus"],
        },
    )

    assert result["created_children"] == ["Consensus"]
    assert calls == [
        ("GET", "/api/knowledge/nodes"),
        ("PATCH", "/api/knowledge/nodes/2"),
        ("POST", "/api/knowledge/nodes"),
        ("GET", "/api/knowledge/nodes"),
    ]
    assert not hasattr(OPERATIONS["atlas.knowledge.node.know"], "endpoint_template")
