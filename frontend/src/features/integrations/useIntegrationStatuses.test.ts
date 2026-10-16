import { describe, expect, it } from "vitest";

import { atlasConnectionStatus, codexConnectionStatus, summarizeConnections } from "./useIntegrationStatuses";

describe("Integration overview summaries", () => {
  it.each([
    [[false, false, false], "disconnected"],
    [[true, false, true], "partial"],
    [[true, true, true], "connected"],
    [[true, true, false, true], "partial"],
    [[], "disconnected"],
  ])("summarizes grouped connection availability %j as %s", (states, expected) => {
    expect(summarizeConnections(states as boolean[], "details").state).toBe(expected);
  });

  it("requires Atlas to be running, initialized, and unlocked", () => {
    const atlas = { provider: "atlas" as const, directory: "", process_ownership: "owned" as const, running: true, initialized: true, locked: false, passphrase_configured: true };
    expect(atlasConnectionStatus(atlas).state).toBe("connected");
    expect(atlasConnectionStatus({ ...atlas, locked: true }).state).toBe("partial");
    expect(atlasConnectionStatus({ ...atlas, initialized: null }).state).toBe("partial");
    expect(atlasConnectionStatus({ ...atlas, running: false }).state).toBe("disconnected");
    expect(atlasConnectionStatus({ ...atlas, error_type: "unavailable" }).state).toBe("unavailable");
  });

  it("reports partial Codex setup until registration matches", () => {
    const codex = { enabled: true, registered: true, config_matches: true, available_tool_count: 3, excluded_ids: [], config_path: "", restart_required: false, error_type: null, error: null };
    expect(codexConnectionStatus(codex).state).toBe("connected");
    expect(codexConnectionStatus({ ...codex, config_matches: false }).state).toBe("partial");
    expect(codexConnectionStatus({ ...codex, enabled: false, registered: false }).state).toBe("disconnected");
    expect(codexConnectionStatus({ ...codex, error_type: "unavailable" }).state).toBe("unavailable");
  });
});
