// @vitest-environment jsdom

import { clearManagedSessionCache } from "../features/chat/managedSessionCache";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ActSession, AgentDefinition, AgentSession, api } from "../api/client";
import AgentsPage from "./AgentsPage";

const actAgent: AgentDefinition = {
  id: "act",
  name: "Act",
  description: "Works persistently with full function access.",
  policy: { max_risk: "high", read_only: false, allowed_functions: [], banned_functions: [], model: null, reasoning_effort: null },
  permissions: { filesystem: "Shared root with workspace writes", web_search: true },
  functions: [],
};

const observerAgent: AgentDefinition = {
  id: "observer",
  name: "Observer",
  description: "Reads local context without making changes.",
  policy: { max_risk: "high", read_only: true, allowed_functions: [], banned_functions: [], model: null, reasoning_effort: null },
  permissions: { filesystem: "Read-only shared root", web_search: false },
  functions: [],
};

function renderAgents(path = "/agents") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/agents/:agentId" element={<AgentsPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function actSession(overrides: Partial<ActSession> = {}): ActSession {
  return {
    id: 4,
    title: "MarkUs",
    origin: "web",
    status: "active",
    wecom_user_id: null,
    created_at: "2026-10-09T12:00:00Z",
    updated_at: "2026-10-09T12:00:00Z",
    turns: [],
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  clearManagedSessionCache();
  vi.spyOn(api, "listActSessions").mockResolvedValue([]);
  vi.spyOn(api, "listAgentSessions").mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AgentsPage", () => {
  it("opens Act by default with Conversation selected", async () => {
    renderAgents();

    expect((await screen.findByRole("link", { name: "Act" })).className).toContain("active");
    expect(screen.getByRole("link", { name: "Act" }).getAttribute("href")).toBe("/agents/act");
    expect(screen.getByRole("button", { name: "Conversation" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Configuration" }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("textbox", { name: "Conversation message" })).toBeTruthy();
    expect(api.listActSessions).toHaveBeenCalled();
  });

  it("opens Observer from the agent tabs and keeps Conversation as the default view", async () => {
    renderAgents("/agents/observer");

    expect((await screen.findByRole("link", { name: "Observer" })).className).toContain("active");
    expect(screen.getByRole("link", { name: "Observer" }).getAttribute("href")).toBe("/agents/observer");
    expect(screen.getByRole("link", { name: "Observer" }).textContent).toBe("Observer");
    expect(screen.getByRole("button", { name: "Conversation" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("textbox", { name: "Conversation message" })).toBeTruthy();
    await waitFor(() => expect(api.listAgentSessions).toHaveBeenCalledWith("observer"));
  });

  it("saves an Act policy only after the user confirms the review", async () => {
    vi.spyOn(api, "getAgent").mockResolvedValue(actAgent);
    const updatePolicy = vi.spyOn(api, "updateAgentPolicy").mockResolvedValue({
      ...actAgent,
      policy: { ...actAgent.policy, max_risk: "low" },
    });
    renderAgents("/agents/act?view=configuration");

    const risk = await screen.findByLabelText("Maximum function risk");
    fireEvent.change(risk, { target: { value: "low" } });
    fireEvent.click(screen.getByRole("button", { name: "Save policy" }));

    expect(screen.getByRole("dialog", { name: "Update Act policy?" })).toBeTruthy();
    expect(updatePolicy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Update policy" }));

    await waitFor(() => expect(updatePolicy).toHaveBeenCalledWith("act", expect.objectContaining({ max_risk: "low" })));
  });

  it("continues the same Act turn when replying to a live notice", async () => {
    const session = actSession({
      turns: [{
        id: 9,
        session_id: 4,
        codex_turn_id: null,
        user_message: "Sign in",
        assistant_message: null,
        status: "running",
        activity_json: [],
        error_message: null,
        cancel_requested_at: null,
        delivery_message_thread_id: null,
        delivery_status: null,
        started_at: null,
        completed_at: null,
        created_at: "2026-10-09T12:00:00Z",
        live_messages: [{
          id: 2,
          role: "assistant",
          text: "Enter 123 in Duo Mobile",
          reply_status: "awaiting",
          reply_expires_at: new Date(Date.now() + 60_000).toISOString(),
          reply_to_message_id: null,
          created_at: "2026-10-09T12:00:01Z",
        }],
      }],
    });
    const summary = (({ turns: _turns, ...value }) => value)(session);
    vi.mocked(api.listActSessions).mockResolvedValue([summary]);
    vi.spyOn(api, "getActSession").mockImplementation(async () => structuredClone(session));
    const run = vi.spyOn(api, "runActTurn").mockImplementation(async () => {
      session.turns[0].live_messages![0].reply_status = "answered";
      session.turns[0].live_messages!.push({
        id: 3,
        role: "user",
        text: "Done",
        reply_status: "none",
        reply_expires_at: null,
        reply_to_message_id: 2,
        created_at: "2026-10-09T12:00:02Z",
      });
      return structuredClone(session.turns[0]);
    });
    renderAgents();

    fireEvent.click(await screen.findByRole("button", { name: /MarkUs/ }));
    await screen.findByText("Enter 123 in Duo Mobile");
    const input = screen.getByRole("textbox", { name: "Conversation message" }) as HTMLTextAreaElement;
    expect(input.disabled).toBe(false);
    fireEvent.change(input, { target: { value: "Done" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await screen.findByText("Done");
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith(4, "Done");
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });

  it("confirms archiving the current conversation before calling the Act API", async () => {
    const session = actSession();
    const summary = (({ turns: _turns, ...value }) => value)(session);
    let sessions = [summary];
    vi.mocked(api.listActSessions).mockImplementation(async () => sessions);
    vi.spyOn(api, "getActSession").mockResolvedValue(session);
    const archive = vi.spyOn(api, "archiveActSession").mockImplementation(async () => { sessions = []; });
    renderAgents();

    fireEvent.click(await screen.findByRole("button", { name: /MarkUs/ }));
    await screen.findByRole("button", { name: "Delete conversation" });
    fireEvent.click(screen.getByRole("button", { name: "Delete conversation" }));

    expect(screen.getByRole("dialog", { name: "Archive this conversation?" })).toBeTruthy();
    expect(archive).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Archive conversation" }));
    await waitFor(() => expect(archive).toHaveBeenCalledWith(4));
  });

  it("shows policy configuration on demand for Observer", async () => {
    vi.spyOn(api, "getAgent").mockResolvedValue(observerAgent);
    renderAgents("/agents/observer");

    fireEvent.click(screen.getByRole("button", { name: "Configuration" }));
    expect(await screen.findByRole("heading", { name: "Permission policy" })).toBeTruthy();
    expect(screen.getByText("Read-only shared root")).toBeTruthy();
    expect(api.getAgent).toHaveBeenCalledWith("observer");
  });
});
