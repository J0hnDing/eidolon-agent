// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SkillSchedule, api } from "../api/client";
import SchedulesPage from "./SchedulesPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AutomationsPage", () => {
  it("shows a clear empty state when no automations are scheduled", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([]);

    render(<MemoryRouter><SchedulesPage /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "Automations" })).toBeTruthy();
    expect(screen.getByText("No automations are scheduled yet.")).toBeTruthy();
  });

  it("combines automation source, search, and backend availability filters", async () => {
    const platform: SkillSchedule = {
      id: -3, schedule_kind: "platform", service_id: "backend.cleanup", read_only: false,
      skill_enabled: null, is_running: false, skill_id: null, skill_name: null,
      name: "System cleanup", status: "paused", schedule_type: "daily",
      schedule_json: { type: "daily", time: "08:00", timezone: "UTC", input: {} }, input_json: {},
      timezone: "UTC", next_run_at: null, last_run_at: null, last_run_status: null,
      created_at: "2026-10-21T00:00:00Z", updated_at: "2026-10-21T00:00:00Z",
    };
    vi.spyOn(api, "listSchedules").mockResolvedValue([
      platform,
      { ...platform, id: 8, schedule_kind: "service", skill_id: 4, skill_name: "weekly_brief", name: "Weekly brief", skill_enabled: true },
    ]);
    render(<MemoryRouter><SchedulesPage /></MemoryRouter>);
    expect(await screen.findByText("System Cleanup")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Skill automations 1" }));
    expect(screen.queryByText("System Cleanup")).toBeNull();
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "active" } });
    expect(screen.getByRole("link", { name: "Weekly Brief" })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search automations" }), { target: { value: "unknown" } });
    expect(screen.getByRole("heading", { name: "No automations match these filters" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByText("System Cleanup")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Weekly Brief" })).toBeTruthy();
  });

  it("shows controls for every backend-owned platform schedule", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([
      {
        id: 0,
        schedule_kind: "platform",
        service_id: "backend.notion.todo.cleanup_done",
        read_only: false,
        skill_enabled: null,
        is_running: true,
        skill_id: null,
        skill_name: null,
        name: "Daily Notion Done Cleanup",
        status: "active",
        schedule_type: "daily",
        schedule_json: {
          type: "daily",
          time: "03:00",
          timezone: "America/Toronto",
          input: {},
        },
        input_json: {},
        timezone: "America/Toronto",
        next_run_at: "2026-08-27T07:00:00Z",
        last_run_at: null,
        last_run_status: null,
        created_at: "2026-08-26T12:00:00Z",
        updated_at: "2026-08-26T12:00:00Z",
      },
      {
        id: -1,
        schedule_kind: "platform",
        service_id: "backend.quercus.knowledge.sync",
        read_only: false,
        skill_enabled: null,
        is_running: false,
        skill_id: null,
        skill_name: null,
        name: "Daily Quercus Knowledge Sync",
        status: "active",
        schedule_type: "daily",
        schedule_json: {
          type: "daily",
          time: "10:00",
          timezone: "America/Toronto",
          input: {},
        },
        input_json: {},
        timezone: "America/Toronto",
        next_run_at: "2026-08-27T14:00:00Z",
        last_run_at: null,
        last_run_status: null,
        created_at: "2026-08-26T12:00:00Z",
        updated_at: "2026-08-26T12:00:00Z",
      },
    ]);

    render(
      <MemoryRouter>
        <SchedulesPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Daily Notion Done Cleanup")).toBeTruthy();
    expect(screen.getByText("Daily Quercus Knowledge Sync")).toBeTruthy();
    expect(screen.getByLabelText("Running").getAttribute("title")).toBe("Running");
    expect(screen.getAllByRole("button", { name: "Disable" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Run Now" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(2);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete" })).toBeNull());
  });

  it("uses ordinary platform controls and opens its editable schedule popup", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([{
      id: -2,
      schedule_kind: "platform",
      service_id: "backend.example.maintenance",
      read_only: false,
      skill_enabled: null,
      is_running: false,
      skill_id: null,
      skill_name: null,
      name: "Example maintenance",
      status: "paused",
      schedule_type: "interval",
      schedule_json: { type: "interval", every: 3, unit: "days", timezone: "UTC", input: {} },
      input_json: {},
      timezone: "UTC",
      next_run_at: null,
      last_run_at: null,
      last_run_status: null,
      created_at: "2026-08-26T12:00:00Z",
      updated_at: "2026-08-26T12:00:00Z",
    }]);
    const update = vi.spyOn(api, "updatePlatformScheduleAvailability").mockResolvedValue({} as never);

    render(<MemoryRouter><SchedulesPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: "Enable" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("backend.example.maintenance", true));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("dialog", { name: "Edit automation" })).toBeTruthy();
    expect((screen.getByLabelText("Every") as HTMLInputElement).value).toBe("3");
    expect((screen.getByLabelText("Type") as HTMLSelectElement).value).toBe("interval");
    expect(screen.getByRole("option", { name: "daily" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "weekly" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run Now" })).toHaveProperty("disabled", true);
  });

  it("shows the last-run status only as a tooltip dot before the timestamp", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([
      {
        id: 1,
        schedule_kind: "service",
        service_id: "weekly_report_service",
        read_only: false,
        skill_enabled: true,
        is_running: true,
        skill_id: 8,
        skill_name: "weekly_report_service",
        name: "weekly_report_service schedule",
        status: "active",
        schedule_type: "weekly",
        schedule_json: {
          type: "weekly",
          day: "monday",
          time: "08:00",
          timezone: "America/Toronto",
          input: {},
        },
        input_json: {},
        timezone: "America/Toronto",
        next_run_at: null,
        last_run_at: "2026-08-31T12:01:00Z",
        last_run_status: "failed",
        created_at: "2026-08-28T12:00:00Z",
        updated_at: "2026-08-31T12:01:00Z",
      },
    ]);

    render(
      <MemoryRouter>
        <SchedulesPage />
      </MemoryRouter>,
    );

    const statusDot = await screen.findByLabelText("Last run status: failed");
    const runningDot = screen.getByLabelText("Running");
    expect(runningDot.classList.contains("running-state-dot")).toBe(true);
    expect(runningDot.closest("td")?.cellIndex).toBe(2);
    expect(runningDot.closest("td")?.textContent).toBe("running");
    expect(runningDot.closest("tr")?.firstElementChild?.querySelector(".running-state-dot")).toBeNull();
    expect(statusDot.getAttribute("title")).toBe("failed");
    expect(statusDot.textContent).toBe("");
    expect(statusDot.parentElement?.firstElementChild).toBe(statusDot);
    const scheduleTable = statusDot.closest("table");
    expect(scheduleTable).toBeTruthy();
    expect(scheduleTable?.textContent).toContain("Weekly on Monday at 08:00");
    expect(scheduleTable?.querySelector("tbody tr td:nth-child(3)")?.textContent).toBe("running");
    expect(screen.queryByText("enabled")).toBeNull();
    expect(screen.getByRole("button", { name: "Disable" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Run Now" })).not.toHaveProperty("disabled", true);
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(scheduleTable?.textContent).not.toMatch(/EDT|EST|GMT[+-]\d+|America\/Toronto/);
    expect(Array.from(scheduleTable?.querySelectorAll("col") ?? [], (column) => column.className)).toEqual([
      "schedule-column-service",
      "schedule-column-recurrence",
      "schedule-column-status",
      "schedule-column-next-run",
      "schedule-column-last-run",
      "schedule-column-actions",
    ]);

    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog", { name: "Edit automation" });
    expect(dialog.querySelector("form")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
