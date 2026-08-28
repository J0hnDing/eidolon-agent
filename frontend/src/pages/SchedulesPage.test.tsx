// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
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
    expect(screen.getByText("Eidolon backend")).toBeTruthy();
    expect(screen.getByText("Managed by Eidolon")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete" })).toBeNull());
  });
});
