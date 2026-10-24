// @vitest-environment jsdom

import { clearManagedSessionCache } from "../features/chat/managedSessionCache";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ActSession, AgentSession, api } from "../api/client";
import { ACTIVE_CONVERSATION_KEY, CHAT_STORAGE_KEY, createConversation } from "../lib/chatStore";
import ChatPage from "./ChatPage";

beforeEach(() => {
  localStorage.clear();
  clearManagedSessionCache();
  vi.spyOn(api, "listActSessions").mockResolvedValue([]);
  vi.spyOn(api, "getProjectConversationState").mockResolvedValue(null);
  vi.spyOn(api, "getAssistantRuntime").mockResolvedValue({ enabled: true, timezone: "America/Toronto", primary_session_id: null, summary: "", change_tracking: { pending_count: 0, sources: [] }, initialization: { status: "pending", reviewed_batches: 0, remaining: null, error: null }, question_asking: { asked_today: 0, timezone: "America/Toronto" } });
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

it("opens the primary Assistant conversation from New Chat and accepts its live reply", async () => {
  const session: AgentSession = {
    id: 42, agent_id: "assistant", title: "Primary Assistant", origin: "web", status: "active", wecom_user_id: null,
    created_at: "2026-10-16T12:00:00Z", updated_at: "2026-10-16T12:00:00Z", turns: [{
      id: 8, session_id: 42, agent_id: "assistant", codex_turn_id: null, user_message: "Internal review prompt", assistant_message: null,
      backend_message_kind: "assistant_daily", status: "running", activity_json: [], error_message: null, cancel_requested_at: null,
      delivery_message_thread_id: null, delivery_status: null, started_at: null, completed_at: null, created_at: "2026-10-16T12:00:00Z",
      live_messages: [{ id: 12, role: "assistant", text: "Enter 456 in Duo Mobile", reply_status: "awaiting", reply_expires_at: new Date(Date.now() + 60000).toISOString(), reply_to_message_id: null, created_at: "2026-10-16T12:00:01Z" }],
    }],
  };
  vi.mocked(api.getAssistantRuntime).mockResolvedValue({ enabled: true, timezone: "America/Toronto", primary_session_id: 42, summary: "", change_tracking: { pending_count: 0, sources: [] }, initialization: { status: "pending", reviewed_batches: 0, remaining: null, error: null }, question_asking: { asked_today: 0, timezone: "America/Toronto" } });
  vi.spyOn(api, "listAgentSessions").mockImplementation(async (id) => id === "assistant" ? [session] : []);
  vi.spyOn(api, "getAgentSession").mockImplementation(async () => structuredClone(session));
  const create = vi.spyOn(api, "createAgentSession");
  const run = vi.spyOn(api, "runAgentTurn").mockImplementation(async () => {
    session.turns[0].live_messages![0].reply_status = "answered";
    return structuredClone(session.turns[0]);
  });
  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
  fireEvent.click(screen.getByRole("button", { name: "Open Assistant" }));
  await screen.findByText("Enter 456 in Duo Mobile");
  expect(create).not.toHaveBeenCalled();
  expect(screen.queryByText("Internal review prompt")).toBeNull();
  expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", false);
  fireEvent.change(screen.getByRole("textbox", { name: "Conversation message" }), { target: { value: "Done" } });
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(run).toHaveBeenCalledWith("assistant", 42, "Done"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", true));
});

it("reopens a previously bound Assistant chat without losing its local draft", async () => {
  const existing = { ...createConversation("assistant", { id: "local-assistant-chat", actSessionId: 42, title: "Existing Assistant" }), draft: "My unfinished question" };
  localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify([existing, createConversation("project", { id: "project-chat" })]));
  localStorage.setItem(ACTIVE_CONVERSATION_KEY, "project-chat");
  const session: AgentSession = { id: 42, agent_id: "assistant", title: "Existing Assistant", origin: "web", status: "active", wecom_user_id: null, turns: [], created_at: existing.createdAt, updated_at: existing.updatedAt };
  vi.spyOn(api, "listAgentSessions").mockImplementation(async (id) => id === "assistant" ? [session] : []);
  vi.spyOn(api, "getAgentSession").mockResolvedValue(session);
  render(<MemoryRouter><ChatPage /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "New Chat" }));
  fireEvent.click(screen.getByRole("button", { name: "Open Assistant" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Conversation message" })).toHaveProperty("value", "My unfinished question"));
  expect(localStorage.getItem(ACTIVE_CONVERSATION_KEY)).toBe("local-assistant-chat");
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
  fireEvent.click(screen.getByRole("button", { name: new RegExp(`^(Open|Create) ${agentId}$`, "i") }));
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
