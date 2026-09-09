// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AgentSession, api } from "../api/client";
import ChatPage from "./ChatPage";

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(api, "listActSessions").mockResolvedValue([]);
  vi.spyOn(api, "getProjectConversationState").mockResolvedValue(null);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each(["observer", "assistant"] as const)("loads and continues the shared %s session", async (agentId) => {
  const session: AgentSession = {
    id: 42, agent_id: agentId, title: `${agentId} session`, origin: "telegram", status: "active",
    created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:00Z", turns: [],
  };
  vi.spyOn(api, "listAgentSessions").mockImplementation(async (id) => id === agentId ? [session] : []);
  vi.spyOn(api, "getAgentSession").mockResolvedValue(session);
  const run = vi.spyOn(api, "runAgentTurn").mockImplementation(async () => {
    const turn = {
      id: 1, session_id: 42, agent_id: agentId, codex_turn_id: null, user_message: "Hello",
      assistant_message: "Shared reply", activity_json: [], status: "completed", error_message: null,
      cancel_requested_at: null, delivery_message_thread_id: null, delivery_status: null, created_at: session.created_at,
      started_at: session.created_at, completed_at: session.updated_at,
    };
    session.turns = [turn];
    return turn;
  });
  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: new RegExp(`${agentId} session`) }));
  await waitFor(() => expect(api.getAgentSession).toHaveBeenCalledWith(agentId, 42));
  fireEvent.change(screen.getByRole("textbox", { name: "Conversation message" }), { target: { value: "Hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByText("Shared reply");
  expect(run).toHaveBeenCalledWith(agentId, 42, "Hello");
});

it.each(["observer", "assistant"] as const)("creates a backend %s session from New Chat", async (agentId) => {
  vi.spyOn(api, "listAgentSessions").mockResolvedValue([]);
  const session: AgentSession = {
    id: 99, agent_id: agentId, title: "Created session", origin: "web", status: "active",
    created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:00Z", turns: [],
  };
  vi.spyOn(api, "createAgentSession").mockResolvedValue(session);
  vi.spyOn(api, "getAgentSession").mockResolvedValue(session);
  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  await waitFor(() => expect(api.listAgentSessions).toHaveBeenCalledWith(agentId));
  fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
  fireEvent.click(screen.getByRole("button", { name: new RegExp(agentId, "i") }));
  await screen.findByText("Created session");
  expect(api.createAgentSession).toHaveBeenCalledWith(agentId);
  await waitFor(() => expect(api.getAgentSession).toHaveBeenCalledWith(agentId, 99));
});
