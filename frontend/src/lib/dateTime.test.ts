import { describe, expect, it } from "vitest";

import { parseBackendDateTime } from "./dateTime";

describe("backend date-time parsing", () => {
  it("treats SQLite timestamps without an offset as UTC before system-time display", () => {
    expect(parseBackendDateTime("2026-08-30T02:48:03.057751").toISOString()).toBe(
      "2026-08-30T02:48:03.057Z",
    );
  });
});
