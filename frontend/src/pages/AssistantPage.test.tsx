// @vitest-environment jsdom

import { clearManagedSessionCache } from "../features/chat/managedSessionCache";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AgentDefinition, AgentSession, AssistantAgendaItem, AssistantRuntime, api } from "../api/client";
import AssistantPage, { AssistantDetailsPage } from "./AssistantPage";

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
    description: "Your proactive personal assistant.",
    policy: { max_risk: "medium", read_only: true, allowed_functions: [], banned_functions: [], model: null, reasoning_effort: null },
    permissions: { filesystem: "Read-only shared root", web_search: true },
    functions: [],
  },
];

const runtime: AssistantRuntime = {
  initialization: { status: "pending", reviewed_batches: 0, remaining: null, error: null },
  enabled: true,
  timezone: "America/Toronto", primary_session_id: null, summary: "", change_tracking: { pending_count: 0, sources: [] }, question_asking: { asked_today: 0, timezone: "America/Toronto" },
};

function mockAssistant(items: AssistantAgendaItem[] = []) {
  vi.spyOn(api, "getAgent").mockResolvedValue(agents[2]);
  const empty = { model: null, reasoning_effort: null };
  vi.spyOn(api, "getCodexRoutingSettings").mockResolvedValue({
    project_build_workflow_override: null, updated_at: null, act: empty, observer: empty, assessment: empty,
    product_manager: { default: empty, blueprint_and_permissions: empty, task_dag: empty, repair: empty, update: empty },
    builder: { default: empty, single_codex: empty, easy: empty, medium: empty, hard: empty, repair: empty, update: empty },
    tester: { default: empty, task: empty, final_e2e: empty, update: empty },
  });
  vi.spyOn(api, "getCodexModels").mockResolvedValue({ available: true, fetched_at: "2026-10-25", error: null, models: [] });
  vi.spyOn(api, "getAgentInstructions").mockResolvedValue({ agent_id: "assistant", instructions: "Assistant runtime instructions." });
  vi.spyOn(api, "listAgentSessions").mockResolvedValue([]);
  vi.spyOn(api, "listAssistantProposals").mockResolvedValue([]);
  vi.spyOn(api, "getAssistantRuntime").mockResolvedValue(runtime);
  vi.spyOn(api, "listAssistantAgenda").mockResolvedValue(items);
}

function renderAssistant(openDetails = true, view = "agenda") {
  return render(<MemoryRouter initialEntries={[openDetails ? `/assistant/details?view=${view}` : "/assistant"]}>
    <Routes><Route path="/assistant" element={<AssistantPage />} /><Route path="/assistant/details" element={<AssistantDetailsPage />} /></Routes>
  </MemoryRouter>);
}

beforeEach(() => {
  localStorage.clear();
  clearManagedSessionCache();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AssistantPage", () => {
  it("keeps one detail section visible and preserves settings drafts across navigation", async () => {
    mockAssistant();
    renderAssistant(true, "settings");
    await screen.findByLabelText("Assistant timezone");
    expect(await screen.findByRole("combobox", { name: "Assistant model" })).toBeTruthy();
    expect(await screen.findByLabelText("Assistant base instructions")).toHaveProperty("textContent", "Assistant runtime instructions.");
    expect(screen.queryByRole("heading", { name: "Assistant agenda" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Assistant timezone"), { target: { value: "Europe/London" } });
    fireEvent.click(screen.getByRole("link", { name: "Agenda & questions" }));
    expect(await screen.findByRole("heading", { name: "Assistant agenda" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Assistant settings" })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "Settings" }));
    expect(screen.getByLabelText("Assistant timezone")).toHaveProperty("value", "Europe/London");
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("aria-current")).toBe("page");
  });

  it("searches and filters action rows before opening a review without executing it", async () => {
    mockAssistant();
    const base = { rationale: "", instruction: "Review first", actions: "", references: [], act_session_id: null, created_at: "2026-10-20T12:00:00Z", source_session_id: 3 };
    vi.mocked(api.listAssistantProposals).mockResolvedValue([
      { ...base, id: 1, title: "Review research", status: "pending", execution_status: null },
      { ...base, id: 2, title: "Prepare notes", status: "approved", execution_status: "running" },
    ]);
    const approve = vi.spyOn(api, "approveAssistantProposal");
    renderAssistant(true, "actions");
    await screen.findByRole("button", { name: /Review research/ });
    expect(screen.queryByRole("button", { name: /Prepare notes/ })).toBeNull();
    fireEvent.change(screen.getByLabelText("Search actions"), { target: { value: "missing" } });
    expect(screen.getByText("No matching actions")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search actions"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Filter actions"), { target: { value: "active" } });
    expect(screen.getByRole("button", { name: /Prepare notes/ })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Filter actions"), { target: { value: "pending" } });
    fireEvent.click(screen.getByRole("button", { name: /Review research/ }));
    expect(screen.getByRole("dialog", { name: "Review research" })).toBeTruthy();
    expect(approve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Approve action" }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByRole("dialog", { name: "Approve this plan?" })).toBeTruthy();
    expect(approve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("dialog", { name: "Review research" })).toBeTruthy();
  });

  it("keeps the conversation focused until the agenda is requested", async () => {
    mockAssistant();
    renderAssistant(false);
    expect(screen.getByRole("link", { name: "Assistant details" }).getAttribute("href")).toBe("/assistant/details");
    expect(screen.queryByRole("heading", { name: "Assistant agenda" })).toBeNull();
    expect(screen.getByRole("textbox", { name: "Conversation message" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Assistant details" }));
    expect(await screen.findByRole("heading", { name: "Assistant agenda" })).toBeTruthy();
  });
  it.each([
    ["pending", 0, null, "Waiting to start"],
    ["reading", 4, 8, "Reading Atlas · 4 batches reviewed · 8 steps remaining"],
    ["blocked", 4, 8, "Waiting to retry · 4 batches reviewed · 8 steps remaining. Progress is saved; the Assistant will retry automatically."],
    ["complete", 12, 0, "Complete · 12 batches reviewed"],
  ] as const)("shows saved Atlas initialization progress for %s", async (status, reviewed_batches, remaining, text) => {
    mockAssistant();
    vi.mocked(api.getAssistantRuntime).mockResolvedValue({ ...runtime,
      initialization: { status, reviewed_batches, remaining, error: status === "blocked" ? "provider_unavailable" : null },
    });
    renderAssistant();
    fireEvent.click(screen.getByRole("link", { name: "Settings" }));
    await screen.findByText("Assistant status");
    expect(await screen.findByText(`Initial Atlas review: ${text}`)).toBeTruthy();
    expect(screen.queryByText("provider_unavailable")).toBeNull();
  });

  it("shows agenda, runtime settings, and consequential proposals on the dedicated page", async () => {
    mockAssistant();
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([{ id: 7, title: "Review open goals", rationale: "Several goals can move forward.", instruction: "Review the goals and complete the next safe action.", actions: "Review and implement", references: ["TODO-12"], status: "pending", execution_status: null, act_session_id: null, created_at: "2026-09-04T12:00:00Z", source_session_id: 3 }]);

    renderAssistant();

    expect(await screen.findByRole("heading", { name: "Assistant details" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Assistant agenda" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: /^Actions/ }));
    expect(await screen.findByText("Review open goals")).toBeTruthy();
    expect(screen.queryByText(/at most the five/i)).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Conversation message" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New Chat" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open Assistant conversation" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Assistant agenda" })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "Tool access" }));
    expect(await screen.findByRole("heading", { name: "Permission policy" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: "Settings" }));
    await screen.findByText("Proactive Assistant and session");
    expect(screen.getByLabelText("Assistant timezone")).toHaveProperty("value", "America/Toronto");
    expect(screen.queryByLabelText("Daily review time")).toBeNull();
  });

  it("requests a refresh and explains that the permanent chat keeps its history", async () => {
    mockAssistant();
    vi.mocked(api.getAssistantRuntime)
      .mockResolvedValueOnce(runtime)
      .mockResolvedValue({ ...runtime, refresh_pending: true });
    const refresh = vi.spyOn(api, "refreshAssistantRuntime").mockResolvedValue({
      ...runtime,
      refresh_pending: true,
      status: "refresh_pending",
      session_id: 41,
    });
    renderAssistant();

    fireEvent.click(screen.getByRole("link", { name: "Settings" }));
    await screen.findByText("Proactive Assistant and session");
    fireEvent.click(screen.getByRole("button", { name: "Refresh Assistant session" }));

    expect(await screen.findByText(/Refresh pending\. It will be applied before the next turn \(after any running turn finishes\)\./)).toBeTruthy();
    expect(await screen.findByText(/fresh context; this permanent chat history stays visible but is not sent into the new session/)).toBeTruthy();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Refresh Assistant session" })).toHaveProperty("disabled", true);
  });

  it("shows the fixed daily refresh time and the next refresh in the configured timezone", async () => {
    mockAssistant();
    vi.mocked(api.getAssistantRuntime).mockResolvedValue({
      ...runtime,
      refresh_time: "04:00",
      next_refresh_at: "2026-10-16T08:00:00Z",
    });
    renderAssistant();

    fireEvent.click(screen.getByRole("link", { name: "Settings" }));
    await screen.findByText("Proactive Assistant and session");
    expect(screen.getByText(/starts a fresh session daily at/i).textContent).toMatch(/4:00\s*AM \(America\/Toronto\)/);
    const nextRefresh = screen.getByText(/^Next session refresh:/);
    expect(nextRefresh.textContent).toContain("America/Toronto");
    expect(nextRefresh.textContent).toContain("4:00");
    expect(screen.getByText(/startup recovery applies one missed refresh before the next Assistant turn/)).toBeTruthy();
  });

  it("lets the user edit pending deterministic function steps", async () => {
    mockAssistant();
    const proposal = {
      id: 9, title: "Create a calendar event", rationale: "", instruction: "",
      actions: "Create a calendar event", references: [], status: "pending",
      execution_status: null, act_session_id: null, created_at: "2026-09-28T12:00:00Z",
      source_session_id: 3,
      action: { type: "functions" as const, description: "Create a calendar event", steps: [
        { function: "google_calendar.event.create", arguments: { title: "Before" } },
      ] },
    };
    vi.spyOn(api, "listAssistantProposals").mockResolvedValue([proposal]);
    const edit = vi.spyOn(api, "editAssistantFunctionProposal").mockResolvedValue(proposal);
    renderAssistant();
    fireEvent.click(screen.getByRole("link", { name: /^Actions/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Create a calendar event/ }));
    const editor = await screen.findByText("Edit function sequence");
    fireEvent.click(editor);
    fireEvent.change(screen.getByLabelText("Function sequence JSON"), { target: {
      value: '[{"function":"google_calendar.event.create","arguments":{"title":"After"}}]',
    } });
    fireEvent.click(screen.getByRole("button", { name: "Save sequence" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith(9, [
      { function: "google_calendar.event.create", arguments: { title: "After" } },
    ]));
  });

  it("starts the primary conversation from its first message and preserves failed messages", async () => {
    mockAssistant();
    const session: AgentSession = { id: 41, agent_id: "assistant", title: "Assistant", origin: "web", status: "active", wecom_user_id: null, created_at: "2026-10-16T12:00:00Z", updated_at: "2026-10-16T12:00:00Z", turns: [] };
    const create = vi.spyOn(api, "createAgentSession").mockResolvedValue(session);
    const run = vi.spyOn(api, "runAgentTurn").mockRejectedValue(new Error("Connection interrupted"));
    vi.spyOn(api, "getAgentSession").mockResolvedValue(session);
    renderAssistant(false);
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toHaveProperty("disabled", false));
    const input = await screen.findByRole("textbox", { name: "Conversation message" });
    fireEvent.change(input, { target: { value: "Help me prepare for tomorrow" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("Connection interrupted");
    expect(create).toHaveBeenCalledWith("assistant");
    expect(run).toHaveBeenCalledWith("assistant", 41, "Help me prepare for tomorrow");
    expect(screen.getByRole("textbox", { name: "Conversation message" })).toHaveProperty("value", "Help me prepare for tomorrow");
  });

  it("opens the primary session and hides quiet activation prompts while keeping useful results", async () => {
    mockAssistant();
    const session: AgentSession = { id: 41, agent_id: "assistant", title: "Primary conversation", origin: "web", status: "active", wecom_user_id: null, created_at: "2026-10-16T12:00:00Z", updated_at: "2026-10-16T12:00:00Z", turns: [{
      id: 9, session_id: 41, agent_id: "assistant", codex_turn_id: null, user_message: "INTERNAL REVIEW PROMPT", assistant_message: "", backend_message_kind: "assistant_daily", assistant_context_json: { kind: "daily" },
      status: "completed", activity_json: [], error_message: null, cancel_requested_at: null,
      delivery_message_thread_id: null, delivery_status: null, started_at: null, completed_at: null, created_at: "2026-10-16T12:00:00Z",
    }, {
      id: 10, session_id: 41, agent_id: "assistant", codex_turn_id: null, user_message: "INTERNAL QUESTION PROMPT", assistant_message: "How did your meeting go?", backend_message_kind: "assistant_agenda",
      status: "completed", activity_json: [], error_message: null, cancel_requested_at: null,
      delivery_message_thread_id: null, delivery_status: null, started_at: null, completed_at: null, created_at: "2026-10-16T12:01:00Z",
    }] };
    vi.mocked(api.getAssistantRuntime).mockResolvedValue({ ...runtime, primary_session_id: 41 });
    vi.mocked(api.listAgentSessions).mockResolvedValue([{ ...session, id: 42, title: "Another conversation" }, session]);
    const getSession = vi.spyOn(api, "getAgentSession").mockResolvedValue(session);
    const create = vi.spyOn(api, "createAgentSession");
    const { container } = renderAssistant(false);
    await screen.findByText("How did your meeting go?");
    expect(getSession).toHaveBeenCalledWith("assistant", 41);
    expect(screen.queryByText(/INTERNAL .* PROMPT/)).toBeNull();
    expect(container.querySelector(".chat-sidebar")).toBeNull();
    expect(screen.queryByRole("button", { name: "Open Assistant conversation" })).toBeNull();
    expect(create).not.toHaveBeenCalled();
  });

  it("creates questions with a local-time activation and sends revision-safe edits", async () => {
    const item: AssistantAgendaItem = { id: 7, kind: "question", question_category: "common", title: "What matters about research?", notes: "Explore motivation", status: "open", due_at: "2026-10-20T14:00:00Z", revision: 3, source_session_id: null, last_turn_id: null, created_at: "2026-10-16T12:00:00Z", updated_at: "2026-10-16T12:00:00Z" };
    mockAssistant([item]);
    const create = vi.spyOn(api, "createAssistantAgendaItem").mockResolvedValue(item);
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue(item);
    renderAssistant();
    fireEvent.click(await screen.findByRole("button", { name: "Questions" }));
    expect(screen.getByText("Asked today: 0 (America/Toronto).")).toBeTruthy();

    await screen.findByText(item.title);
    expect(screen.getByText("Common question")).toBeTruthy();
    expect(screen.queryByText(/two questions per day/i)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add question" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "What did you enjoy?" } });
    fireEvent.change(screen.getByLabelText("Question category"), { target: { value: "temporal" } });
    const localDue = "2026-10-21T10:30";
    fireEvent.change(screen.getByLabelText(/^Ask at \(/), { target: { value: localDue } });
    fireEvent.change(screen.getByLabelText("Context and why it matters"), { target: { value: "Follow up on the experience" } });
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith({ kind: "question", question_category: "temporal", title: "What did you enjoy?", notes: "Follow up on the experience", status: "open", due_at: new Date(localDue).toISOString() }));
    await waitFor(() => expect(screen.queryByRole("form", { name: "Agenda item editor" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: new RegExp(item.title) }));
    fireEvent.click(screen.getByRole("button", { name: "Edit question" }));
    fireEvent.change(screen.getByLabelText("Question category"), { target: { value: "important" } });
    fireEvent.change(screen.getByLabelText(/^Ask at \(/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(7, expect.objectContaining({ expected_revision: 3, question_category: "important", due_at: null })));
  });

  it("preserves conflicting question edits and confirms deletion in an Eidolon modal", async () => {
    const item: AssistantAgendaItem = { id: 7, kind: "question", question_category: "common", title: "Open question", notes: "", status: "open", due_at: null, revision: 3, source_session_id: null, last_turn_id: null, created_at: "2026-10-16T12:00:00Z", updated_at: "2026-10-16T12:00:00Z" };
    mockAssistant([item]);
    vi.spyOn(api, "updateAssistantAgendaItem").mockRejectedValue(new Error("Item changed. Reload before editing."));
    const remove = vi.spyOn(api, "deleteAssistantAgendaItem").mockResolvedValue(undefined);
    renderAssistant();
    fireEvent.click(await screen.findByRole("button", { name: "Questions" }));
    fireEvent.click(screen.getByRole("button", { name: new RegExp(item.title) }));
    fireEvent.click(screen.getByRole("button", { name: "Edit question" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "My revised question" } });
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await screen.findByText("Item changed. Reload before editing.");
    expect(screen.getByLabelText("Title")).toHaveProperty("value", "My revised question");
    fireEvent.click(screen.getByRole("button", { name: "Cancel edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete question: Open question" }));
    expect(screen.getByRole("dialog", { name: "Delete this question?" })).toBeTruthy();
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete item" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(7, 3));
  });

  it("saves proactive settings while schedules remain in the agenda", async () => {
    mockAssistant();
    const save = vi.spyOn(api, "updateAssistantRuntime").mockResolvedValue({ ...runtime, timezone: "Europe/London" });
    renderAssistant();
    fireEvent.click(screen.getByRole("link", { name: "Settings" }));
    await screen.findByText("Proactive Assistant and session");
    fireEvent.change(screen.getByLabelText("Assistant timezone"), { target: { value: "Europe/London" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Assistant settings" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ enabled: true, timezone: "Europe/London" }));
    expect(screen.queryByRole("button", { name: "Review now" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Write summary now" })).toBeNull();
    expect(screen.getByText(/Morning planning, evening reflection, and other scheduled tasks are managed in the agenda/)).toBeTruthy();
  });
});
