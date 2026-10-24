// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SkillRun, SkillVersion, SkillVersionComparison } from "../../api/client";
import {
  RunHistory,
  RunOutput,
  SkillFilesDisclosure,
  SkillDetailView,
  SkillDetailViews,
  VersionList,
  VersionComparisonPanel,
  parseRunInput,
} from "./SkillDetailPanels";

afterEach(cleanup);

describe("SkillDetailPanels", () => {
  it("keeps editor drafts mounted across views and supports keyboard navigation", () => {
    function Workspace() {
      const [view, setView] = useState<SkillDetailView>("overview");
      return <SkillDetailViews activeView={view} onViewChange={setView}>{{
        overview: <label>Draft input<input defaultValue="Original input" /></label>,
        versions: <p>Version history</p>, permissions: <p>Runtime access</p>,
        runs: <p>Execution history</p>, files: <p>Package files</p>, settings: <p>Model settings</p>,
      }}</SkillDetailViews>;
    }
    render(<Workspace />);
    const input = screen.getByLabelText("Draft input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Unsaved input" } });
    fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
    expect(screen.getByRole("tabpanel", { name: "Settings" })).toBeTruthy();
    expect(input.closest("[role='tabpanel']")).toHaveProperty("hidden", true);
    fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(screen.getByLabelText("Draft input")).toBe(input);
    expect(input.value).toBe("Unsaved input");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Overview" }), { key: "ArrowRight" });
    expect(screen.getByRole("tabpanel", { name: "Versions" })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Versions" }));
    fireEvent.keyDown(screen.getByRole("tab", { name: "Versions" }), { key: "End" });
    expect(screen.getByRole("tabpanel", { name: "Settings" })).toBeTruthy();
  });

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

  it("searches package files and keeps a single selected source readable", () => {
    render(<SkillFilesDisclosure files={[
      { path: "skill.py", content: "print('ready')" },
      { path: "tests/test_skill.py", content: "def test_ready(): pass" },
    ]} />);
    expect(screen.getByLabelText("Source of skill.py").textContent).toBe("print('ready')");
    expect(screen.queryByText("def test_ready(): pass")).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "Find a file" }), { target: { value: "tests/" } });
    const navigation = screen.getByRole("navigation", { name: "Package files" });
    expect(within(navigation).queryByRole("button", { name: "skill.py" })).toBeNull();
    fireEvent.click(within(navigation).getByRole("button", { name: "tests/test_skill.py" }));
    expect(screen.getByLabelText("Source of tests/test_skill.py").textContent).toBe("def test_ready(): pass");
    fireEvent.change(screen.getByRole("searchbox", { name: "Find a file" }), { target: { value: "missing" } });
    expect(screen.getByText("No matching files.")).toBeTruthy();
    expect(screen.getByLabelText("Source of tests/test_skill.py")).toBeTruthy();
  });

  it("opens run details from a searchable status-filtered list and restores focus on close", () => {
    const runs = [makeRun(2, { report: "new result" }), { ...makeRun(1, { report: "older result" }), status: "failed" as const, error_message: "Provider unavailable" }];
    const { rerender } = render(<RunOutput run={runs[0]} />);
    expect(screen.getByLabelText("Output for run 2").textContent).toContain('"new result"');
    rerender(<RunHistory runs={runs} />);
    expect(screen.queryByText(/new result/)).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), { target: { value: "failed" } });
    expect(screen.queryByRole("button", { name: "Inspect Run #2" })).toBeNull();
    expect(screen.getByRole("button", { name: "Inspect Run #1" })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search runs" }), { target: { value: "unavailable" } });
    const trigger = screen.getByRole("button", { name: "Inspect Run #1" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Run #1" });
    expect(within(dialog).getByRole("heading", { name: "Failure summary" })).toBeTruthy();
    expect(within(dialog).getAllByText(/older result/).length).toBeGreaterThan(0);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search runs" }), { target: { value: "missing" } });
    expect(screen.getByText("No runs match these filters.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("button", { name: "Inspect Run #2" })).toBeTruthy();
  });

  it("inspects called runs within the same dialog and returns to the parent", () => {
    const child = { ...makeRun(3, { child: true }), skill_name: "text_reader" };
    render(<RunHistory runs={[{ ...makeRun(2, {}), child_runs: [child] }]} />);
    fireEvent.click(screen.getByRole("button", { name: "Inspect Run #2" }));
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Inspect Text Reader Run #3" }));
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByRole("dialog", { name: "Text Reader · Run #3" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back to Run #2" }));
    expect(screen.getByRole("dialog", { name: "Run #2" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close Run #2" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("selects comparison files without stacking disclosures and preserves missing-file diagnostics", () => {
    const onClose = vi.fn();
    render(<VersionComparisonPanel comparison={{
      active_version: { ...makeVersion(), id: 11, version: "v1" }, candidate_version: makeVersion(),
      files: [
        { path: "skill.py", active: "old source", candidate: "new source" },
        { path: "new.py", active: null, candidate: "added source" },
      ],
    } as SkillVersionComparison} onClose={onClose} />);
    expect(screen.getByLabelText("Active source of skill.py").textContent).toBe("old source");
    expect(screen.queryByText("added source")).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: "File" }), { target: { value: "new.py" } });
    expect(screen.getByLabelText("Active source of new.py").textContent).toBe("File not present.");
    expect(screen.getByLabelText("Candidate source of new.py").textContent).toBe("added source");
    fireEvent.click(screen.getByRole("button", { name: "Close comparison" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it.each([
    [
      "The Google Calendar authorization is invalid or revoked",
      "Integration authentication/authorization error: The Google Calendar authorization is invalid or revoked",
    ],
    [
      "[invalid_credential] The Google Calendar authorization is invalid or revoked",
      "Integration authentication/authorization error [invalid_credential]: The Google Calendar authorization is invalid or revoked",
    ],
    [
      "[authorization_missing_or_stale] Integration authorization is missing",
      "Integration authentication/authorization error [authorization_missing_or_stale]: Integration authorization is missing",
    ],
    [
      "[rate_limited] Notion rate limited the integration request",
      "Integration error [rate_limited]: Notion rate limited the integration request",
    ],
    [
      "Integration connection or configured resource is unavailable",
      "Integration error: Integration connection or configured resource is unavailable",
    ],
  ])("recognizes historical integration failures: %s", (diagnostic, expected) => {
    const run: SkillRun = {
      ...makeRun(74, {}),
      status: "failed",
      output_json: null,
      error_message: "Skill exited with code 1",
      stderr: `Traceback (most recent call last):\nintegration_runtime_capabilities.IntegrationRuntimeCapabilityError: ${diagnostic}\n`,
      exit_code: 1,
    };
    const { container } = render(<RunHistory runs={[run]} />);
    expect(container.querySelector(".run-history-error-summary")?.textContent).toBe(expected);
    fireEvent.click(screen.getByRole("button", { name: "Inspect Run #74" }));
    expect(container.querySelector(".run-failure-report p")?.textContent).toBe(expected);
  });

  it("keeps an unrelated terminal error from being misclassified as integration auth", () => {
    const run: SkillRun = {
      ...makeRun(75, {}),
      status: "failed",
      error_message: "Skill exited with code 1",
      stderr: "integration_runtime_capabilities.IntegrationRuntimeCapabilityError: The Google Calendar authorization is invalid or revoked\nValueError: Invalid output\n",
      exit_code: 1,
    };
    const { container } = render(<RunHistory runs={[run]} />);
    expect(container.querySelector(".run-history-error-summary")?.textContent).toBe("ValueError: Invalid output");
  });

  it("retains a specific recorded error and the no-stderr fallback", () => {
    const run: SkillRun = {
      ...makeRun(76, {}),
      status: "failed",
      error_message: "Skill timed out after 300 seconds",
      stderr: "integration_runtime_capabilities.IntegrationRuntimeCapabilityError: The Google Calendar authorization is invalid or revoked\n",
      exit_code: 1,
    };
    const { container, rerender } = render(<RunHistory runs={[run]} />);
    expect(container.querySelector(".run-history-error-summary")?.textContent).toBe(run.error_message);
    rerender(<RunHistory runs={[{ ...run, stderr: null, error_message: "Skill exited with code 1" }]} />);
    expect(container.querySelector(".run-history-error-summary")?.textContent).toBe("Skill exited with code 1");
  });

  it("propagates a nested integration failure to the parent history summary", () => {
    const child: SkillRun = {
      ...makeRun(78, {}),
      status: "failed",
      skill_name: "calendar_reader",
      error_message: "Skill exited with code 1",
      stderr: "integration_runtime_capabilities.IntegrationRuntimeCapabilityError: The Google Calendar authorization is invalid or revoked\n",
      exit_code: 1,
    };
    const parent: SkillRun = {
      ...makeRun(77, {}),
      status: "failed",
      error_message: "Skill exited with code 1",
      child_runs: [child],
    };
    const { container } = render(<RunHistory runs={[parent]} />);
    expect(container.querySelector(".run-history-error-summary")?.textContent).toBe(
      "Calendar Reader failed in run #78: Integration authentication/authorization error: The Google Calendar authorization is invalid or revoked",
    );
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
