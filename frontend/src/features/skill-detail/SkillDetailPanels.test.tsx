// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SkillRun, SkillSchedule } from "../../api/client";
import { RunHistory, RunOutput, ScheduleList, VersionList, parseRunInput } from "./SkillDetailPanels";

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

  it("shows the current output and keeps every historical output visible", () => {
    const runs = [
      makeRun(2, { report: "new result" }),
      makeRun(1, { report: "older result" }),
    ];

    const { rerender } = render(<RunOutput run={runs[0]} />);
    expect(screen.getByLabelText("Output for run 2").textContent).toContain('"new result"');

    rerender(<RunHistory runs={runs} />);
    expect(screen.getByText("Run #2")).toBeTruthy();
    expect(screen.getByText("Run #1")).toBeTruthy();
    expect(screen.getAllByText(/new result/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/older result/).length).toBeGreaterThan(0);
    expect(screen.queryByText("Latest Run")).toBeNull();
  });
});

function makeRun(id: number, output: Record<string, unknown>): SkillRun {
  return {
    id,
    skill_id: 7,
    status: "succeeded",
    input_json: {},
    output_json: output,
    stdout: JSON.stringify(output),
    stderr: null,
    exit_code: 0,
    started_at: "2026-08-19T12:00:00Z",
    ended_at: "2026-08-19T12:00:01Z",
    error_message: null,
    codex_invocations_json: [],
    input_tokens: 0,
    cached_input_tokens: 0,
    output_tokens: 0,
    reasoning_output_tokens: 0,
    total_tokens: 0,
  };
}
