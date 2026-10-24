// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApprovalRequest, RunnerStatus, Skill, api } from "../api/client";
import SkillDetailPage from "./SkillDetailPage";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function mockDetail() {
  const skill: Skill = {
    id: 7, name: "text_tools", description: "A focused text utility.", runtime: "function",
    status: "installed", risk_level: "low", manifest_path: "skills/text_tools/manifest.json",
    instructions_path: null, input_schema_json: { type: "object" }, output_schema_json: { type: "object" },
    installed_path: "skills/text_tools", active_version_id: null, enabled: true, is_running: false,
    created_at: "2026-10-21T00:00:00Z", updated_at: "2026-10-21T00:00:00Z",
  };
  vi.spyOn(api, "getSkill").mockResolvedValue(skill);
  vi.spyOn(api, "getSkillModel").mockResolvedValue({ skill_id: 7, model: null, reasoning_effort: null });
  vi.spyOn(api, "getCodexModels").mockResolvedValue({ available: false, fetched_at: "", error: null, models: [] });
  vi.spyOn(api, "listSkillRuns").mockResolvedValue([]);
  vi.spyOn(api, "listSkillFiles").mockResolvedValue([{ path: "skill.py", content: "def run(): pass" }]);
  vi.spyOn(api, "listPermissionRequests").mockResolvedValue([{
    id: 5, request_type: "install", status: "approved", risk_level: "low", reason_json: {},
    reason: "Reviewed runtime contract", user_explanation: "Reviewed runtime contract",
  } as ApprovalRequest]);
  vi.spyOn(api, "getRunnerStatus").mockResolvedValue({ available: true, selected_mode: "local", detail: "Local development runner" } as RunnerStatus);
  vi.spyOn(api, "listSchedules").mockResolvedValue([]);
  vi.spyOn(api, "listAgentRuns").mockResolvedValue([]);
  vi.spyOn(api, "listSkillVersions").mockResolvedValue([]);
  return skill;
}

function renderDetail() {
  render(<MemoryRouter initialEntries={["/skills/7"]}><Routes><Route path="/skills/:skillId" element={<SkillDetailPage />} /></Routes></MemoryRouter>);
}

describe("SkillDetailPage", () => {
  it("keeps the run draft while moving between focused views and retains it after a run fails", async () => {
    mockDetail();
    const run = vi.spyOn(api, "runSkill").mockRejectedValue(new Error("Runtime permission changed"));
    renderDetail();
    expect(await screen.findByRole("heading", { name: "Text Tools" })).toBeTruthy();
    const input = screen.getByRole("textbox", { name: "Run input JSON" }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '{"text":"my draft"}' } });
    fireEvent.click(screen.getByRole("tab", { name: "Files" }));
    expect(screen.getByRole("tabpanel", { name: "Files" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "skill.py" })).toBeTruthy();
    expect(input.closest("[role='tabpanel']")).toHaveProperty("hidden", true);
    fireEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(screen.getByRole("textbox", { name: "Run input JSON" })).toBe(input);
    fireEvent.click(screen.getByRole("button", { name: "Run with this input" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Runtime permission changed");
    expect(run).toHaveBeenCalledWith(7, { text: "my draft" });
    expect(input.value).toBe('{"text":"my draft"}');
    expect(screen.getByRole("heading", { name: "Text Tools" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Reset Example" }));
    expect(JSON.parse(input.value)).toEqual({ hello: "world" });
  });

  it("asks for an Eidolon confirmation before deleting the skill", async () => {
    mockDetail();
    const remove = vi.spyOn(api, "deleteSkill").mockResolvedValue({} as never);
    renderDetail();
    expect(await screen.findByRole("heading", { name: "Text Tools" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete Skill" }));
    expect(screen.getByRole("dialog", { name: "Delete skill" })).toBeTruthy();
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(remove).not.toHaveBeenCalled();
  });
});
