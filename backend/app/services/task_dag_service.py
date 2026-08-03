from __future__ import annotations

from typing import Any

from app.workflows.base import ProjectBuildWorkflowError


class TaskDagService:
    """Validate and schedule task DAGs without owning workflow state or I/O."""

    @staticmethod
    def nodes(task_dag: dict[str, Any]) -> list[dict[str, Any]]:
        raw_nodes = task_dag.get("nodes")
        if not isinstance(raw_nodes, list):
            return []
        nodes: list[dict[str, Any]] = []
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                continue
            node = dict(raw_node)
            if "task_prompt" not in node and "summary" in node:
                node["task_prompt"] = node["summary"]
            if "write_paths" not in node:
                node["write_paths"] = list(
                    dict.fromkeys(
                        [
                            *(node.get("expected_output_paths", []) or []),
                            *(node.get("file_write_claims", []) or []),
                        ]
                    )
                )
            for legacy_field in (
                "title",
                "summary",
                "expected_output_paths",
                "file_write_claims",
                "interface_artifact_expectations",
            ):
                node.pop(legacy_field, None)
            nodes.append(node)
        return nodes

    def validate(self, task_dag: dict[str, Any], blueprint: dict[str, Any]) -> None:
        nodes = self.nodes(task_dag)
        if not nodes:
            raise ProjectBuildWorkflowError("Task DAG must contain at least one node")
        node_by_id: dict[str, dict[str, Any]] = {}
        for node in nodes:
            node_id = str(node.get("id") or "")
            if not self._is_safe_path_segment(node_id):
                raise ProjectBuildWorkflowError(f"Task node id is not a safe path segment: {node_id}")
            if node_id in node_by_id:
                raise ProjectBuildWorkflowError(f"Duplicate task node id: {node_id}")
            if not str(node.get("task_prompt") or "").strip():
                raise ProjectBuildWorkflowError(f"Task node {node_id} must include a task prompt")
            if not node.get("acceptance_criteria"):
                raise ProjectBuildWorkflowError(f"Task node {node_id} must include acceptance criteria")
            write_paths = node.get("write_paths", []) or []
            if not write_paths:
                raise ProjectBuildWorkflowError(f"Task node {node_id} must include write paths")
            if len(write_paths) != len(set(write_paths)):
                raise ProjectBuildWorkflowError(f"Task node {node_id} contains duplicate write paths")
            approved_function_ids = set(blueprint.get("functions", []) or [])
            function_ids = node.get("function_ids", []) or []
            if len(function_ids) != len(set(function_ids)):
                raise ProjectBuildWorkflowError(f"Task node {node_id} contains duplicate function ids")
            for function_id in function_ids:
                if function_id not in approved_function_ids:
                    raise ProjectBuildWorkflowError(
                        f"Task node {node_id} references unapproved function: {function_id}"
                    )
            node_by_id[node_id] = node
        assigned_function_ids = {
            str(function_id)
            for node in nodes
            for function_id in node.get("function_ids", []) or []
        }
        blueprint_function_ids = {str(function_id) for function_id in blueprint.get("functions", []) or []}
        if blueprint_function_ids - assigned_function_ids:
            raise ProjectBuildWorkflowError(
                "Task DAG does not assign approved functions: "
                f"{sorted(blueprint_function_ids - assigned_function_ids)}"
            )
        for node in nodes:
            for dependency in node.get("depends_on", []):
                if dependency not in node_by_id:
                    raise ProjectBuildWorkflowError(
                        f"Task node {node['id']} depends on missing node {dependency}"
                    )
        self.topological_nodes(task_dag)
        if not any(node.get("requires_tests") for node in nodes):
            raise ProjectBuildWorkflowError("Skill DAG must include at least one tested node")
        for left in nodes:
            for right in nodes:
                if left["id"] >= right["id"]:
                    continue
                if self.has_dependency_path(task_dag, left["id"], right["id"]) or self.has_dependency_path(
                    task_dag, right["id"], left["id"]
                ):
                    continue
                overlap = set(left.get("write_paths", [])) & set(right.get("write_paths", []))
                if overlap:
                    raise ProjectBuildWorkflowError(
                        f"Task nodes {left['id']} and {right['id']} have overlapping write paths "
                        f"without dependency ordering: {sorted(overlap)}"
                    )

    def topological_nodes(self, task_dag: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = self.nodes(task_dag)
        node_by_id = {node["id"]: node for node in nodes if "id" in node}
        visited: set[str] = set()
        visiting: set[str] = set()
        ordered: list[dict[str, Any]] = []

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ProjectBuildWorkflowError("Task DAG is cyclic")
            if node_id not in node_by_id:
                raise ProjectBuildWorkflowError(f"Task DAG references missing node: {node_id}")
            visiting.add(node_id)
            for dependency in node_by_id[node_id].get("depends_on", []):
                visit(str(dependency))
            visiting.remove(node_id)
            visited.add(node_id)
            ordered.append(node_by_id[node_id])

        for node_id in node_by_id:
            visit(node_id)
        return ordered

    def execution_batches(
        self,
        task_dag: dict[str, Any],
        *,
        completed_task_ids: set[str] | None = None,
    ) -> list[list[dict[str, Any]]]:
        """Group ready nodes at the quota admission boundary.

        Nodes in a batch are still executed serially in the shared skill workspace. The
        grouping defines dependency-safe admission and preserves the future concurrency seam.
        """
        nodes = self.nodes(task_dag)
        completed = set(completed_task_ids or set())
        remaining = {str(node["id"]): node for node in nodes if str(node["id"]) not in completed}
        batches: list[list[dict[str, Any]]] = []
        while remaining:
            ready = [
                node
                for node in nodes
                if str(node["id"]) in remaining
                and {str(dependency) for dependency in node.get("depends_on", [])}.issubset(completed)
            ]
            if not ready:
                raise ProjectBuildWorkflowError("Task DAG has no ready nodes; dependency state is invalid")
            parallel_ready = [node for node in ready if node.get("parallel_safe", False)]
            if parallel_ready:
                batches.append(parallel_ready)
            batches.extend([[node] for node in ready if not node.get("parallel_safe", False)])
            for node in ready:
                node_id = str(node["id"])
                completed.add(node_id)
                remaining.pop(node_id, None)
        return batches

    def has_dependency_path(self, task_dag: dict[str, Any], start: str, target: str) -> bool:
        node_by_id = {node["id"]: node for node in self.nodes(task_dag) if "id" in node}
        stack = list(node_by_id.get(target, {}).get("depends_on", []))
        while stack:
            node_id = str(stack.pop())
            if node_id == start:
                return True
            stack.extend(node_by_id.get(node_id, {}).get("depends_on", []))
        return False

    @staticmethod
    def _is_safe_path_segment(value: str) -> bool:
        return bool(value) and all(character.isalnum() or character in {"_", "-"} for character in value)
