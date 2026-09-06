// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentDefinition, api } from "../api/client";
import AgentsPage from "./AgentsPage";

const agents: AgentDefinition[] = [
  {
    id: "act",
    name: "Act",
    description: "Works persistently with full function access.",
    policy: { max_risk: "high", read_only: false, allowed_functions: [], banned_functions: [], model: null, reasoning_effort: null },
    permissions: { filesystem: "Shared root with workspace writes", web_search: true },
    functions: [],
  },
  {
    id: "observer",
    name: "Observer",
    description: "Reads local context without making changes.",
    policy: { max_risk: "high", read_only: true, allowed_functions: [], banned_functions: [], model: null, reasoning_effort: null },
    permissions: { filesystem: "Read-only shared root", web_search: false },
    functions: [],
  },
  {
    id: "assistant",
    name: "Assistant",
    description: "Assesses goals and proposes work for Act.",
    policy: { max_risk: "medium", read_only: true, allowed_functions: ["plan_approval_request"], banned_functions: [], model: null, reasoning_effort: null },
    permissions: { filesystem: "Read-only shared root", web_search: true },
    functions: [{ id: "plan_approval_request", title: "Request plan approval", description: "Submits a plan for review.", risk_level: "medium", mcp_read_only: false, allowed: true, reason: "Private Assistant capability" }],
  },
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AgentsPage", () => {
  it("shows Assistant boundaries, proposals, assessment controls, and session retention", async () => {
    vi.spyOn(api, "listAgents").mockResolvedValue(agents);
    vi.spyOn(api, "getAgent").mockResolvedValue(agents[2]);
    vi.spyOn(api, "listAgentSessions").mockResolvedValue([]);
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([{ id: 7, title: "Review open goals", rationale: "Several goals can move forward.", instruction: "Review the goals and complete the next safe action.", actions: "Review and implement", references: ["TODO-12"], status: "pending", execution_status: null, act_session_id: null, created_at: "2026-09-04T12:00:00Z", source_session_id: 3 }]);
    vi.spyOn(api, "getAssistantAssessment").mockResolvedValue({ enabled: true, next_run_at: "2026-09-07T12:00:00Z", last_run_at: null, last_status: null });

    render(
      <MemoryRouter initialEntries={["/agents/assistant"]}>
        <Routes><Route path="/agents/:agentId" element={<AgentsPage />} /></Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Assistant" })).toBeTruthy();
    expect(screen.getByText("Read-only shared root")).toBeTruthy();
    expect(screen.getByText("Request plan approval")).toBeTruthy();
    expect(screen.getByText("Review open goals")).toBeTruthy();
    expect(screen.getByText(/at most the five most recently created Assistant sessions/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Disable" })).toBeTruthy();
  });

  it("reviews policy edits in an Eidolon modal before saving", async () => {
    vi.spyOn(api, "listAgents").mockResolvedValue(agents);
    vi.spyOn(api, "getAgent").mockResolvedValue(agents[1]);
    vi.spyOn(api, "listAgentSessions").mockResolvedValue([]);
    const updatePolicy = vi.spyOn(api, "updateAgentPolicy").mockResolvedValue({
      ...agents[1],
      policy: { ...agents[1].policy, max_risk: "low" },
    });

    render(
      <MemoryRouter initialEntries={["/agents/observer"]}>
        <Routes><Route path="/agents/:agentId" element={<AgentsPage />} /></Routes>
      </MemoryRouter>,
    );

    const risk = await screen.findByLabelText("Maximum function risk");
    fireEvent.change(risk, { target: { value: "low" } });
    fireEvent.click(screen.getByRole("button", { name: "Review policy change" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Update Observer policy?" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Update policy" }));

    await waitFor(() => expect(updatePolicy).toHaveBeenCalledWith("observer", expect.objectContaining({ max_risk: "low" })));
  });
});
