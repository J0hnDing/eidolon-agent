// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentDefinition, AssistantRuntime, api } from "../api/client";
import { AgentConfiguration } from "./AgentConfiguration";
import { AgentFunctionList, AssistantRuntimeSettings } from "./AgentControls";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

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
    render(<AgentConfiguration agentId="act" />);
    const model = await screen.findByRole("textbox", { name: "Model override" }) as HTMLInputElement;
    fireEvent.change(model, { target: { value: "draft-model" } });
    fireEvent.click(screen.getByRole("button", { name: /Effective functions/ }));
    expect(screen.queryByRole("textbox", { name: "Model override" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Effective functions" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Policy & model" }));
    expect(screen.getByRole("textbox", { name: "Model override" })).toBe(model);
    expect(model.value).toBe("draft-model");
    fireEvent.click(screen.getByRole("button", { name: /Save policy/i }));
    expect(screen.getByRole("dialog", { name: "Update Act policy?" })).toBeTruthy();
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(model.value).toBe("draft-model");
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
