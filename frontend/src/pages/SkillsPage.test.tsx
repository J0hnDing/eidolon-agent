// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Skill, api } from "../api/client";
import SkillsPage, { SkillTable } from "./SkillsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SkillsPage", () => {
  it("offers a direct entry to the skill-building flow", async () => {
    vi.spyOn(api, "listSkills").mockResolvedValue([]);

    render(<MemoryRouter><SkillsPage /></MemoryRouter>);

    const buildLink = await screen.findByRole("link", { name: "Build a skill" });
    expect(buildLink.getAttribute("href")).toBe("/skills/build");
    expect(screen.getByText("No skills yet. Build one to add a reusable capability.")).toBeTruthy();
  });

  it("combines runtime, search, and availability and can clear an empty result", async () => {
    vi.spyOn(api, "listSkills").mockResolvedValue([
      { id: 1, name: "daily_brief", description: "Morning report", runtime: "service", status: "installed", risk_level: "low", enabled: true, is_running: false } as Skill,
      { id: 2, name: "brief_editor", description: "Edit reports", runtime: "web_app", status: "installed", risk_level: "low", enabled: false, is_running: false } as Skill,
    ]);
    render(<MemoryRouter><SkillsPage /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: "Open Daily Brief skill details" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Services 1" }));
    expect(screen.queryByRole("link", { name: "Open Brief Editor skill details" })).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search skills" }), { target: { value: "daily brief" } });
    expect(screen.getByRole("link", { name: "Open Daily Brief skill details" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Availability"), { target: { value: "disabled" } });
    expect(screen.getByRole("heading", { name: "No skills match these filters" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("link", { name: "Open Daily Brief skill details" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Brief Editor skill details" })).toBeTruthy();
  });
});

describe("SkillTable", () => {
  it("shows the shared running indicator only for active skills", () => {
    const skill = (id: number, isRunning: boolean): Skill => ({
      id,
      name: `skill_${id}`,
      description: "Example skill",
      runtime: "function",
      status: "installed",
      risk_level: "low",
      manifest_path: `skills/installed/skill_${id}/manifest.json`,
      instructions_path: null,
      input_schema_json: { type: "object" },
      output_schema_json: { type: "object" },
      installed_path: `skills/installed/skill_${id}`,
      active_version_id: id,
      enabled: true,
      is_running: isRunning,
      created_at: "2026-09-02T00:00:00Z",
      updated_at: "2026-09-02T00:00:00Z",
    });

    render(
      <MemoryRouter>
        <SkillTable skills={[skill(1, true), skill(2, false)]} />
      </MemoryRouter>,
    );

    const runningDot = screen.getByLabelText("Running");
    expect(screen.getAllByLabelText("Running")).toHaveLength(1);
    expect(runningDot.getAttribute("title")).toBe("Running");
    expect(runningDot.closest("td")?.cellIndex).toBe(1);
    expect(runningDot.closest("td")?.textContent).toBe("running");
    expect(runningDot.closest("tr")?.firstElementChild?.querySelector(".running-state-dot")).toBeNull();
    expect(screen.getAllByRole("cell", { name: "installed" })).toHaveLength(1);
  });
});
