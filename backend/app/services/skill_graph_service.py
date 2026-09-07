from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY
from app.models import Skill, SkillVersion
from app.schemas.manifest import (
    RiskLevel,
    SkillManifest,
    classify_permission_risk,
    manifest_permission_requests,
)
from app.services.manifest_validator import ManifestValidationError, validate_manifest_file

GraphAvailability = Literal["available", "disabled", "error"]
_RISK_RANK: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}
_AVAILABILITY_RANK: dict[GraphAvailability, int] = {
    "available": 0,
    "disabled": 1,
    "error": 2,
}


class SkillGraphError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveSkillContract:
    risk_level: RiskLevel
    permissions: dict[str, object]
    availability: GraphAvailability
    availability_reasons: tuple[str, ...]
    descendants: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class _GraphNode:
    name: str
    risk_level: RiskLevel
    permissions: dict[str, object]
    availability: GraphAvailability
    availability_reasons: tuple[str, ...]
    descendants: tuple[str, ...]
    fingerprint_payload: dict[str, object]


class SkillGraphService:
    """Derive transitive function relationships from active manifest declarations."""

    def __init__(self, db: Session, *, project_root: Path | None = None) -> None:
        self.db = db
        self.project_root = (
            project_root or Path(__file__).resolve().parents[3]
        ).resolve()

    def effective_contract(
        self,
        skill: Skill,
        *,
        manifest: SkillManifest | None = None,
        strict: bool = False,
    ) -> EffectiveSkillContract:
        overrides = {skill.name: manifest} if manifest is not None else {}
        node = self._visit(
            skill,
            overrides=overrides,
            root_name=skill.name,
            path=(),
        )
        if strict and node.availability == "error":
            raise SkillGraphError("; ".join(node.availability_reasons))
        encoded = json.dumps(
            node.fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return EffectiveSkillContract(
            risk_level=node.risk_level,
            permissions=node.permissions,
            availability=node.availability,
            availability_reasons=node.availability_reasons,
            descendants=node.descendants,
            fingerprint=hashlib.sha256(encoded).hexdigest(),
        )

    def ancestors(self, child_name: str) -> list[Skill]:
        skills = list(self.db.scalars(select(Skill).where(Skill.status == "installed")).all())
        by_child: dict[str, list[Skill]] = {}
        for skill in skills:
            for requirement in skill.function_requirements_json or []:
                if isinstance(requirement, str) and requirement:
                    by_child.setdefault(requirement, []).append(skill)
        result: list[Skill] = []
        seen: set[int] = set()
        pending = [child_name]
        while pending:
            current = pending.pop(0)
            for parent in by_child.get(current, []):
                if parent.id in seen:
                    continue
                seen.add(parent.id)
                result.append(parent)
                pending.append(parent.name)
        return result

    def refresh_effective_risks(self) -> None:
        for skill in self.db.scalars(select(Skill).where(Skill.status == "installed")).all():
            contract = self.effective_contract(skill)
            skill.risk_level = contract.risk_level

    def _visit(
        self,
        skill: Skill,
        *,
        overrides: dict[str, SkillManifest | None],
        root_name: str,
        path: tuple[str, ...],
    ) -> _GraphNode:
        if skill.name in path:
            cycle = " -> ".join((*path, skill.name))
            return self._error_node(skill.name, f"Function dependency cycle detected: {cycle}")

        manifest = overrides.get(skill.name)
        if manifest is None:
            manifest = self._active_manifest(skill)
        if manifest is None:
            return self._error_node(
                skill.name,
                f"Required function {skill.name} has no active manifest",
            )

        own_risk = classify_permission_risk(
            manifest.permissions,
            manifest.dependencies,
            requires_invocation_approval=manifest.requires_invocation_approval,
        )
        permissions = manifest_permission_requests(manifest.permissions)
        availability: GraphAvailability = "available"
        reasons: list[str] = []
        if skill.name != root_name:
            if skill.status != "installed" or skill.runtime != "function":
                availability = "error"
                reasons.append(f"Required function {skill.name} is not an installed function")
            elif not skill.enabled:
                availability = "disabled"
                reasons.append(f"Required function {skill.name} is disabled")

        descendants: list[str] = []
        child_payloads: list[dict[str, object]] = []
        risk = own_risk
        next_path = (*path, skill.name)
        for child_name in manifest.function_requirements:
            child = self.db.scalar(select(Skill).where(Skill.name == child_name))
            if child is None:
                child_node = self._error_node(
                    child_name,
                    f"Required function {child_name} is missing or deleted",
                )
            else:
                child_node = self._visit(
                    child,
                    overrides=overrides,
                    root_name=root_name,
                    path=next_path,
                )
            risk = max((risk, child_node.risk_level), key=_RISK_RANK.__getitem__)
            permissions = self._merge_permissions(permissions, child_node.permissions)
            availability = max(
                (availability, child_node.availability),
                key=_AVAILABILITY_RANK.__getitem__,
            )
            reasons.extend(child_node.availability_reasons)
            for name in (child_name, *child_node.descendants):
                if name not in descendants:
                    descendants.append(name)
            child_payloads.append(child_node.fingerprint_payload)

        for requirement in manifest.integration_requirements:
            for operation_id in requirement.operations:
                operation = DEFAULT_INTEGRATION_REGISTRY.get(operation_id)
                if operation is None:
                    raise SkillGraphError(f"Unknown integration operation: {operation_id}")
                risk = max((risk, operation.risk), key=_RISK_RANK.__getitem__)

        payload: dict[str, object] = {
            "name": skill.name,
            "runtime": manifest.runtime,
            "own_risk": own_risk,
            "requires_invocation_approval": manifest.requires_invocation_approval,
            "permissions": manifest_permission_requests(manifest.permissions),
            "dependencies": list(manifest.dependencies),
            "children": child_payloads,
        }
        return _GraphNode(
            name=skill.name,
            risk_level=risk,
            permissions=permissions,
            availability=availability,
            availability_reasons=tuple(dict.fromkeys(reasons)),
            descendants=tuple(descendants),
            fingerprint_payload=payload,
        )

    def _active_manifest(self, skill: Skill) -> SkillManifest | None:
        raw_path = skill.manifest_path
        if raw_path:
            manifest_path = Path(raw_path)
            if not manifest_path.is_absolute():
                manifest_path = self.project_root / manifest_path
            try:
                resolved = manifest_path.resolve()
                if resolved.is_relative_to(self.project_root) and resolved.is_file():
                    return validate_manifest_file(resolved)
            except (OSError, ManifestValidationError):
                return None
        if skill.active_version_id is None:
            return None
        version = self.db.get(SkillVersion, skill.active_version_id)
        if version is None or not isinstance(version.manifest_json, dict):
            return None
        try:
            return SkillManifest.model_validate(version.manifest_json)
        except ValueError:
            return None

    @staticmethod
    def _error_node(name: str, reason: str) -> _GraphNode:
        return _GraphNode(
            name=name,
            risk_level="high",
            permissions={},
            availability="error",
            availability_reasons=(reason,),
            descendants=(),
            fingerprint_payload={"name": name, "error": reason},
        )

    @staticmethod
    def _merge_permissions(
        left: dict[str, object],
        right: dict[str, object],
    ) -> dict[str, object]:
        merged: dict[str, object] = {}
        for key in ("network", "filesystem_read", "filesystem_write", "secrets"):
            values: list[str] = []
            for source in (left, right):
                raw_values = source.get(key, [])
                if not isinstance(raw_values, list):
                    continue
                for value in raw_values:
                    normalized = str(value)
                    if normalized not in values:
                        values.append(normalized)
            if values:
                merged[key] = values
        if bool(left.get("shell")) or bool(right.get("shell")):
            merged["shell"] = True
        codex: dict[str, bool] = {}
        for key in ("call_response", "internet_access"):
            if any(
                isinstance(source.get("codex"), dict)
                and bool(source["codex"].get(key))  # type: ignore[index,union-attr]
                for source in (left, right)
            ):
                codex[key] = True
        if codex:
            merged["codex"] = codex
        return merged
