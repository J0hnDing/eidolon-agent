// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SkillRun, SkillVersion } from "../../api/client";
import {
  RunHistory,
  RunOutput,
  SkillFilesDisclosure,
  VersionList,
  parseRunInput,
} from "./SkillDetailPanels";

describe("SkillDetailPanels", () => {
  it("keeps run-input validation behavior at the feature boundary", () => {
    expect(parseRunInput(" ")).toEqual({});
    expect(parseRunInput('{"hello":"world"}')).toEqual({ hello: "world" });
    expect(() => parseRunInput("{hello: world}")).toThrow("Run input must be valid JSON");
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

  it("presents version validation and tests as accessible status dots", () => {
    render(
      <VersionList
        versions={[makeVersion()]}
        activeVersionId={null}
        isWorking={false}
        onCompare={vi.fn()}
        onActivate={vi.fn()}
        onDiscard={vi.fn()}
      />,
    );

    const validation = screen.getByLabelText("Validation: passed");
    const tests = screen.getByLabelText("Tests: passed");
    expect(validation.classList.contains("status-passed")).toBe(true);
    expect(tests.classList.contains("status-passed")).toBe(true);
    expect(validation.textContent).toBe("Validation");
    expect(tests.textContent).toBe("Tests");
    expect(screen.getByRole("button", { name: "Delete version v2" })).toBeTruthy();
  });

  it("shows file names while keeping each file content closed behind its own chevron", () => {
    const { container } = render(
      <SkillFilesDisclosure files={[
        { path: "skill.py", content: "print('ready')" },
        { path: "tests/test_skill.py", content: "def test_ready(): pass" },
      ]} />,
    );

    const disclosures = container.querySelectorAll(".skill-file-disclosure");
    expect(screen.getByText("skill.py")).toBeTruthy();
    expect(screen.getByText("tests/test_skill.py")).toBeTruthy();
    expect(disclosures).toHaveLength(2);
    expect([...disclosures].every((disclosure) => !disclosure.hasAttribute("open"))).toBe(true);
    expect(container.querySelectorAll(".skill-file-chevron")).toHaveLength(2);
    fireEvent.click(screen.getByText("skill.py"));
    expect(disclosures[0].hasAttribute("open")).toBe(true);
    expect(disclosures[1].hasAttribute("open")).toBe(false);
  });

  it("shows the current output and collapses each historical run independently", () => {
    const runs = [
      makeRun(2, { report: "new result" }),
      makeRun(1, { report: "older result" }),
    ];

    const { container, rerender } = render(<RunOutput run={runs[0]} />);
    expect(screen.getByLabelText("Output for run 2").textContent).toContain('"new result"');

    rerender(<RunHistory runs={runs} />);
    expect(screen.getByText("Run #2")).toBeTruthy();
    expect(screen.getByText("Run #1")).toBeTruthy();
    expect(screen.getAllByText(/new result/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/older result/).length).toBeGreaterThan(0);
    const disclosures = container.querySelectorAll(".run-history-entry");
    expect(disclosures).toHaveLength(2);
    expect([...disclosures].every((disclosure) => !disclosure.hasAttribute("open"))).toBe(true);
    expect(container.querySelectorAll(".run-history-chevron")).toHaveLength(2);
    fireEvent.click(screen.getByText("Run #2"));
    expect(disclosures[0].hasAttribute("open")).toBe(true);
    expect(disclosures[1].hasAttribute("open")).toBe(false);
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

function makeVersion(): SkillVersion {
  return {
    id: 12,
    skill_id: 6,
    version: "v2",
    status: "proposed_update",
    folder_path: "skills/installed/example/versions/v2",
    manifest_json: {},
    code_snapshot_path: "runtime/snapshots/example-v2",
    created_by: "agent",
    parent_version_id: 11,
    permission_fingerprint: "fingerprint",
    test_status: "passed",
    validation_status: "passed",
    change_summary: "Improved output formatting.",
    changelog: null,
    created_at: "2026-09-01T12:00:00Z",
    activated_at: null,
  };
}
