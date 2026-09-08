// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import SchedulesPage from "./SchedulesPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SchedulesPage", () => {
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

  it("uses the assessment controls and opens its editable schedule popup", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([{
      id: -2,
      schedule_kind: "platform",
      service_id: "backend.assistant.assessment",
      read_only: false,
      skill_enabled: null,
      is_running: false,
      skill_id: null,
      skill_name: null,
      name: "Assistant assessment",
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
    const update = vi.spyOn(api, "updateAssistantAssessment").mockResolvedValue({
      enabled: true,
      next_run_at: null,
      last_run_at: null,
      last_status: null,
    });

    render(<MemoryRouter><SchedulesPage /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: "Enable" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(true));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("dialog", { name: "Edit Service Schedule" })).toBeTruthy();
    expect((screen.getByLabelText("Every") as HTMLInputElement).value).toBe("3");
    expect((screen.getByLabelText("Type") as HTMLSelectElement).value).toBe("interval");
    expect(screen.queryByRole("option", { name: "daily" })).toBeNull();
    expect(screen.queryByRole("option", { name: "weekly" })).toBeNull();
    expect(screen.getByRole("button", { name: "Run Now" })).not.toHaveProperty("disabled", true);
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
    const dialog = screen.getByRole("dialog", { name: "Edit Service Schedule" });
    expect(dialog.querySelector("form")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
