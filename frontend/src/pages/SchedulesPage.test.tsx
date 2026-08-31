// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import SchedulesPage from "./SchedulesPage";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SchedulesPage", () => {
  it("shows the scheduler-owned cleanup as a read-only platform schedule", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([
      {
        id: 0,
        schedule_kind: "platform",
        service_id: "backend.notion.todo.cleanup_done",
        read_only: true,
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
    ]);

    render(
      <MemoryRouter>
        <SchedulesPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Daily Notion Done Cleanup")).toBeTruthy();
    expect(screen.getByText("Managed by Eidolon")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete" })).toBeNull());
  });

  it("shows the last-run status only as a tooltip dot before the timestamp", async () => {
    vi.spyOn(api, "listSchedules").mockResolvedValue([
      {
        id: 1,
        schedule_kind: "service",
        service_id: "weekly_report_service",
        read_only: false,
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
    expect(statusDot.getAttribute("title")).toBe("failed");
    expect(statusDot.textContent).toBe("");
    expect(statusDot.parentElement?.firstElementChild).toBe(statusDot);
    const scheduleTable = statusDot.closest("table");
    expect(scheduleTable).toBeTruthy();
    expect(scheduleTable?.textContent).toContain("Weekly on Monday at 08:00");
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
