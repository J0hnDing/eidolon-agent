// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ActSession, AgentSession, api } from "../api/client";
import ChatPage from "./ChatPage";

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(api, "listActSessions").mockResolvedValue([]);
  vi.spyOn(api, "getProjectConversationState").mockResolvedValue(null);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("shows a live Duo notice and accepts one reply while the same Act turn keeps running", async () => {
  const session: ActSession = {
    id: 7, title: "Duo sign in", origin: "web", status: "active", wecom_user_id: null,
    created_at: "2026-10-09T12:00:00Z", updated_at: "2026-10-09T12:00:00Z", turns: [{
      id: 1, session_id: 7, codex_turn_id: null, user_message: "Sign in to MarkUs", assistant_message: null,
      status: "running", activity_json: [], error_message: null, cancel_requested_at: null,
      delivery_message_thread_id: null, delivery_status: null, started_at: null, completed_at: null,
      created_at: "2026-10-09T12:00:00Z", live_messages: [{
        id: 2, role: "assistant", text: "Enter 123 in Duo Mobile", reply_status: "awaiting",
        reply_expires_at: new Date(Date.now() + 60000).toISOString(), reply_to_message_id: null,
        created_at: "2026-10-09T12:00:01Z",
      }],
    }],
  };
  vi.spyOn(api, "listActSessions").mockResolvedValue([session]);
  vi.spyOn(api, "listAgentSessions").mockResolvedValue([]);
  vi.spyOn(api, "getActSession").mockImplementation(async () => structuredClone(session));
  const run = vi.spyOn(api, "runActTurn").mockImplementation(async () => {
    session.turns[0].live_messages![0].reply_status = "answered";
    session.turns[0].live_messages!.push({
      id: 3, role: "user", text: "Done", reply_status: "none", reply_expires_at: null,
      reply_to_message_id: 2, created_at: "2026-10-09T12:00:02Z",
    });
    return structuredClone(session.turns[0]);
  });
  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Duo sign in/ }));
  await screen.findByText("Enter 123 in Duo Mobile");
  await waitFor(() => expect((screen.getByRole("button", { name: "Send message" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(screen.getByRole("textbox", { name: "Conversation message" }), { target: { value: "Done" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByText("Done");
  await waitFor(() => expect((screen.getByRole("button", { name: "Send message" }) as HTMLButtonElement).disabled).toBe(true));
  expect(run).toHaveBeenCalledTimes(1);
  expect(run).toHaveBeenCalledWith(7, "Done");
  expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  const transcript = screen.getByText("Sign in to MarkUs").closest(".message-list")!;
  expect(transcript.textContent!.indexOf("Enter 123")).toBeLessThan(transcript.textContent!.indexOf("Done"));
  expect(transcript.textContent!.indexOf("Done")).toBeLessThan(transcript.textContent!.indexOf("Eidolon is thinking"));
});

it.each(["observer", "assistant"] as const)("loads and continues the shared %s session", async (agentId) => {
  const session: AgentSession = {
    id: 42, agent_id: agentId, title: `${agentId} session`, origin: "telegram", status: "active",
    created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:00Z", wecom_user_id: null, turns: [],
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
    created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:00Z", wecom_user_id: null, turns: [],
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

it("selects the same user's replacement after deleting a WeCom session", async () => {
  const oldSession: AgentSession = {
    id: 42, agent_id: "observer", title: "Old WeCom session", origin: "wecom", status: "active",
    created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:00Z", wecom_user_id: "user-1", turns: [],
  };
  const replacement: AgentSession = {
    ...oldSession,
    id: 43,
    title: "New observer",
    created_at: "2026-09-05T13:00:00Z",
    updated_at: "2026-09-05T13:00:00Z",
  };
  let observerSessions = [oldSession];
  vi.spyOn(api, "listAgentSessions").mockImplementation(async (id) => id === "observer" ? observerSessions : []);
  vi.spyOn(api, "getAgentSession").mockImplementation(async (_id, sessionId) =>
    sessionId === oldSession.id ? oldSession : replacement,
  );
  const archive = vi.spyOn(api, "archiveAgentSession").mockImplementation(async () => {
    observerSessions = [replacement];
  });

  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole("button", { name: /Old WeCom session/ }));
  await waitFor(() => expect(api.getAgentSession).toHaveBeenCalledWith("observer", 42));
  fireEvent.click(screen.getByRole("button", { name: "Delete conversation" }));

  await waitFor(() => expect(archive).toHaveBeenCalledWith("observer", 42));
  await screen.findByText("New observer");
  await waitFor(() => expect(api.getAgentSession).toHaveBeenCalledWith("observer", 43));
});
