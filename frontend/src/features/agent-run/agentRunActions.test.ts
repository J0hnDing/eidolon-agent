import { describe, expect, it } from "vitest";

import { canRetryAgentRun } from "./agentRunActions";

describe("canRetryAgentRun", () => {
  it("does not offer retry after a single-Codex error", () => {
    expect(canRetryAgentRun({ run_type: "build_skill", build_workflow: "single_codex", status: "failed" })).toBe(false);
    expect(canRetryAgentRun({ run_type: "build_skill", build_workflow: "single_codex", status: "blocked" })).toBe(false);
  });

  it("keeps retry available for failed task-DAG builds", () => {
    expect(canRetryAgentRun({ run_type: "build_skill", build_workflow: "task_dag", status: "failed" })).toBe(true);
  });
});
