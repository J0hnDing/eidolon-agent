import { beforeEach, describe, expect, it } from "vitest";
import { ActSession } from "../../api/client";
import { cacheSession, clearManagedSessionCache, forgetCachedSession, getCachedSession, reconcileCachedSessions } from "./managedSessionCache";

function session(id: number): ActSession {
  return { id, title: `Session ${id}`, origin: "web", status: "active", created_at: "2026-10-22T12:00:00Z", updated_at: "2026-10-22T12:00:00Z", wecom_user_id: null, turns: [] };
}

beforeEach(clearManagedSessionCache);

describe("managed session navigation cache", () => {
  it("keeps identical session IDs isolated by agent and evicts archived or pruned sessions", () => {
    cacheSession("act", session(7));
    cacheSession("observer", session(7));
    cacheSession("assistant", session(7));
    forgetCachedSession("act", 7);
    expect(getCachedSession("act", 7)).toBeUndefined();
    expect(getCachedSession("observer", 7)).toBeDefined();
    reconcileCachedSessions("observer", []);
    expect(getCachedSession("observer", 7)).toBeUndefined();
    expect(getCachedSession("assistant", 7)).toBeDefined();
  });

  it("retains only the 20 most recently refreshed sessions", () => {
    for (let id = 1; id <= 20; id++) cacheSession("act", session(id));
    cacheSession("act", session(1));
    cacheSession("act", session(21));
    expect(getCachedSession("act", 1)).toBeDefined();
    expect(getCachedSession("act", 2)).toBeUndefined();
    expect(getCachedSession("act", 21)).toBeDefined();
    clearManagedSessionCache();
    expect(getCachedSession("act", 1)).toBeUndefined();
  });
});
