import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdir, mkdtemp } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { loadAdapter } from "../app/act_browser_runtime.mjs";

// Opt-in: uses the existing pinned package and a fresh test-only profile.
test("real persistent browser recovers closure with cookies and conversation isolation", {
  skip: process.env.EIDOLON_BROWSER_RECOVERY_SMOKE !== "1", timeout: 90_000,
}, async () => {
  const runtime = fileURLToPath(new URL("../../runtime/", import.meta.url));
  await mkdir(runtime, { recursive: true });
  const root = await mkdtemp(path.join(runtime, "browser-recovery-smoke-"));
  const server = http.createServer((_request, response) => response.end("<!doctype html><title>Browser recovery test</title><p>Local test page</p>"));
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let owner;
  try {
    owner = await loadAdapter(process.env.EIDOLON_PLAYWRIGHT_MODULE_PATH, {
      EIDOLON_BROWSER_PROFILE: path.join(root, "profile"), EIDOLON_BROWSER_WORKSPACE: root,
      EIDOLON_BROWSER_OWNER_TOKEN: "test-master", EIDOLON_BROWSER_HEADLESS: "1",
    });
    const register = (sessionId, sessionToken) => owner.handle({ token: "test-master", action: "register", sessionId, sessionToken });
    const call = (sessionId, token, name, args = {}) => owner.handle({ action: "mcp_call", sessionId, token, name, arguments: args });
    const url = `http://127.0.0.1:${server.address().port}/`;
    await register(1, "a"); await register(2, "b");
    assert.ok(!(await call(1, "a", "browser_navigate", { url })).isError);
    assert.ok(!(await call(2, "b", "browser_navigate", { url })).isError);
    const oldContext = owner.pages.context;
    await oldContext.addCookies([{ name: "recovery-smoke", value: "retained", domain: "example.test", path: "/", expires: Date.now() / 1000 + 3600 }]);
    await oldContext.close();
    const resets = await Promise.all([call(1, "a", "browser_tabs", { action: "list" }), call(2, "b", "browser_tabs", { action: "list" })]);
    assert.ok(resets.every((result) => result.isError && result.content[0].text.includes("Previous tabs")));
    assert.equal(owner.generation, 1);
    assert.equal((await owner.pages.context.cookies()).find((cookie) => cookie.name === "recovery-smoke")?.value, "retained");
    assert.ok(!(await call(1, "a", "browser_navigate", { url })).isError);
    assert.ok(!(await call(2, "b", "browser_navigate", { url })).isError);
    assert.equal(owner.pages.create(1).context.pages().length, 1);
    assert.equal(owner.pages.create(2).context.pages().length, 1);
    await call(1, "a", "browser_close");
    assert.ok(!(await call(2, "b", "browser_snapshot")).isError);
    assert.equal(owner.generation, 1);
    await owner.handle({ token: "test-master", action: "revoke", sessionId: 1 });
    assert.deepEqual(await call(1, "a", "browser_tabs", { action: "list" }), { status: "browser_unavailable" });
  } finally {
    await owner?.shutdown();
    await new Promise((resolve) => server.close(resolve));
  }
});
