// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentRun, AgentRunDetail, ApprovalRequest, AssistantProposal, InvocationApproval, api } from "../api/client";
import ActivityPage from "./ActivityPage";

const permissionApproval = {
  id: 5,
  skill_id: 7,
  generation_request_id: null,
  schedule_id: null,
  request_scope: "runtime",
  request_type: "permissions",
  risk_level: "low",
  requested_permissions_json: {},
  requested_dependencies_json: [],
  requested_network_domains_json: [],
  requested_filesystem_json: {},
  reason_json: {},
  reason: "The skill requires network access.",
  user_explanation: "Fetch current weather data.",
  status: "pending",
  created_at: "2026-10-20T12:00:00Z",
  resolved_at: null,
  resolved_by: null,
  decision_notes: null,
} as ApprovalRequest;

const agentRun = {
  id: 23,
  run_type: "build_skill",
  status: "succeeded",
  skill_id: 7,
  generation_request_id: null,
  user_request: "Build a weather summary skill",
  summary: null,
  current_task_id: null,
  current_step: null,
  failure_count_json: {},
  build_workflow: null,
  blueprint_json: null,
  final_summary_json: null,
  created_at: "2026-10-20T12:00:00Z",
  updated_at: "2026-10-20T12:05:00Z",
  completed_at: "2026-10-20T12:05:00Z",
  error_message: null,
  total_input_tokens: 0,
  total_cached_input_tokens: 0,
  total_output_tokens: 0,
  total_reasoning_output_tokens: 0,
  total_tokens: 0,
  pause_reason: null,
  steps: [],
} as AgentRunDetail;

const assistantProposal: AssistantProposal = {
  id: 31,
  title: "Review the open project goals",
  rationale: "A goal is ready for a safe next step.",
  instruction: "Review current progress and make the next bounded change.",
  actions: "Inspect and update the project",
  action: null,
  provenance: { kind: "goal", title: "Project goals" },
  follow_up: null,
  references: ["GOAL-4"],
  status: "pending",
  execution_status: null,
  act_session_id: null,
  created_at: "2026-10-20T12:00:00Z",
  source_session_id: 10,
};

function renderActivity(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/activity/*" element={<ActivityPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Activity page", () => {
  it("opens the approvals view by default and links a decision to its source skill", async () => {
    vi.spyOn(api, "listPermissionRequests").mockResolvedValue([permissionApproval]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
    vi.spyOn(api, "approvePermissionRequest").mockResolvedValue({ ...permissionApproval, status: "approved" });

    renderActivity("/activity");

    expect(await screen.findByRole("heading", { name: "Activity" })).toBeTruthy();
    expect(await screen.findByRole("link", { name: "Open Skill #7" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Approvals" }).getAttribute("aria-current")).toBe("page");

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(api.approvePermissionRequest).toHaveBeenCalledWith(5));
  });

  it("shows executions and keeps run links inside Activity", async () => {
    vi.spyOn(api, "listAgentRuns").mockResolvedValue([agentRun as AgentRun]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);

    renderActivity("/activity/runs");

    const runLink = await screen.findByRole("link", { name: /Run #23/ });
    expect(runLink.getAttribute("href")).toBe("/activity/runs/23");
    expect(within(screen.getByRole("navigation", { name: "Activity sections" })).getByRole("link", { name: "Executions" }).getAttribute("aria-current")).toBe("page");
  });

  it("keeps run details and return links within Activity", async () => {
    vi.spyOn(api, "getAgentRun").mockResolvedValue(agentRun);
    vi.spyOn(api, "listSkillRuns").mockResolvedValue([]);

    renderActivity("/activity/runs/23");

    expect(await screen.findByText("Build a weather summary skill")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to executions" }).getAttribute("href")).toBe("/activity/runs");
    expect(screen.getByRole("link", { name: "Skill #7" }).getAttribute("href")).toBe("/skills/7");
  });

  it("shows Assistant proposals and links review back to the Assistant", async () => {
    vi.spyOn(api, "listPermissionRequests").mockResolvedValue([]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([assistantProposal]);

    renderActivity("/activity/approvals");
    fireEvent.click(await screen.findByRole("tab", { name: "Assistant proposals" }));

    expect(await screen.findByRole("button", { name: /Review the open project goals/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Assistant" }).getAttribute("href")).toBe("/assistant");
    expect(screen.getByText("GOAL-4")).toBeTruthy();
  });
  it("approves the request displayed after filtering instead of a previously selected request", async () => {
    const second = { ...permissionApproval, id: 6, user_explanation: "Read calendar events" };
    vi.spyOn(api, "listPermissionRequests").mockResolvedValue([permissionApproval, second]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
    const approve = vi.spyOn(api, "approvePermissionRequest").mockResolvedValue({ ...second, status: "approved" });
    renderActivity("/activity/approvals");
    await screen.findByText("Fetch current weather data.", { selector: "p.approval-request-explanation" });
    fireEvent.change(screen.getByLabelText("Search approval requests"), { target: { value: "calendar" } });
    expect(screen.getByText("Read calendar events", { selector: "p.approval-request-explanation" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith(6));
  });

  it("keeps resolved requests in History and prevents approval of blocked access", async () => {
    vi.spyOn(api, "listPermissionRequests").mockResolvedValue([
      { ...permissionApproval, risk_level: "blocked" },
      { ...permissionApproval, id: 6, status: "approved", user_explanation: "Previously reviewed access" },
    ]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
    renderActivity("/activity/approvals");
    await screen.findByText("Fetch current weather data.", { selector: "p.approval-request-explanation" });
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText("Previously reviewed access", { selector: "p.approval-request-explanation" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByText("Previously reviewed access", { selector: "p.approval-request-explanation" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "Deny" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("filters workflow steps and keeps their reader open after a retry fails", async () => {
    const step: AgentRunDetail["steps"][number] = {
      id: 51, agent_run_id: 23, step_name: "builder", action: "Build the reader", task_node_id: "reader",
      approval_request_id: null, status: "failed", input_json: null, output_json: null,
      agent_input_text: "Exact input", agent_output_text: "Exact output", logs: "Recorded build log",
      started_at: null, ended_at: null, error_message: "Build failed", codex_invocations_json: [],
      input_tokens: 10, output_tokens: 5, cached_input_tokens: 0, reasoning_output_tokens: 0, total_tokens: 15,
    };
    vi.spyOn(api, "getAgentRun").mockResolvedValue({ ...agentRun, status: "failed", steps: [step, { ...step, id: 52, step_name: "tester", status: "succeeded" }, { ...step, id: 53, step_name: "backend" }] });
    vi.spyOn(api, "listSkillRuns").mockResolvedValue([]);
    const retry = vi.spyOn(api, "retryAgentRunStep").mockRejectedValue(new Error("Runtime is unavailable"));
    renderActivity("/activity/runs/23");
    await screen.findByRole("button", { name: "Inspect Builder step 51" });
    fireEvent.change(screen.getByRole("combobox", { name: "Filter workflow steps by status" }), { target: { value: "failed" } });
    expect(screen.queryByRole("button", { name: "Inspect Tester step 52" })).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search workflow steps" }), { target: { value: "missing" } });
    expect(screen.getByText("No steps match these filters.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    const trigger = screen.getByRole("button", { name: "Inspect Builder step 51" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Builder · Step 51" });
    expect(within(dialog).getByText("Recorded build log")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Agent input" }));
    expect(within(dialog).getByText("Exact input")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Agent output" }));
    expect(within(dialog).getByText("Exact output")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Retry Current Task" }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith(23, 51));
    expect((await within(dialog).findByRole("alert")).textContent).toBe("Runtime is unavailable");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    fireEvent.click(screen.getByRole("button", { name: "Inspect Backend step 53" }));
    const backendDialog = screen.getByRole("dialog", { name: "Backend · Step 53" });
    expect(within(backendDialog).queryByRole("button", { name: "Agent input" })).toBeNull();
    expect(within(backendDialog).queryByRole("button", { name: "Agent output" })).toBeNull();
  });

  it("filters executions by status and request text", async () => {
    vi.spyOn(api, "listAgentRuns").mockResolvedValue([agentRun, { ...agentRun, id: 24, user_request: "Repair a reading list", status: "paused" }]);
    renderActivity("/activity/runs");
    await screen.findByRole("link", { name: /Run #24/ });
    fireEvent.change(screen.getByLabelText("Filter executions by status"), { target: { value: "attention" } });
    expect(screen.queryByRole("link", { name: /Run #23/ })).toBeNull();
    fireEvent.change(screen.getByLabelText("Search executions"), { target: { value: "weather" } });
    expect(screen.getByText("No matching executions")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Filter executions by status"), { target: { value: "all" } });
    expect(screen.getByRole("link", { name: /Run #23/ })).toBeTruthy();
  });

  it("opens a focused build artifact reader and confirms deletion", async () => {
    vi.spyOn(api, "getAgentRun").mockResolvedValue({ ...agentRun, blueprint_json: { name: "Weather blueprint" } });
    vi.spyOn(api, "listSkillRuns").mockResolvedValue([]);
    const remove = vi.spyOn(api, "deleteAgentRun").mockResolvedValue(undefined);
    vi.spyOn(api, "listAgentRuns").mockResolvedValue([]);
    renderActivity("/activity/runs/23");
    await screen.findByText("Build a weather summary skill");
    expect(screen.queryByLabelText("Choose build artifact")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Build artifacts" }));
    expect(screen.getByText(/Weather blueprint/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Choose build artifact"), { target: { value: "graph" } });
    expect(screen.getByText("No task DAG recorded yet.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete run" }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Keep execution" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Delete run" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete execution" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(23));
  });

  it("keeps loading neutral while approval tabs and filters remain available", async () => {
    let finishLoading!: (requests: ApprovalRequest[]) => void;
    vi.spyOn(api, "listPermissionRequests").mockReturnValue(new Promise((resolve) => { finishLoading = resolve; }));
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
    renderActivity("/activity/approvals");
    expect(screen.getByRole("heading", { name: "Loading requests" })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("Loading approvals...");
    expect(screen.queryByText("You're all caught up")).toBeNull();
    expect(screen.queryByText("No requests here")).toBeNull();
    expect(screen.queryByText("Everything in its place")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Assistant proposals" }));
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByRole("tab", { name: "Assistant proposals" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("button", { name: "History" }).getAttribute("aria-pressed")).toBe("true");
    finishLoading([]);
    expect(await screen.findByRole("heading", { name: "You're all caught up" })).toBeTruthy();
    expect(screen.getByText("No requests here")).toBeTruthy();
  });

  it("does not present a failed initial load as an empty inbox and supports retry", async () => {
    vi.spyOn(api, "listPermissionRequests").mockRejectedValueOnce(new Error("Approval source is offline")).mockResolvedValue([]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
    renderActivity("/activity/approvals");
    expect(await screen.findByRole("heading", { name: "Requests unavailable" })).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe("Approval source is offline");
    expect(screen.queryByText("You're all caught up")).toBeNull();
    expect(screen.queryByText("No requests here")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("heading", { name: "You're all caught up" })).toBeTruthy();
    expect(screen.getByText("No requests here")).toBeTruthy();
  });

  it("includes all resolved permission states in History and keeps pending requests separate", async () => {
    const resolved = (["approved", "denied", "expired", "superseded"] as const).map((status, index) => ({
      ...permissionApproval, id: 10 + index, status, user_explanation: `${status} permission`,
    }));
    vi.spyOn(api, "listPermissionRequests").mockResolvedValue([permissionApproval, ...resolved]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
    renderActivity("/activity/approvals");
    await screen.findByRole("heading", { name: "Review requested access" });
    expect(screen.getByRole("button", { name: /Fetch current weather data/ })).toBeTruthy();
    for (const request of resolved) expect(screen.queryByRole("button", { name: new RegExp(request.user_explanation) })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.queryByRole("button", { name: /Fetch current weather data/ })).toBeNull();
    for (const request of resolved) expect(screen.getByRole("button", { name: new RegExp(request.user_explanation) })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "All requests" }));
    expect(screen.getByRole("button", { name: /Fetch current weather data/ })).toBeTruthy();
    for (const request of resolved) expect(screen.getByRole("button", { name: new RegExp(request.user_explanation) })).toBeTruthy();
  });

  it("filters invocation decisions separately from execution state", async () => {
    const invocation: InvocationApproval = {
      id: 41,
      target_kind: "function",
      target_id: "calendar.read",
      target_skill_id: null,
      target_version_id: null,
      target_contract_fingerprint: "test-contract",
      target_description: "Read calendar events",
      provider: "google_calendar",
      provider_account_id: null,
      caller_type: "agent",
      source: "Assistant",
      caller_skill_id: null,
      caller_version_id: null,
      caller_run_id: null,
      web_app_instance_id: null,
      initiating_action: null,
      input_json: {},
      input_hash: "test-input",
      reason_to_call: "Read the next event",
      presentation_json: { version: 1, preset: "generic", action: "Read calendar", caller: "Assistant", fields: [], reason: "Read the next event" },
      dispatch_metadata_json: {},
      decision_status: "pending",
      execution_status: "not_started",
      decided_via: null,
      decided_by: null,
      telegram_delivery_status: "not_sent",
      telegram_message_ids_json: [],
      result_json: null,
      error_type: null,
      error_message: null,
      created_at: "2026-10-20T12:00:00Z",
      delivered_at: null,
      decided_at: null,
      execution_started_at: null,
      execution_completed_at: null,
      updated_at: "2026-10-20T12:00:00Z",
    };
    vi.spyOn(api, "listPermissionRequests").mockResolvedValue([]);
    vi.spyOn(api, "listInvocationApprovals").mockResolvedValue([
      invocation, { ...invocation, id: 42, target_id: "calendar.create", decision_status: "approved", execution_status: "executing" },
    ]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
    renderActivity("/activity/approvals");
    await screen.findByRole("heading", { name: "calendar.read" });
    expect(screen.queryByRole("button", { name: /calendar.create/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    expect(screen.getByRole("heading", { name: "calendar.create" })).toBeTruthy();
    expect(screen.getByText("executing")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /calendar.read/ })).toBeNull();
    expect((screen.getByRole("button", { name: "Approve action" }) as HTMLButtonElement).disabled).toBe(true);
  });

});
