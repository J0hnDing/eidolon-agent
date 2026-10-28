// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssistantAgendaItem, AssistantAgendaOccurrence, api } from "../api/client";
import { AssistantAgenda } from "./AssistantAgenda";

const task: AssistantAgendaItem = {
  id: 12, kind: "task", question_category: "common", title: "Evening reflection", notes: "Write the daily summary.",
  status: "open", due_at: "2026-10-21T02:00:00Z", revision: 3, source_session_id: null, last_turn_id: null,
  created_at: "2026-10-20T12:00:00Z", updated_at: "2026-10-20T12:00:00Z",
  recurrence: { frequency: "daily", time: "22:00", timezone: "America/Toronto" },
  last_occurrence: { id: 7, scheduled_at: "2026-10-20T02:00:00Z", status: "succeeded", turn_id: 9, attempts: 1, retry_at: null, completed_at: "2026-10-20T02:01:00Z" },
};
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("agenda history", () => {

  it("shows completed and dismissed one-time items in Past and keeps question actions available", async () => {
    const completedTask: AssistantAgendaItem = {
      ...task, id: 13, title: "Review exam result", notes: "Check how it went.", status: "done", due_at: null,
      recurrence: null, last_occurrence: null,
    };
    const dismissedQuestion: AssistantAgendaItem = {
      ...completedTask, id: 14, kind: "question", question_category: "temporal", title: "Ask how the exam went", status: "dismissed",
    };
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue({ ...completedTask, status: "open" });
    render(<AssistantAgenda items={[completedTask, dismissedQuestion]} onChange={vi.fn().mockResolvedValue(undefined)} />);
    expect(screen.queryByText("Review exam result")).toBeNull();
    expect(screen.getByText("No current tasks.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Past" }));
    expect(screen.getByText("Review exam result")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open Review exam result" }));
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(13, { expected_revision: 3, status: "open" }));
    fireEvent.click(screen.getByRole("button", { name: "Close Review exam result" }));
    fireEvent.click(screen.getByRole("button", { name: "Questions" }));
    expect(screen.getByText("Ask how the exam went")).toBeTruthy();
    expect(screen.getByText("Temporal question")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open Ask how the exam went" }));
    expect(screen.getByRole("button", { name: "Edit question" })).toBeTruthy();
  });

  it("loads and displays the occurrence history for a recurring task in Past", async () => {
    const occurrences: AssistantAgendaOccurrence[] = [
      { id: 9, scheduled_at: "2026-10-21T02:00:00Z", status: "succeeded", turn_id: 11, attempts: 1, retry_at: null, completed_at: "2026-10-21T02:01:00Z" },
      { id: 8, scheduled_at: "2026-10-20T02:00:00Z", status: "failed", turn_id: 10, attempts: 2, retry_at: null, completed_at: "2026-10-20T02:02:00Z" },
    ];
    const list = vi.spyOn(api, "listAssistantAgendaOccurrences").mockResolvedValue(occurrences);
    render(<AssistantAgenda items={[task]} onChange={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "Past" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    fireEvent.click(screen.getByRole("button", { name: "Show occurrence history for Evening reflection" }));
    const history = await screen.findByRole("list", { name: "Past occurrences for Evening reflection" });
    expect(list).toHaveBeenCalledWith(12);
    expect(history.querySelectorAll("li")).toHaveLength(2);
    expect(history.textContent).toContain("succeeded");
    expect(history.textContent).toContain("failed");
  });
});

describe("agenda completion requirements", () => {

  it.each([
    ["interaction_today", "Requires an interaction today"],
    ["morning_plan", "Requires 2 work items, 2 interactions, 1 other item and the morning feed"],
  ] as const)("shows %s and leaves it intact in an edit PATCH", async (completion_requirement, label) => {
    const dailyCheckIn = { ...task, completion_requirement };
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue(dailyCheckIn);
    render(<AssistantAgenda items={[dailyCheckIn]} onChange={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    expect(screen.getByText(label)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Edit task" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Daily planning and check-in" } });
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, expect.objectContaining({ title: "Daily planning and check-in", expected_revision: 3 })));
    expect(update.mock.calls[0][1]).not.toHaveProperty("completion_requirement");
    expect(screen.getByText(label)).toBeTruthy();
  });

  it("shows planned work separately from completion and initiated interactions", () => {
    render(<AssistantAgenda items={[]} dailyPlan={{
      day: "2026-10-25", timezone: "America/Toronto", plan_turn_id: 34,
      required: { work: 2, interaction: 2, other: 1 }, planned: { work: 2, interaction: 2, other: 1 },
      completed: { work: 1, interaction: 0, other: 0 }, planned_satisfied: true, follow_through_satisfied: false,
    }} onChange={vi.fn().mockResolvedValue(undefined)} />);
    const progress = screen.getByRole("status", { name: "Today's Assistant plan" });
    expect(progress.textContent).toContain("Work: 1/2 completed · 2 planned");
    expect(progress.textContent).toContain("Interactions: 0/2 initiated · 2 planned");
    expect(progress.textContent).toContain("Other: 0/1 completed · 1 planned");
  });
});

describe("recurring agenda", () => {

  it.each(["daily", "weekly", "monthly"] as const)("creates a %s task with a timezone and no caller-managed due time", async (frequency) => {
    const create = vi.spyOn(api, "createAssistantAgendaItem").mockResolvedValue(task);
    const onChange = vi.fn().mockResolvedValue(undefined);
    render(<AssistantAgenda items={[]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Add task" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Review work" } });
    fireEvent.click(screen.getByLabelText("Repeat this task"));
    fireEvent.change(screen.getByLabelText("Repeat"), { target: { value: frequency } });
    fireEvent.change(screen.getByLabelText("Scheduled time"), { target: { value: "08:30" } });
    fireEvent.change(screen.getByLabelText("Schedule timezone"), { target: { value: "America/Toronto" } });
    if (frequency === "weekly") fireEvent.click(screen.getByLabelText("Friday"));
    if (frequency === "monthly") {
      fireEvent.change(screen.getByLabelText("Day of month"), { target: { value: "31" } });
      expect(screen.getByText("Months without the selected day are skipped.")).toBeTruthy();
    }
    expect(screen.queryByLabelText(/^Next check/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith({
      kind: "task", question_category: "common", title: "Review work", notes: "", status: "open",
      recurrence: { frequency, time: "08:30", timezone: "America/Toronto", ...(frequency === "weekly" ? { weekdays: [0, 4] } : {}), ...(frequency === "monthly" ? { day_of_month: 31 } : {}) },
    }));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("switches an existing daily task to monthly using the displayed default day", async () => {
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue(task);
    render(<AssistantAgenda items={[task]} onChange={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit task" }));
    fireEvent.change(screen.getByLabelText("Repeat"), { target: { value: "monthly" } });
    expect(screen.getByLabelText("Day of month")).toHaveProperty("value", "1");
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, expect.objectContaining({
      recurrence: { frequency: "monthly", time: "22:00", timezone: "America/Toronto", day_of_month: 1 },
    })));
  });

  it("runs, pauses, and ends an ordinary recurring task using its current revision", async () => {
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue(task);
    const run = vi.spyOn(api, "runAssistantAgendaItem").mockResolvedValue({ status: "queued", turn_id: 10 });
    const onChange = vi.fn().mockResolvedValue(undefined);
    render(<AssistantAgenda items={[task]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    expect(screen.getByText("Daily at 22:00 (America/Toronto) · Active")).toBeTruthy();
    expect(screen.getByText(/Last occurrence:.*succeeded/)).toBeTruthy();
    expect(screen.getByText(/^Next occurrence:/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(run).toHaveBeenCalledWith(12, 3));
    await waitFor(() => expect(screen.getByRole("button", { name: "Pause schedule" })).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByRole("button", { name: "Pause schedule" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, { expected_revision: 3, status: "waiting" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "End recurring task" })).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByRole("button", { name: "End recurring task" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, { expected_revision: 3, status: "done" }));
  });

  it.each([
    ["busy", "The Assistant is busy. Run this task again after the current turn finishes."],
    ["disabled", "Enable proactive Assistant in settings before running this task."],
  ])("explains why Run now did not queue while %s", async (status, message) => {
    vi.spyOn(api, "runAssistantAgendaItem").mockResolvedValue({ status });
    const onChange = vi.fn().mockResolvedValue(undefined);
    render(<AssistantAgenda items={[task]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", message);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Run now" })).toHaveProperty("disabled", false);
  });

  it("resumes a paused schedule without exposing Run now", async () => {
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue(task);
    render(<AssistantAgenda items={[{ ...task, status: "waiting" }]} onChange={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    expect(screen.queryByRole("button", { name: "Run now" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Resume schedule" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, { expected_revision: 3, status: "open" }));
  });

  it("edits a schedule and can remove recurrence", async () => {
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue(task);
    render(<AssistantAgenda items={[task]} onChange={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit task" }));
    fireEvent.change(screen.getByLabelText("Scheduled time"), { target: { value: "21:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, expect.objectContaining({ expected_revision: 3, recurrence: { frequency: "daily", time: "21:00", timezone: "America/Toronto" } })));
    expect(update.mock.calls[0][1]).not.toHaveProperty("due_at");
    await waitFor(() => expect(screen.queryByRole("form", { name: "Agenda item editor" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Edit task" }));
    fireEvent.click(screen.getByLabelText("Repeat this task"));
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    await waitFor(() => expect(update).toHaveBeenLastCalledWith(12, expect.objectContaining({ expected_revision: 3, recurrence: null })));
  });

  it("preserves invalid or conflicting schedule edits and excludes recurrence from questions", async () => {
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockRejectedValue(new Error("Item changed. Reload before editing."));
    render(<AssistantAgenda items={[task]} onChange={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit task" }));
    fireEvent.change(screen.getByLabelText("Schedule timezone"), { target: { value: "Unknown/Zone" } });
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    expect(await screen.findByText(/Enter a valid IANA timezone/)).toBeTruthy();
    expect(update).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Schedule timezone"), { target: { value: "America/Toronto" } });
    fireEvent.click(screen.getByRole("button", { name: "Save item" }));
    expect(await screen.findByText("Item changed. Reload before editing.")).toBeTruthy();
    expect(screen.getByLabelText("Scheduled time")).toHaveProperty("value", "22:00");
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "question" } });
    expect(screen.queryByLabelText("Repeat this task")).toBeNull();
    expect(screen.queryByLabelText("Scheduled time")).toBeNull();
  });
});

describe("agenda list and details", () => {

  it("searches full notes, combines schedule and status filters, and resets an empty result", () => {
    const oneTime = { ...task, id: 30, title: "Prepare proposal", notes: "Include the research budget.", recurrence: null, last_occurrence: null, status: "waiting" as const };
    render(<AssistantAgenda items={[task, oneTime]} onChange={vi.fn().mockResolvedValue(undefined)} compact />);
    expect(screen.queryByRole("button", { name: "Edit task" })).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search agenda" }), { target: { value: "research budget" } });
    expect(screen.getByRole("button", { name: "Open Prepare proposal" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open Evening reflection" })).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "Filter by schedule" }), { target: { value: "recurring" } });
    expect(screen.getByText("No tasks match your filters.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    fireEvent.change(screen.getByRole("combobox", { name: "Filter by status" }), { target: { value: "waiting" } });
    expect(screen.getByRole("button", { name: "Open Prepare proposal" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open Evening reflection" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Prepare proposal" }));
    const detail = screen.getByRole("dialog", { name: "Prepare proposal" });
    expect(within(detail).getByText("Include the research budget.")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("filters question categories and opens full context in its own dialog", () => {
    const question = { ...task, id: 31, kind: "question" as const, question_category: "important" as const, title: "Choose a research direction", notes: "A long context ".repeat(30), recurrence: null, last_occurrence: null };
    render(<AssistantAgenda items={[question, { ...question, id: 32, question_category: "common", title: "Weekend plans" }]} fixedKind="question" onChange={vi.fn().mockResolvedValue(undefined)} />);
    fireEvent.change(screen.getByRole("combobox", { name: "Filter by question category" }), { target: { value: "important" } });
    expect(screen.queryByRole("button", { name: "Open Weekend plans" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Choose a research direction" }));
    expect(screen.getByRole("dialog", { name: question.title }).textContent).toContain(question.notes);
    expect(screen.queryByText("Details & actions")).toBeNull();
    expect(screen.getByRole("button", { name: "Resolve" })).toBeTruthy();
  });

  it("uses refreshed revisions for actions in an open detail and confirms deletion", async () => {
    const update = vi.spyOn(api, "updateAssistantAgendaItem").mockResolvedValue(task);
    const remove = vi.spyOn(api, "deleteAssistantAgendaItem").mockResolvedValue(undefined);
    const onChange = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<AssistantAgenda items={[task]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Open Evening reflection" }));
    rerender(<AssistantAgenda items={[{ ...task, revision: 9 }]} onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(12, { expected_revision: 9, status: "dismissed" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Delete task: Evening reflection" })).toHaveProperty("disabled", false));
    fireEvent.click(screen.getByRole("button", { name: "Delete task: Evening reflection" }));
    expect(remove).not.toHaveBeenCalled();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Delete item" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(12, 9));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
