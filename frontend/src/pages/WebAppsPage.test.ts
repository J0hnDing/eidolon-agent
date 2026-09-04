import { describe, expect, it } from "vitest";

import { Skill } from "../api/client";
import { applicationSkills } from "./WebAppsPage";

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
