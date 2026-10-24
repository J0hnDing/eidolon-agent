// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Skill, api } from "../api/client";
import WebAppsPage, { applicationSkills } from "./WebAppsPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function skill(id: number, runtime: Skill["runtime"]): Skill {
  return {
    id,
    name: `skill_${id}`,
    description: "Example skill",
    runtime,
    status: "installed",
    risk_level: "low",
    manifest_path: `skills/installed/skill_${id}/manifest.json`,
    instructions_path: null,
    input_schema_json: null,
    output_schema_json: null,
    installed_path: `skills/installed/skill_${id}`,
    active_version_id: id,
    enabled: true,
    is_running: false,
    created_at: "2026-07-16T00:00:00Z",
    updated_at: "2026-07-16T00:00:00Z",
  };
}

describe("applicationSkills", () => {
  it("uses runtime as the only Applications visibility discriminator", () => {
    const webApp = skill(1, "web_app");
    const functionSkill = skill(2, "function");

    expect(applicationSkills([functionSkill, webApp])).toEqual([webApp]);
  });
});

describe("WebAppsPage", () => {
  it("explains how to add the first application", async () => {
    vi.spyOn(api, "listSkills").mockResolvedValue([]);

    render(<MemoryRouter><WebAppsPage /></MemoryRouter>);

    expect(await screen.findByRole("heading", { name: "No applications yet" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Browse skills" }).getAttribute("href")).toBe("/skills");
  });

  it("filters applications without exposing a launch action for disabled apps", async () => {
    vi.spyOn(api, "listSkills").mockResolvedValue([
      { ...skill(1, "web_app"), name: "reading_room", is_running: true },
      { ...skill(2, "web_app"), name: "notes_board", enabled: false },
      skill(3, "function"),
    ]);
    render(<MemoryRouter><WebAppsPage /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "Reading Room" })).toBeTruthy();
    expect(screen.getAllByRole("link", { name: "Open Application" })).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Open Application" }).getAttribute("href")).toBe("/apps/1");
    fireEvent.change(screen.getByLabelText("Show"), { target: { value: "running" } });
    expect(screen.queryByRole("heading", { name: "Notes Board" })).toBeNull();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search applications" }), { target: { value: "missing" } });
    expect(screen.getByRole("heading", { name: "No applications match these filters" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("heading", { name: "Notes Board" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Skill 3" })).toBeNull();
  });
});
