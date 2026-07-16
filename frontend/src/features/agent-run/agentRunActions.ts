import { AgentRun } from "../../api/client";

type RetryContext = Pick<AgentRun, "run_type" | "build_workflow" | "status">;

export function canRetryAgentRun(run: RetryContext): boolean {
  if (!new Set(["failed", "blocked"]).has(run.status)) return false;
  return !(run.run_type === "build_skill" && run.build_workflow === "single_codex");
}
