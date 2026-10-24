// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantAgendaItem, AssistantRuntime, api } from "../api/client";
import MemoryPage from "./MemoryPage";

const runtime: AssistantRuntime = {
  initialization: { status: "pending", reviewed_batches: 0, remaining: null, error: null },
  question_asking: { asked_today: 0, timezone: "America/Toronto" },
  enabled: true,
  memory_root: "C:/Users/John/.eidolon/agents/memory",
  timezone: "America/Toronto",
  primary_session_id: null,
  change_tracking: { pending_count: 0, sources: [] },
};

beforeEach(() => {
  vi.spyOn(api, "listMemoryFacts").mockResolvedValue([]);
  vi.spyOn(api, "getAssistantRuntime").mockResolvedValue(runtime);
  vi.spyOn(api, "listAssistantAgenda").mockResolvedValue([]);
  vi.spyOn(api, "listDailyMemorySummaries").mockResolvedValue({ items: [], total: 0, offset: 0, limit: 20 });
  vi.spyOn(api, "listMemoryChatDays").mockResolvedValue({ items: [], total: 0, offset: 0, limit: 30 });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MemoryPage", () => {
  it("organizes current responsibilities, questions, and recent summaries on the overview", async () => {
    vi.mocked(api.listAssistantAgenda).mockResolvedValue([
      agendaItem(1, "task", "Prepare interview"), agendaItem(2, "question", "Preferred interview time?"),
      { ...agendaItem(3, "question", "Already answered"), status: "done" },
    ]);
    vi.mocked(api.listDailyMemorySummaries).mockResolvedValue({
      items: [{ day: "2026-10-19", summary: "Prepared interview notes.", path: "/memory/daily-summaries/2026-10-19.md", source_turn_id: 1 }], total: 1, offset: 0, limit: 3,
    });
    render(<MemoryPage />);
    expect(await screen.findByText("Prepare interview")).toBeTruthy();
    expect(screen.getByText("Preferred interview time?")).toBeTruthy();
    expect(screen.getByText("Prepared interview notes.")).toBeTruthy();
    expect(screen.queryByText("Already answered")).toBeNull();
    expect(screen.getByRole("navigation", { name: "Memory sections" }).children.length).toBe(6);
  });

  it("shows the whole question list and resolves questions through the shared agenda", async () => {
    const question = agendaItem(2, "question", "Preferred interview time?");
    vi.mocked(api.listAssistantAgenda).mockResolvedValue([
      agendaItem(1, "task", "Prepare interview"), question,
      { ...agendaItem(3, "question", "Answered question"), status: "done", notes: "Answer saved in Atlas." },
    ]);
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue({ ...question, status: "done" });
    render(<MemoryPage />);
    await screen.findByText(question.title);
    fireEvent.click(screen.getByRole("button", { name: "Questions" }));
    expect(screen.queryByText("Prepare interview")).toBeNull();
    expect(screen.queryByText("Answered question")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Past" }));
    expect(screen.getByText("Answer saved in Atlas.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Current" }));
    fireEvent.click(screen.getByRole("button", { name: `Open ${question.title}` }));
    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(2, { expected_revision: 4, status: "done" }));
  });

  it("browses older daily summaries through pagination", async () => {
    vi.mocked(api.listDailyMemorySummaries).mockImplementation(async (offset = 0, limit = 20) => ({
      items: [{ day: offset ? "2026-09-01" : "2026-10-19", summary: offset ? "Older saved decision" : "Recent saved decision", path: "memory/daily-summaries/day.md", source_turn_id: null }],
      total: 21, offset, limit,
    }));
    render(<MemoryPage />);
    fireEvent.click(screen.getByRole("button", { name: "Daily summaries" }));
    await screen.findByText("Recent saved decision");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Older saved decision")).toBeTruthy();
    expect(api.listDailyMemorySummaries).toHaveBeenCalledWith(20);
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Recent saved decision")).toBeTruthy();
  });

  it("filters retained transcripts by date and agent", async () => {
    vi.mocked(api.listMemoryChatDays).mockResolvedValue({ items: ["2026-10-19", "2026-10-18"], total: 2, offset: 0, limit: 30 });
    const history = vi.spyOn(api, "listMemoryChatHistory").mockImplementation(async (day, agent, offset = 0) => ({
      items: [{ agent: "act", role: "assistant", text: `Conversation ${day} ${agent || "all"}`, timestamp: `${day}T10:00:00Z`, session_id: 1, turn_id: 2, source: "turn", source_id: 2 }],
      total: 1, offset, limit: 50,
    }));
    render(<MemoryPage />);
    fireEvent.click(screen.getByRole("button", { name: "Chat history" }));
    expect(await screen.findByText("Conversation 2026-10-19 all")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "act" } });
    expect(await screen.findByText("Conversation 2026-10-19 act")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Conversation date (UTC)"), { target: { value: "2026-10-18" } });
    expect(await screen.findByText("Conversation 2026-10-18 act")).toBeTruthy();
    expect(history).toHaveBeenLastCalledWith("2026-10-18", "act", 0);
  });

  it("keeps other memory available when a source fails and supports retry", async () => {
    vi.mocked(api.listAssistantAgenda).mockRejectedValueOnce(new Error("Offline"));
    render(<MemoryPage />);
    expect(await screen.findByText("Agenda and questions could not be loaded.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Facts outside Atlas" }));
    expect(screen.getByRole("button", { name: "Add fact" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry loading memory" }));
    await waitFor(() => expect(screen.queryByText("Agenda and questions could not be loaded.")).toBeNull());
  });
  it("distinguishes inferred understanding from stated memory and shows its evidence", async () => {
    vi.spyOn(api, "listMemoryFacts").mockResolvedValue([{
      id: 1, key: "meeting-preparation", value: "May prefer concise preparation notes", category: "facts",
      source_message_id: null, sensitivity: "normal", expires_at: null, user_editable: true,
      created_at: "2026-10-16T12:00:00Z", updated_at: "2026-10-16T12:00:00Z",
      provenance_json: { attribution: "inferred", evidence: "Asked to shorten preparation notes twice", updated_at: "2026-10-16T12:00:00Z" },
    }]);
    vi.spyOn(api, "getAssistantRuntime").mockResolvedValue(runtime);
    render(<MemoryPage />);
    fireEvent.click(screen.getByRole("button", { name: "Facts outside Atlas" }));
    await screen.findByText("Hypothesis (inferred)");
    expect(screen.queryByText("Asked to shorten preparation notes twice")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View fact: meeting-preparation" }));
    expect(screen.getByText("Asked to shorten preparation notes twice")).toBeTruthy();
  });

  it("opens the backend-owned agent workspace from storage details", async () => {
    vi.spyOn(api, "listMemoryFacts").mockResolvedValue([]);
    vi.spyOn(api, "getAssistantRuntime").mockResolvedValue(runtime);
    const open = vi.spyOn(api, "openActRoot").mockResolvedValue(undefined);

    render(<MemoryPage />);
    fireEvent.click(screen.getByText("Storage and memory folders"));
    fireEvent.click(screen.getByRole("button", { name: "Open agent workspace" }));

    await waitFor(() => expect(open).toHaveBeenCalledOnce());
  });

  it("combines evidence search and source filtering, and preserves the full fact when editing fails", async () => {
    const fact = {
      id: 8, key: "preparation", value: "May prefer concise notes", category: "facts" as const,
      source_message_id: 27, sensitivity: "normal", expires_at: "2026-12-01T12:00:00Z", user_editable: true,
      created_at: "2026-10-16T12:00:00Z", updated_at: "2026-10-16T12:00:00Z",
      provenance_json: { attribution: "inferred" as const, evidence: "Asked twice to shorten interview notes", updated_at: "2026-10-16T12:00:00Z" },
    };
    vi.mocked(api.listMemoryFacts).mockResolvedValue([fact, { ...fact, id: 9, key: "observed preference", provenance_json: { ...fact.provenance_json, attribution: "observed" } }]);
    const update = vi.spyOn(api, "updateMemoryFact").mockRejectedValue(new Error("Memory unavailable"));
    render(<MemoryPage />);
    fireEvent.click(screen.getByRole("button", { name: "Facts outside Atlas" }));
    await screen.findByText("preparation");
    fireEvent.change(screen.getByLabelText("Search facts"), { target: { value: "interview" } });
    fireEvent.change(screen.getByLabelText("Filter facts by source"), { target: { value: "inferred" } });
    expect(screen.queryByText("observed preference")).toBeNull();
    expect(screen.queryByText(fact.provenance_json.evidence)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View fact: preparation" }));
    expect(screen.getByText(fact.provenance_json.evidence)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit preparation" }));
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "Prefers short interview notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Memory unavailable");
    expect(screen.getByLabelText("Value")).toHaveProperty("value", "Prefers short interview notes");
    expect(update).toHaveBeenCalledWith(8, {
      key: "preparation", value: "Prefers short interview notes", category: "facts", source_message_id: 27,
      sensitivity: "normal", expires_at: "2026-12-01T12:00:00Z", user_editable: true,
    });
  });

  it("keeps an unsaved fact draft when the editor closes and recovers a failed save", async () => {
    const create = vi.spyOn(api, "createMemoryFact").mockRejectedValueOnce(new Error("Memory is temporarily unavailable"));
    render(<MemoryPage />);
    await waitFor(() => expect(api.listMemoryFacts).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Facts outside Atlas" }));
    fireEvent.click(screen.getByRole("button", { name: "Add fact" }));
    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "library" } });
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "Closes at six" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add fact" }));
    expect((screen.getByLabelText("Key") as HTMLInputElement).value).toBe("library");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Memory is temporarily unavailable");
    expect((screen.getByLabelText("Value") as HTMLTextAreaElement).value).toBe("Closes at six");
    expect(create).toHaveBeenCalledOnce();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("searches facts and requires an explicit deletion confirmation", async () => {
    vi.mocked(api.listMemoryFacts).mockResolvedValue([
      { id: 1, key: "library hours", value: "Closes at six", category: "facts", source_message_id: null, sensitivity: "normal", expires_at: null, user_editable: true, created_at: "2026-10-20T12:00:00Z", updated_at: "2026-10-20T12:00:00Z", provenance_json: null },
      { id: 2, key: "coffee", value: "Black, no sugar", category: "facts", source_message_id: null, sensitivity: "normal", expires_at: null, user_editable: true, created_at: "2026-10-20T12:00:00Z", updated_at: "2026-10-20T12:00:00Z", provenance_json: null },
    ]);
    const remove = vi.spyOn(api, "deleteMemoryFact").mockResolvedValue(undefined);
    render(<MemoryPage />);
    fireEvent.click(screen.getByRole("button", { name: "Facts outside Atlas" }));
    await screen.findByText("coffee");
    fireEvent.change(screen.getByLabelText("Search facts"), { target: { value: "six" } });
    expect(screen.queryByText("coffee")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "View fact: library hours" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete fact outside Atlas: library hours" }));
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Keep fact" }));
    expect(screen.getByRole("dialog", { name: "library hours" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete fact outside Atlas: library hours" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete fact" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(1));
  });

  it("shows the memory layout and edits only a generic fact outside Atlas", async () => {
    vi.spyOn(api, "listMemoryFacts").mockResolvedValue([]);
    vi.spyOn(api, "getAssistantRuntime").mockResolvedValue(runtime);
    const create = vi.spyOn(api, "createMemoryFact").mockResolvedValue({
      id: 1, key: "local library hours", value: "The library closes at 6 PM", category: "facts",
      source_message_id: null, sensitivity: "normal", expires_at: null, user_editable: true,
      created_at: "2026-10-17T12:00:00Z", updated_at: "2026-10-17T12:00:00Z", provenance_json: null,
    });

    render(<MemoryPage />);
    expect(await screen.findByText(/Workspace memory folder:/)).toBeTruthy();
    expect(screen.getByText("C:/Users/John/.eidolon/agents/memory")).toBeTruthy();
    expect(screen.getByText(/memory\/daily-summaries\//)).toBeTruthy();
    expect(screen.getByText(/memory\/chat-history\/assistant\//)).toBeTruthy();
    expect(screen.queryByLabelText("Category")).toBeNull();
    expect(screen.queryByLabelText("Sensitivity")).toBeNull();
    expect(screen.queryByLabelText("User editable")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Facts outside Atlas" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add fact" }));
    expect(screen.getByRole("dialog", { name: "Add fact outside Atlas" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "local library hours" } });
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "The library closes at 6 PM" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith({
      key: "local library hours", value: "The library closes at 6 PM", category: "facts",
      source_message_id: null, sensitivity: "normal", expires_at: null, user_editable: true,
    }));
  });
});

function agendaItem(id: number, kind: "task" | "question", title: string): AssistantAgendaItem {
  return { id, kind, title, question_category: "common", notes: "", status: "open", due_at: null, revision: 4,
    source_session_id: null, last_turn_id: null, created_at: "2026-10-19T10:00:00Z", updated_at: "2026-10-19T10:00:00Z" };
}
