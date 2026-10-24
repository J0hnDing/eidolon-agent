// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DailyMemorySummary, api } from "../api/client";
import { MemoryHistory, MemorySummaries } from "./MemoryArchive";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const summary: DailyMemorySummary = {
  day: "2026-10-19", summary: "Prepared interview notes.\nA complete saved decision and its context.",
  path: "memory/daily-summaries/2026-10-19.md", source_turn_id: 1,
};
const dayLabel = new Date("2026-10-19T12:00:00Z").toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });

describe("memory detail browsing", () => {
  it("opens a complete summary and its source in a dismissible dialog", async () => {
    vi.spyOn(api, "listDailyMemorySummaries").mockResolvedValue({ items: [summary], total: 1, offset: 0, limit: 20 });
    render(<MemorySummaries />);
    const row = await screen.findByRole("button", { name: `Read summary for ${dayLabel}` });
    row.focus();
    fireEvent.click(row);
    const dialog = screen.getByRole("dialog", { name: `Daily summary · ${dayLabel}` });
    expect(within(dialog).getByText(/A complete saved decision and its context/)).toBeTruthy();
    expect(within(dialog).getByText(summary.path)).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement?.getAttribute("aria-label")).toBe(`Read summary for ${dayLabel}`);
  });

  it("searches older archive pages and combines search with the month filter", async () => {
    const recent = Array.from({ length: 100 }, (_, index) => ({ ...summary, day: new Date(Date.UTC(2026, 9, 19 - index)).toISOString().slice(0, 10), summary: `Recent decision ${index}` }));
    const older = { ...summary, day: "2026-09-01", summary: "Older scholarship decision" };
    const items = [...recent, older];
    const list = vi.spyOn(api, "listDailyMemorySummaries").mockImplementation(async (offset = 0, limit = 20) => ({ items: items.slice(offset, offset + limit), total: items.length, offset, limit }));
    render(<MemorySummaries />);
    await screen.findByText("Recent decision 0");
    fireEvent.change(screen.getByLabelText("Search daily summaries"), { target: { value: "scholarship" } });
    expect(await screen.findByText("Older scholarship decision")).toBeTruthy();
    expect(list).toHaveBeenCalledWith(100, 100);
    fireEvent.change(screen.getByLabelText("Filter summaries by month"), { target: { value: "2026-10" } });
    expect(screen.getByText("No daily summaries match these filters.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Filter summaries by month"), { target: { value: "2026-09" } });
    expect(screen.getByText("Older scholarship decision")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("Recent decision 0")).toBeTruthy();
  });

  it("reports a failed archive search without claiming no matches and retries", async () => {
    const list = vi.spyOn(api, "listDailyMemorySummaries").mockImplementation(async (offset = 0, limit = 20) => {
      if (limit === 100) throw new Error("Archive temporarily unavailable");
      return { items: [summary], total: 1, offset, limit };
    });
    render(<MemorySummaries />);
    await screen.findByRole("button", { name: `Read summary for ${dayLabel}` });
    fireEvent.change(screen.getByLabelText("Search daily summaries"), { target: { value: "interview" } });
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Archive temporarily unavailable");
    expect(screen.queryByText("No daily summaries match these filters.")).toBeNull();
    list.mockResolvedValue({ items: [summary], total: 1, offset: 0, limit: 100 });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: `Read summary for ${dayLabel}` })).toBeTruthy();
  });

  it("clearly scopes transcript search to the selected page without changing date or agent", async () => {
    vi.spyOn(api, "listMemoryChatDays").mockResolvedValue({ items: ["2026-10-19"], total: 1, offset: 0, limit: 30 });
    const history = vi.spyOn(api, "listMemoryChatHistory").mockResolvedValue({
      items: [
        { agent: "act", role: "assistant", text: "Interview plan", timestamp: "2026-10-19T12:00:00Z", session_id: 1, turn_id: 1, source: "turn", source_id: 1 },
        { agent: "assistant", role: "user", text: "Library hours", timestamp: "2026-10-19T13:00:00Z", session_id: 2, turn_id: 2, source: "turn", source_id: 2 },
      ], total: 2, offset: 0, limit: 50,
    });
    render(<MemoryHistory />);
    await screen.findByText("Interview plan");
    fireEvent.change(screen.getByLabelText("Search messages on this page"), { target: { value: "library" } });
    expect(screen.queryByText("Interview plan")).toBeNull();
    expect(screen.getByText("Library hours")).toBeTruthy();
    expect(screen.getByText(/1 matching message on this page/)).toBeTruthy();
    await waitFor(() => expect(history).toHaveBeenCalledTimes(1));
  });
});
