// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API request coalescing", () => {
  it("shares concurrent identical GETs without caching the completed response", async () => {
    const payload = {
      provider: "github",
      connected: false,
      status: "disconnected",
      account_login: null,
      account_id: null,
      last_validated_at: null,
      created_at: null,
      updated_at: null,
      error_type: null,
    };
    let resolveFirst: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => {
        resolveFirst = resolve;
      }))
      .mockResolvedValueOnce(jsonResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const first = api.getGitHubConnection();
    const duplicate = api.getGitHubConnection();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveFirst?.(jsonResponse(payload));
    await expect(Promise.all([first, duplicate])).resolves.toEqual([payload, payload]);

    await expect(api.getGitHubConnection()).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response;
}
