// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { Skill } from "../api/client";
import { SkillTable } from "./SkillsPage";

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
    expect(runningDot.closest("td")?.textContent).toContain("running");
    expect(runningDot.closest("tr")?.firstElementChild?.querySelector(".running-state-dot")).toBeNull();
  });
});
