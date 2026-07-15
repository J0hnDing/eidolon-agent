// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SkillSchedule } from "../../api/client";
import { ScheduleList, VersionList, parseRunInput } from "./SkillDetailPanels";

describe("SkillDetailPanels", () => {
  it("keeps run-input validation behavior at the feature boundary", () => {
    expect(parseRunInput(" ")).toEqual({});
    expect(parseRunInput('{"hello":"world"}')).toEqual({ hello: "world" });
    expect(() => parseRunInput("{hello: world}")).toThrow("Run input must be valid JSON");
  });

  it("renders schedule state and delegates allowed actions", () => {
    const onRunNow = vi.fn();
    const schedule = {
      id: 7,
      name: "Daily report",
      status: "active",
      schedule_type: "daily",
      schedule_json: { time: "08:00" },
      timezone: "America/Toronto",
      last_run_at: null,
      last_run_status: null,
    } as SkillSchedule;
    render(
      <ScheduleList
        schedules={[schedule]}
        isWorking={false}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
        onPause={vi.fn()}
        onResume={vi.fn()}
        onDelete={vi.fn()}
        onRunNow={onRunNow}
      />,
    );
    expect(screen.getByText("Daily at 08:00 America/Toronto")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Run Now" }));
    expect(onRunNow).toHaveBeenCalledWith(7);
  });

  it("keeps version empty-state rendering out of the route page", () => {
    render(
      <VersionList
        versions={[]}
        activeVersionId={null}
        isWorking={false}
        onCompare={vi.fn()}
        onActivate={vi.fn()}
        onDiscard={vi.fn()}
      />,
    );
    expect(screen.getByText(/No versions have been initialized/)).toBeTruthy();
  });
});
