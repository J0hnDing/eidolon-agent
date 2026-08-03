from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import AgentRun
from app.schemas.interface_artifact import InterfaceArtifact
from app.services.task_dag_service import TaskDagService
from app.workflows.base import ProjectBuildWorkflowError


class AgentRunArtifactStore:
    """Own persisted workflow artifacts under one backend-controlled run directory."""

    def __init__(self, db: Session, project_root: Path, task_dags: TaskDagService) -> None:
        self.db = db
        self.project_root = project_root.resolve()
        self.task_dags = task_dags

    def directory(self, agent_run: AgentRun) -> Path:
        path = self.project_root / "runtime" / "agent_runs" / f"run_{agent_run.id}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def initialize(self, agent_run: AgentRun) -> Path:
        path = self.project_root / "runtime" / "agent_runs" / f"run_{agent_run.id}"
        if path.exists() and any(path.iterdir()):
            orphaned_dir = path.parent / "orphaned"
            orphaned_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            path.replace(orphaned_dir / f"{path.name}_{timestamp}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def relative_path(self, agent_run: AgentRun, filename: str) -> str:
        return (self.directory(agent_run) / filename).resolve().relative_to(self.project_root).as_posix()

    def write_json(self, agent_run: AgentRun, filename: str, payload: dict[str, Any]) -> str:
        path = self.directory(agent_run) / filename
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path.resolve().relative_to(self.project_root).as_posix()

    def read_json(self, agent_run: AgentRun, filename: str) -> dict[str, Any]:
        path = self.directory(agent_run) / filename
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def write_text(self, agent_run: AgentRun, filename: str, content: str) -> str:
        path = self.directory(agent_run) / filename
        path.write_text(content, encoding="utf-8")
        return path.resolve().relative_to(self.project_root).as_posix()

    def write_task_artifacts(self, agent_run: AgentRun, task_dag: dict[str, Any]) -> list[str]:
        task_root = self.directory(agent_run) / "tasks"
        task_root.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        nodes = self.task_dags.nodes(task_dag)
        for index, node in enumerate(nodes, start=1):
            payload = {**node, "index": index, "status": "pending"}
            path = task_root / f"{node['id']}.json"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            paths.append(path.resolve().relative_to(self.project_root).as_posix())
            (task_root / node["id"]).mkdir(exist_ok=True)
        expected = {f"{node['id']}.json" for node in nodes}
        for existing in task_root.glob("*.json"):
            if existing.name not in expected:
                existing.unlink()
        return paths

    def write_task_statuses(self, agent_run: AgentRun, statuses: dict[str, str]) -> None:
        final_summary = dict(agent_run.final_summary_json or {})
        final_summary["task_statuses"] = statuses
        agent_run.final_summary_json = final_summary
        for task_id, status in statuses.items():
            path = self.directory(agent_run) / "tasks" / f"{task_id}.json"
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = status
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.db.commit()

    def write_task_test_result(self, agent_run: AgentRun, task_id: str, validation: Any) -> str:
        path = self.directory(agent_run) / "tasks" / task_id / "test_result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = validation.model_dump(mode="json") if hasattr(validation, "model_dump") else {}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path.resolve().relative_to(self.project_root).as_posix()

    def move_validated_interface_artifact(
        self,
        agent_run: AgentRun,
        skill_dir: Path,
        task_node: dict[str, Any],
    ) -> str:
        task_id = str(task_node.get("id") or agent_run.current_task_id or "core_skill")
        source = skill_dir / "interface_artifact.json"
        if not source.is_file():
            raise ProjectBuildWorkflowError(f"Builder did not write interface_artifact.json for task {task_id}")
        try:
            raw_artifact = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProjectBuildWorkflowError(f"Builder interface_artifact.json is not valid JSON: {exc}") from exc
        try:
            artifact = InterfaceArtifact.model_validate(raw_artifact)
        except ValidationError as exc:
            raise ProjectBuildWorkflowError(f"Builder interface_artifact.json is invalid: {exc}") from exc

        self._validate_interface_artifact(skill_dir, artifact, task_id, task_node, agent_run)
        destination = self.directory(agent_run) / "tasks" / task_id / "interface_artifact.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        return destination.resolve().relative_to(self.project_root).as_posix()

    def task_interface_artifact(self, agent_run: AgentRun, task_id: str) -> dict[str, Any]:
        path = self.directory(agent_run) / "tasks" / task_id / "interface_artifact.json"
        return self._read_interface_artifact(path, task_id) if path.is_file() else {}

    def parent_interface_artifacts(
        self,
        agent_run: AgentRun,
        task_node: dict[str, Any],
    ) -> list[dict[str, Any]]:
        node_by_id = {
            str(node.get("id")): node for node in self.task_dags.nodes(self.read_json(agent_run, "task_dag.json"))
        }
        ordered_parent_ids: list[str] = []
        visited: set[str] = set()

        def collect(parent_id: str) -> None:
            if parent_id in visited:
                return
            visited.add(parent_id)
            parent = node_by_id.get(parent_id, {})
            for ancestor_id in parent.get("depends_on", []) or []:
                collect(str(ancestor_id))
            ordered_parent_ids.append(parent_id)

        for parent_id in task_node.get("depends_on", []) or []:
            collect(str(parent_id))
        return self._interface_artifacts_for_ids(agent_run, ordered_parent_ids)

    def direct_parent_interface_artifacts(
        self,
        agent_run: AgentRun,
        task_node: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return self._interface_artifacts_for_ids(
            agent_run,
            [str(parent_id) for parent_id in task_node.get("depends_on", []) or []],
        )

    def all_interface_artifacts(self, agent_run: AgentRun) -> list[dict[str, Any]]:
        task_ids = [
            str(node["id"])
            for node in self.task_dags.nodes(self.read_json(agent_run, "task_dag.json"))
            if "id" in node
        ]
        return self._interface_artifacts_for_ids(agent_run, task_ids)

    def _interface_artifacts_for_ids(self, agent_run: AgentRun, task_ids: list[str]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for task_id in task_ids:
            path = self.directory(agent_run) / "tasks" / task_id / "interface_artifact.json"
            if path.is_file():
                artifacts.append(self._read_interface_artifact(path, task_id))
        return artifacts

    @staticmethod
    def _read_interface_artifact(path: Path, task_id: str) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {"task_id": task_id, **payload}

    def _parent_declared_paths(self, agent_run: AgentRun, task_node: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for artifact in self.parent_interface_artifacts(agent_run, task_node):
            for key in ("created_paths", "updated_paths"):
                values = artifact.get(key)
                if isinstance(values, list):
                    paths.update(str(value) for value in values)
        return paths

    def _validate_interface_artifact(
        self,
        skill_dir: Path,
        artifact: InterfaceArtifact,
        task_id: str,
        task_node: dict[str, Any],
        agent_run: AgentRun,
    ) -> None:
        declared_paths = set(artifact.created_paths) | set(artifact.updated_paths)
        write_paths = {
            str(path).replace("\\", "/").removeprefix("./")
            for path in task_node.get("write_paths", []) or []
        }
        missing_declarations = write_paths - declared_paths
        if missing_declarations:
            raise ProjectBuildWorkflowError(
                f"Interface artifact does not declare required task write paths: {sorted(missing_declarations)}"
            )
        allowed_paths = set(write_paths)
        allowed_paths.add("manifest.json")
        unexpected_paths = declared_paths - allowed_paths
        if unexpected_paths:
            raise ProjectBuildWorkflowError(
                f"Interface artifact declares paths outside task write_paths: {sorted(unexpected_paths)}"
            )

        parent_paths = self._parent_declared_paths(agent_run, task_node)
        update_contract_paths = parent_paths | {"manifest.json"}
        incorrectly_created = set(artifact.created_paths) & update_contract_paths
        incorrectly_updated = set(artifact.updated_paths) - update_contract_paths
        if incorrectly_created:
            raise ProjectBuildWorkflowError(
                f"Interface artifact marks existing contract paths as created: {sorted(incorrectly_created)}"
            )
        if incorrectly_updated:
            raise ProjectBuildWorkflowError(
                f"Interface artifact marks new task paths as updated: {sorted(incorrectly_updated)}"
            )

        skill_root = skill_dir.resolve()
        for relative_path in declared_paths:
            resolved = (skill_dir / relative_path).resolve()
            if not resolved.is_relative_to(skill_root) or not resolved.is_file():
                raise ProjectBuildWorkflowError(
                    f"Interface artifact declares a missing or unsafe skill file: {relative_path}"
                )
