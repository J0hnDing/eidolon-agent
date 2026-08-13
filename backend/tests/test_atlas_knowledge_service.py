import json
import subprocess
from pathlib import Path

import pytest

from app.services.atlas_knowledge_service import AtlasKnowledgeError, AtlasKnowledgeService
from app.services.atlas_provider import FakeAtlasProviderAdapter


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
        provider.node, None, "ATLAS_KEY_SENTINEL"
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
        provider.node, explanation, "key"
    )

    assert provider.calls[-1][1]["explanation"] == "My authoritative explanation."
    assert "Preserve user_explanation exactly" in codex.prompts[0]
    assert codex.plans[0]["codex_task"] == "atlas_knowledge_expand"


def test_already_known_rejected_before_codex(tmp_path: Path) -> None:
    provider = FakeAtlasProviderAdapter()
    provider.node["status"] = "known"
    codex = CapturingCodex()

    with pytest.raises(AtlasKnowledgeError, match="already known") as exc:
        AtlasKnowledgeService(provider, adapter=codex, project_root=tmp_path).know(provider.node, None, "key")

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
        AtlasKnowledgeService(provider, adapter=codex, project_root=tmp_path).know(provider.node, None, "key")

    assert exc.value.error_type == "codex_failed"
    assert provider.calls == []
