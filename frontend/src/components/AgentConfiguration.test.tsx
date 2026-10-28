// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryRouter } from "react-router-dom";
import { AgentDefinition, AssistantRuntime, CodexRoutingSettings, api } from "../api/client";
import { AgentInstructions, AgentModelSettings } from "./AgentSettings";
import { AgentConfiguration } from "./AgentConfiguration";
import { AgentFunctionList, AssistantRuntimeSettings } from "./AgentControls";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const empty = { model: null, reasoning_effort: null };
const routing: CodexRoutingSettings = {
  project_build_workflow_override: null,
  act: empty, observer: empty, assessment: empty, updated_at: null,
  product_manager: { default: empty, blueprint_and_permissions: empty, task_dag: empty, repair: empty, update: empty },
  builder: { default: empty, single_codex: empty, easy: empty, medium: empty, hard: empty, repair: empty, update: empty },
  tester: { default: empty, task: empty, final_e2e: empty, update: empty },
};
const catalog = { available: true, fetched_at: "2026-10-25", error: null, models: [
  { id: "fast", model: "fast", display_name: "Fast", description: "", is_default: true, default_reasoning_effort: "low", supported_reasoning_efforts: ["low", "medium"] },
  { id: "smart", model: "smart", display_name: "Smart", description: "", is_default: false, default_reasoning_effort: "high", supported_reasoning_efforts: ["high", "xhigh"] },
] };
beforeEach(() => {
  vi.spyOn(api, "getCodexRoutingSettings").mockResolvedValue(routing);
  vi.spyOn(api, "getCodexModels").mockResolvedValue(catalog);
  vi.spyOn(api, "getAgentInstructions").mockImplementation(async (agentId) => ({ agent_id: agentId, instructions: `Instructions for ${agentId}.` }));
});

const agent: AgentDefinition = {
  id: "act",
  name: "Act",
  description: "Works persistently.",
  policy: { max_risk: "medium", read_only: false, allowed_functions: [], banned_functions: [], model: null, reasoning_effort: null },
  permissions: { filesystem: "Shared workspace", web_search: true },
  functions: [
    { id: "notes.read", title: "Read notes", description: "Read saved notes.", risk_level: "low", mcp_read_only: true, allowed: true, reason: "Within defaults" },
    { id: "notes.write", title: "Write notes", description: "Update saved notes.", risk_level: "medium", mcp_read_only: false, allowed: false, reason: "Explicitly banned" },
  ],
};

const runtime: AssistantRuntime = {
  initialization: { status: "complete", reviewed_batches: 1, remaining: 0, error: null },
  question_asking: { asked_today: 0, timezone: "America/Toronto" },
  enabled: true,
  timezone: "America/Toronto",
  primary_session_id: 1,
  refresh_time: "04:00",
  next_refresh_at: null,
  refresh_pending: false,
  change_tracking: { pending_count: 0, sources: [] },
};

describe("agent configuration controls", () => {
  it("preserves a policy draft across function inspection and requires confirmation to save", async () => {
    vi.spyOn(api, "getAgent").mockResolvedValue(agent);
    const save = vi.spyOn(api, "updateAgentPolicy");
    render(<MemoryRouter><AgentConfiguration agentId="act" /></MemoryRouter>);
    const model = await screen.findByLabelText("Maximum function risk") as HTMLSelectElement;
    fireEvent.change(model, { target: { value: "low" } });
    fireEvent.click(screen.getByRole("button", { name: /Effective functions/ }));
    expect(screen.queryByRole("combobox", { name: "Maximum function risk" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Effective functions" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Policy & model" }));
    expect(screen.getByLabelText("Maximum function risk")).toBe(model);
    expect(model.value).toBe("low");
    fireEvent.click(screen.getByRole("button", { name: /Save policy/i }));
    expect(screen.getByRole("dialog", { name: "Update Act policy?" })).toBeTruthy();
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(model.value).toBe("low");
  });
  it.each(["act", "observer", "assistant"] as const)("edits the existing model route for %s", async (agentId) => {
    const route = agentId === "assistant" ? "assessment" : agentId;
    const name = { act: "Act", observer: "Observer", assistant: "Assistant" }[agentId];
    vi.mocked(api.getCodexRoutingSettings).mockResolvedValue({ ...routing, [route]: { model: "smart", reasoning_effort: "high" } });
    const save = vi.spyOn(api, "updateAgentModel").mockResolvedValue({ ...routing, [route]: { model: "fast", reasoning_effort: "medium" } });
    render(<MemoryRouter><AgentModelSettings agentId={agentId} /></MemoryRouter>);
    const model = await screen.findByRole("combobox", { name: `${name} model` });
    expect(model).toHaveProperty("value", "smart");
    const effort = screen.getByRole("combobox", { name: `${name} effort` });
    expect(effort).toHaveProperty("value", "high");
    fireEvent.change(model, { target: { value: "fast" } });
    expect(effort).toHaveProperty("value", "");
    expect(screen.queryByRole("option", { name: "xhigh" })).toBeNull();
    fireEvent.change(effort, { target: { value: "medium" } });
    fireEvent.click(screen.getByRole("button", { name: "Save model settings" }));
    await screen.findByText("Model settings saved.");
    expect(save).toHaveBeenCalledWith(agentId, { model: "fast", reasoning_effort: "medium" });
    expect(screen.getByRole("link", { name: "Model routing" }).getAttribute("href")).toBe("/settings/models");
  });

  it("retains a model draft after a failed save", async () => {
    vi.spyOn(api, "updateAgentModel").mockRejectedValue(new Error("Catalog unavailable"));
    render(<MemoryRouter><AgentModelSettings agentId="act" /></MemoryRouter>);
    const model = await screen.findByRole("combobox", { name: "Act model" });
    fireEvent.change(model, { target: { value: "smart" } });
    fireEvent.click(screen.getByRole("button", { name: "Save model settings" }));
    await screen.findByText("Catalog unavailable");
    expect(model).toHaveProperty("value", "smart");
    expect(screen.getByRole("button", { name: "Save model settings" })).toHaveProperty("disabled", false);
  });

  it("retries unavailable model catalogs", async () => {
    vi.mocked(api.getCodexModels).mockResolvedValueOnce({ ...catalog, available: false, error: "Unavailable", models: [] });
    render(<MemoryRouter><AgentModelSettings agentId="act" /></MemoryRouter>);
    await screen.findByRole("alert");
    expect(screen.queryByRole("combobox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("combobox", { name: "Act model" });
  });

  it("loads runtime instructions only when requested and preserves model drafts across views", async () => {
    vi.spyOn(api, "getAgent").mockResolvedValue(agent);
    render(<MemoryRouter><AgentConfiguration agentId="act" /></MemoryRouter>);
    const model = await screen.findByRole("combobox", { name: "Act model" });
    expect(api.getAgentInstructions).not.toHaveBeenCalled();
    fireEvent.change(model, { target: { value: "smart" } });
    fireEvent.click(screen.getByRole("button", { name: "Instructions" }));
    expect(await screen.findByLabelText("Act base instructions")).toHaveProperty("textContent", "Instructions for act.");
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Policy & model" }));
    expect(screen.getByRole("combobox", { name: "Act model" })).toBe(model);
    expect(model).toHaveProperty("value", "smart");
  });

  it("retries instruction loading without showing invented content", async () => {
    vi.mocked(api.getAgentInstructions).mockRejectedValueOnce(new Error("Instructions unavailable"));
    render(<AgentInstructions agentId="observer" />);
    await screen.findByText("Instructions unavailable");
    expect(screen.queryByLabelText("Observer base instructions")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByLabelText("Observer base instructions").textContent).toBe("Instructions for observer."));
  });

  it("filters effective functions by status and search text", () => {
    render(<AgentFunctionList agent={agent} />);

    expect(screen.getByText("Read notes")).toBeTruthy();
    expect(screen.getByText("Write notes")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Show"), { target: { value: "blocked" } });
    expect(screen.queryByText("Read notes")).toBeNull();
    expect(screen.getByText("Write notes")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Search functions"), { target: { value: "notes.read" } });
    expect(screen.getByText("No functions match these filters.")).toBeTruthy();
  });

  it("shows Assistant runtime settings directly as a section", () => {
    const { container } = render(
      <AssistantRuntimeSettings runtime={runtime} isWorking={false} onSave={vi.fn(async () => {})} onRefresh={vi.fn(async () => {})} />,
    );

    expect(screen.getByRole("heading", { name: "Proactive Assistant and session" })).toBeTruthy();
    expect(screen.getByLabelText("Enable proactive Assistant")).toBeTruthy();
    expect(container.querySelector("details")).toBeNull();
  });
});
