from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from app.services.atlas_provider import AtlasProviderAdapter
from app.services.codex_output_schema import output_schema_for_action
from app.services.github_provider import IntegrationProviderError


class AtlasKnowledgeError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class AtlasKnowledgeService:
    provider: AtlasProviderAdapter
    adapter: Any | None = None
    project_root: Path | None = None

    def __post_init__(self) -> None:
        self.project_root = (self.project_root or Path(__file__).resolve().parents[3]).resolve()
        if self.adapter is None:
            from app.services.codex_cli_service import CodexCliCompatibilityError
            from app.services.codex_service import RealCodexAdapter

            try:
                self.adapter = RealCodexAdapter()
            except CodexCliCompatibilityError:
                raise AtlasKnowledgeError(
                    "codex_unavailable",
                    "A compatible Codex CLI is unavailable",
                ) from None

    def know(self, node: dict[str, Any], explanation: str | None) -> dict[str, Any]:
        if node.get("status") == "known":
            raise AtlasKnowledgeError("node_already_known", "The selected Knowledge node is already known")
        supplied = explanation is not None
        normalized_explanation = explanation.strip() if supplied else None
        if supplied and (not normalized_explanation or len(normalized_explanation) > 2000):
            raise AtlasKnowledgeError("invalid_input", "Explanation must contain 1 to 2,000 characters")
        action = "atlas_knowledge_expand" if supplied else "atlas_knowledge_explain_expand"
        context = self._context(node, normalized_explanation)
        prompt = self._prompt(context, supplied=supplied)
        plan = {
            "codex_task": action,
            "permission_plan": {"build_time": {"internet_research": True}},
            "requested_network_domains": ["public-topic-research"],
        }
        runtime_dir = self.project_root / "runtime" / "atlas_knowledge"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="call_", dir=runtime_dir) as workspace:
                result = self.adapter.generate(prompt, Path(workspace), plan)
        except Exception:
            raise AtlasKnowledgeError("codex_failed", "Codex could not expand the Knowledge node") from None
        if result.returncode != 0:
            raise AtlasKnowledgeError("codex_failed", "Codex could not expand the Knowledge node")
        try:
            generated = json.loads(result.stdout)
            Draft202012Validator(output_schema_for_action(action)).validate(generated)
        except (json.JSONDecodeError, TypeError, ValidationError):
            raise AtlasKnowledgeError("codex_failed", "Codex returned invalid Knowledge expansion data") from None
        terms = self._terms(generated["terms"])
        children = self._children(generated["children"])
        final_explanation = normalized_explanation if supplied else str(generated["explanation"]).strip()
        if not final_explanation or len(final_explanation) > 2000:
            raise AtlasKnowledgeError("codex_failed", "Codex returned an invalid Knowledge explanation")
        payload = {
            "node_id": int(node["node_id"]),
            "expected_revision": int(node["revision"]),
            "explanation": final_explanation,
            "terms": terms,
            "children": children,
        }
        try:
            return self.provider.establish(payload)
        except IntegrationProviderError:
            raise

    @staticmethod
    def _context(node: dict[str, Any], explanation: str | None) -> dict[str, Any]:
        parent = node.get("parent") if isinstance(node.get("parent"), dict) else None
        children = node.get("children") if isinstance(node.get("children"), list) else []
        return {
            "target": {
                "node_id": node.get("node_id"),
                "name": node.get("name"),
                "status": node.get("status"),
                "revision": node.get("revision"),
            },
            "path": list(node.get("path") or []),
            "parent": (
                {"node_id": parent.get("node_id"), "name": parent.get("name"), "status": parent.get("status")}
                if parent
                else None
            ),
            "immediate_children": [
                {"name": item.get("name"), "status": item.get("status")}
                for item in children
                if isinstance(item, dict)
            ],
            "user_explanation": explanation,
        }

    @staticmethod
    def _prompt(context: dict[str, Any], *, supplied: bool) -> str:
        requested = (
            "Preserve user_explanation exactly as authoritative context. Return only target-local terms and immediate child names."
            if supplied
            else "Return one direct explanation, target-local terms, and immediate child names."
        )
        return (
            "You are expanding one public Knowledge topic for Eidolon-Atlas. Return exactly one JSON object matching "
            "the output schema and no prose. You may use live web search only for the public topic named by target.name. "
            "Never search for, quote, evaluate, or rewrite user_explanation. Propose only immediate conceptual children; "
            "do not recursively expand, rename, move, delete, or merge anything. Terms must be local to this target, use "
            "lowercase kebab-case ids, and be concise. Avoid duplicate child names already listed. "
            f"{requested}\n\nBounded context:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _terms(value: list[dict[str, Any]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in value:
            term_id = str(item["id"]).strip()
            label = str(item["label"]).strip()
            definition = str(item["definition"]).strip()
            if (
                not re.fullmatch(r"[a-z][a-z0-9-]*", term_id)
                or not label
                or len(label) > 80
                or not definition
                or len(definition) > 500
                or term_id in seen
            ):
                raise AtlasKnowledgeError("codex_failed", "Codex returned invalid Knowledge terms")
            seen.add(term_id)
            result.append({"id": term_id, "label": label, "definition": definition})
        return result

    @staticmethod
    def _children(value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            name = raw.strip()
            key = name.casefold()
            if not name or len(name) > 120:
                raise AtlasKnowledgeError("codex_failed", "Codex returned invalid Knowledge child names")
            if key not in seen:
                seen.add(key)
                result.append(name)
        return result


def codex_available(adapter: Any | None = None) -> bool:
    if adapter is not None:
        return True
    from app.services.codex_cli_service import codex_cli_service

    status = codex_cli_service.resolve()
    return bool(status.available and status.compatible)
