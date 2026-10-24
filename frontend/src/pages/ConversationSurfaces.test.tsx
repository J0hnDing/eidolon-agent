// @vitest-environment jsdom

import { clearManagedSessionCache } from "../features/chat/managedSessionCache";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ActSession,
  ActSessionSummary,
  ActTurn,
  AgentSession,
  AgentSessionSummary,
  AgentTurn,
  AssistantRuntime,
  api,
} from "../api/client";
import { ACTIVE_CONVERSATION_KEY, CHAT_STORAGE_KEY, createConversation, saveActiveConversationId, saveStoredConversations } from "../lib/chatStore";
import AgentsPage from "./AgentsPage";
import ChatPage from "./ChatPage";

const now = "2026-10-20T12:00:00Z";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function actTurn(overrides: Partial<ActTurn> = {}): ActTurn {
  return {
    id: 1,
    session_id: 7,
    codex_turn_id: null,
    user_message: "Start the task",
    assistant_message: null,
    activity_json: [],
    status: "running",
    error_message: null,
    cancel_requested_at: null,
    delivery_message_thread_id: null,
    delivery_status: null,
    created_at: now,
    started_at: now,
    completed_at: null,
    ...overrides,
  };
}

function actSession(id: number, title: string, turns: ActTurn[] = []): ActSession {
  return {
    id,
    title,
    origin: "web",
    status: "active",
    created_at: now,
    updated_at: now,
    wecom_user_id: null,
    turns,
  };
}

function actSummary(session: ActSession): ActSessionSummary {
  const { turns: _turns, ...summary } = session;
  return summary;
}

function agentTurn(agentId: AgentSession["agent_id"], overrides: Partial<AgentTurn> = {}): AgentTurn {
  return {
    ...actTurn({ id: 2, session_id: 77, user_message: "Background work", assistant_message: "Primary response", status: "completed", completed_at: now }),
    agent_id: agentId,
    ...overrides,
  };
}

function agentSession(id: number, title: string, turns: AgentTurn[] = []): AgentSession {
  return {
    id,
    agent_id: "assistant",
    title,
    origin: "web",
    status: "active",
    created_at: now,
    updated_at: now,
    wecom_user_id: null,
    turns,
  };
}

function agentSummary(session: AgentSession): AgentSessionSummary {
  const { turns: _turns, ...summary } = session;
  return summary;
}

function assistantRuntime(primarySessionId: number | null): AssistantRuntime {
  return {
    enabled: true,
    timezone: "America/Toronto",
    primary_session_id: primarySessionId,
    initialization: { status: "pending", reviewed_batches: 0, remaining: null, error: null },
    question_asking: { asked_today: 0, timezone: "America/Toronto" },
    change_tracking: { pending_count: 0, sources: [] },
  };
}

beforeEach(() => {
  localStorage.clear();
  clearManagedSessionCache();
  vi.spyOn(api, "listActSessions").mockResolvedValue([]);
  vi.spyOn(api, "listAgentSessions").mockResolvedValue([]);
  vi.spyOn(api, "getProjectConversationState").mockResolvedValue(null);
  vi.spyOn(api, "getAssistantRuntime").mockResolvedValue(assistantRuntime(null));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("conversation surfaces", () => {
  it.each(["act", "observer", "assistant"] as const)("restores %s history immediately on return and refreshes it in the background", async (mode) => {
    const session: AgentSession = { ...agentSession(77, "Saved conversation", [agentTurn(mode, { assistant_message: "Cached reply" })]), agent_id: mode };
    const conversation = createConversation(mode, { id: `${mode}-77`, actSessionId: 77, title: session.title });
    saveStoredConversations([conversation]);
    saveActiveConversationId(conversation.id);
    const discovery = deferred<AgentSessionSummary[]>();
    const read = deferred<AgentSession>();
    if (mode === "act") {
      vi.mocked(api.listActSessions).mockResolvedValueOnce([actSummary(session)]).mockImplementation(() => discovery.promise);
      vi.spyOn(api, "getActSession").mockResolvedValueOnce(session).mockImplementation(() => read.promise);
    } else {
      vi.mocked(api.listAgentSessions).mockResolvedValueOnce([agentSummary(session)]).mockImplementation(() => discovery.promise);
      vi.mocked(api.getAssistantRuntime).mockResolvedValue(assistantRuntime(77));
      vi.spyOn(api, "getAgentSession").mockResolvedValueOnce(session).mockImplementation(() => read.promise);
    }
    const page = <MemoryRouter><ChatPage surface={mode === "assistant" ? "assistant" : "agent"} agentId={mode === "observer" ? "observer" : "act"} /></MemoryRouter>;
    const first = render(page);
    await screen.findByText("Cached reply");
    first.unmount();

    const reopened = render(page);
    expect(screen.getByText("Cached reply")).toBeTruthy();
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(reopened.container.querySelector(".conversation-welcome")).toBeNull();
    expect(localStorage.getItem(CHAT_STORAGE_KEY)).not.toContain("Cached reply");

    await act(async () => {
      discovery.resolve([agentSummary(session)]);
      read.resolve({ ...session, turns: [agentTurn(mode, { assistant_message: "Updated reply" })] });
    });
    expect(await screen.findByText("Updated reply")).toBeTruthy();
    expect(screen.queryByText("Cached reply")).toBeNull();
    expect(localStorage.getItem(CHAT_STORAGE_KEY)).not.toContain("Updated reply");
  });

  it("keeps cached history visible when background refresh is unavailable", async () => {
    const session = actSession(7, "Saved task", [actTurn({ status: "completed", assistant_message: "Keep this reply" })]);
    saveStoredConversations([createConversation("act", { id: "act-7", actSessionId: 7, title: session.title })]);
    vi.mocked(api.listActSessions).mockResolvedValueOnce([actSummary(session)]).mockRejectedValue(new Error("Offline"));
    vi.spyOn(api, "getActSession").mockResolvedValueOnce(session).mockRejectedValue(new Error("Offline"));
    const page = <MemoryRouter><ChatPage surface="agent" agentId="act" /></MemoryRouter>;
    const first = render(page);
    await screen.findByText("Keep this reply");
    first.unmount();
    render(page);
    expect(screen.getByText("Keep this reply")).toBeTruthy();
    await screen.findByText("Offline");
    expect(screen.getByText("Keep this reply")).toBeTruthy();
    expect(screen.queryByText("Could not load this conversation.")).toBeNull();
    expect(screen.queryByText("Loading conversation…")).toBeNull();
  });

  it.each(["act", "observer", "assistant"] as const)("waits for %s history without showing the welcome screen", async (mode) => {
    const read = deferred<AgentSession>();
    const session: AgentSession = { ...agentSession(77, "Saved conversation", [agentTurn(mode, { assistant_message: "Saved reply" })]), agent_id: mode };
    const conversation = createConversation(mode, { id: `${mode}-77`, actSessionId: 77, title: session.title });
    saveStoredConversations([conversation]);
    saveActiveConversationId(conversation.id);
    if (mode === "act") {
      vi.mocked(api.listActSessions).mockResolvedValue([actSummary(session)]);
      vi.spyOn(api, "getActSession").mockImplementation(() => read.promise);
    } else {
      vi.mocked(api.listAgentSessions).mockResolvedValue([agentSummary(session)]);
      vi.mocked(api.getAssistantRuntime).mockResolvedValue(assistantRuntime(77));
      vi.spyOn(api, "getAgentSession").mockImplementation(() => read.promise);
    }
    const { container } = render(<MemoryRouter><ChatPage surface={mode === "assistant" ? "assistant" : "agent"} agentId={mode === "observer" ? "observer" : "act"} /></MemoryRouter>);
    expect(screen.getByText("Loading conversation…")).toBeTruthy();
    expect(container.querySelector(".conversation-welcome")).toBeNull();
    await waitFor(() => expect(mode === "act" ? api.getActSession : api.getAgentSession).toHaveBeenCalled());
    expect(container.querySelector(".conversation-welcome")).toBeNull();
    await act(async () => read.resolve(session));
    expect(await screen.findByText("Saved reply")).toBeTruthy();
    expect(screen.queryByText("Loading conversation…")).toBeNull();
    expect(container.querySelector(".conversation-welcome")).toBeNull();
  });

  it("shows a fresh Assistant welcome only after session discovery confirms it is empty", async () => {
    const discovery = deferred<AgentSessionSummary[]>();
    vi.mocked(api.listAgentSessions).mockImplementation(() => discovery.promise);
    const { container } = render(<MemoryRouter><ChatPage surface="assistant" /></MemoryRouter>);
    expect(container.querySelector(".conversation-welcome")).toBeNull();
    await act(async () => discovery.resolve([]));
    await waitFor(() => expect(container.querySelector(".conversation-welcome")).not.toBeNull());
    expect(screen.queryByText("Loading conversation…")).toBeNull();
  });

  it("offers retry instead of a welcome when saved history cannot be loaded", async () => {
    const session = actSession(7, "Saved task", [actTurn({ status: "completed", assistant_message: "Recovered reply" })]);
    const conversation = createConversation("act", { id: "act-7", actSessionId: 7, title: session.title });
    saveStoredConversations([conversation]);
    saveActiveConversationId(conversation.id);
    vi.mocked(api.listActSessions).mockResolvedValue([actSummary(session)]);
    vi.spyOn(api, "getActSession").mockRejectedValueOnce(new Error("Offline")).mockResolvedValue(session);
    const { container } = render(<MemoryRouter><ChatPage surface="agent" agentId="act" /></MemoryRouter>);
    await screen.findByText("Could not load this conversation.");
    expect(container.querySelector(".conversation-welcome")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry conversation" }));
    expect(await screen.findByText("Recovered reply")).toBeTruthy();
  });

  it("ignores an older session read that resolves after a newer live-reply snapshot", async () => {
    const staleRead = deferred<ActSession>();
    const staleSession = actSession(7, "Task", []);
    const liveTurn = actTurn({
      user_message: "Start the task",
      live_messages: [{
        id: 30,
        role: "assistant",
        text: "Enter the current verification code",
        reply_to_message_id: null,
        reply_status: "awaiting",
        reply_expires_at: new Date(Date.now() + 60_000).toISOString(),
        created_at: now,
      }],
    });
    const latestSession = actSession(7, "Task", [liveTurn]);
    const initialSession = actSession(7, "Task", [{ ...liveTurn, live_messages: [{ ...liveTurn.live_messages![0], id: 29, text: "Earlier verification prompt" }] }]);
    const conversation = createConversation("act", { id: "act-7", actSessionId: 7, title: "Task" });
    saveStoredConversations([conversation]);
    saveActiveConversationId(conversation.id);

    vi.mocked(api.listActSessions).mockResolvedValue([actSummary(staleSession)]);
    const polls: Array<() => void> = [];
    vi.spyOn(window, "setInterval").mockImplementation((callback) => { polls.push(callback as () => void); return polls.length; });
    vi.spyOn(api, "getActSession")
      .mockResolvedValueOnce(initialSession)
      .mockImplementationOnce(() => staleRead.promise)
      .mockResolvedValue(latestSession);
    vi.spyOn(api, "runActTurn").mockResolvedValue(liveTurn);

    render(<MemoryRouter><ChatPage surface="agent" agentId="act" /></MemoryRouter>);

    await screen.findByText("Earlier verification prompt");
    act(() => polls.forEach((poll) => poll()));
    expect(api.getActSession).toHaveBeenCalledTimes(2);
    const input = screen.getByRole("textbox", { name: "Conversation message" });
    fireEvent.change(input, { target: { value: "Start the task" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(api.runActTurn).toHaveBeenCalledWith(7, "Start the task"));
    const send = await screen.findByRole("button", { name: "Send message" }) as HTMLButtonElement;
    await waitFor(() => expect(send.disabled).toBe(false));

    staleRead.resolve(staleSession);
    await waitFor(() => expect(screen.getByText("Enter the current verification code")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", false);
    expect(screen.getByText("Start the task")).toBeTruthy();
  });

  it("binds the scoped Assistant page to the runtime primary without creating another session", async () => {
    const secondary = agentSession(66, "Older Assistant", [agentTurn("assistant", { id: 3, assistant_message: "Secondary transcript" })]);
    const primary = agentSession(77, "Primary Assistant", [agentTurn("assistant", { assistant_message: "Primary transcript" })]);
    vi.mocked(api.listAgentSessions).mockResolvedValue([agentSummary(secondary), agentSummary(primary)]);
    vi.mocked(api.getAssistantRuntime).mockResolvedValue(assistantRuntime(77));
    vi.spyOn(api, "getAgentSession").mockImplementation(async (_agentId, sessionId) => sessionId === 77 ? primary : secondary);
    const create = vi.spyOn(api, "createAgentSession");

    render(<MemoryRouter><ChatPage surface="assistant" /></MemoryRouter>);

    expect(await screen.findByText("Primary transcript")).toBeTruthy();
    expect(screen.queryByText("Secondary transcript")).toBeNull();
    expect(api.getAgentSession).toHaveBeenCalledWith("assistant", 77);
    expect(create).not.toHaveBeenCalled();
    const saved = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY) ?? "[]");
    expect(saved).toEqual(expect.arrayContaining([expect.objectContaining({ mode: "assistant", actSessionId: 77 })]));
    expect(localStorage.getItem(`${ACTIVE_CONVERSATION_KEY}.assistant`)).toBeTruthy();
  });

  it("keeps Build scoped to Project history and does not import agent sessions", async () => {
    const build = createConversation("project", { id: "project-history", title: "Atlas report" });
    build.messages = [
      { id: 1, role: "assistant", content: "Build introduction" },
      { id: 2, role: "user", content: "Project history stays here" },
    ];
    const act = createConversation("act", { id: "act-history", actSessionId: 45, title: "Agent history" });
    saveStoredConversations([build, act]);
    saveActiveConversationId(build.id);

    render(<MemoryRouter><ChatPage surface="build" /></MemoryRouter>);

    expect(await screen.findByText("Project history stays here")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Atlas report/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Agent history/ })).toBeNull();
    expect(api.listActSessions).not.toHaveBeenCalled();
    expect(api.listAgentSessions).not.toHaveBeenCalled();
  });

  it("selects the backend session named by an Agents deep link", async () => {
    const other = actSession(81, "Other session", [actTurn({ id: 4, session_id: 81, assistant_message: "Other session response", status: "completed", completed_at: now })]);
    const requested = actSession(82, "Requested session", [actTurn({ id: 5, session_id: 82, assistant_message: "Requested session response", status: "completed", completed_at: now })]);
    vi.mocked(api.listActSessions).mockResolvedValue([actSummary(other), actSummary(requested)]);
    vi.spyOn(api, "getActSession").mockImplementation(async (sessionId) => sessionId === 82 ? requested : other);

    render(<MemoryRouter initialEntries={["/agents/act?session=82"]}>
      <Routes><Route path="/agents/:agentId" element={<AgentsPage />} /></Routes>
    </MemoryRouter>);

    expect(await screen.findByText("Requested session response")).toBeTruthy();
    expect(screen.queryByText("Other session response")).toBeNull();
    expect(api.getActSession).toHaveBeenCalledWith(82);
    expect(localStorage.getItem(`${ACTIVE_CONVERSATION_KEY}.act`)).toBe("act-82");
  });
});
