import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { BrowserOwner, SessionPages, inlineSnapshotTool } from "../app/act_browser_runtime.mjs";
import { createPageAuthentication } from "../app/browser_authentication_init_page.mjs";

class Page extends EventEmitter {
  constructor(context, opener = null) { super(); this.ownerContext = context; this.openedBy = opener; this.closed = false; this.address = "https://login.example/"; }
  context() { return this.ownerContext; }
  async opener() { return this.openedBy; }
  isClosed() { return this.closed; }
  async close() { this.closed = true; this.emit("close"); }
  url() { return this.address; }
  locator() { return { innerText: async () => "Log in" }; }
}
class Context extends EventEmitter {
  constructor() { super(); this.allPages = []; this.realBrowser = new EventEmitter(); this.realBrowser.contexts = () => [this]; this.realBrowser.close = () => this.close(); this.realBrowser.isConnected = () => !this.closed; }
  pages() { return this.allPages.filter((p) => !p.closed); }
  browser() { return this.realBrowser; }
  async newPage() { const page = new Page(this); this.allPages.push(page); this.emit("page", page); return page; }
  popup(opener) { const page = new Page(this, opener); this.allPages.push(page); this.emit("page", page); opener.emit("popup", page); return page; }
  async close() { this.closed = true; await Promise.all(this.pages().map((p) => p.close())); this.emit("close"); this.realBrowser.emit("disconnected"); }
}

test("session page discovery, popups, and close stay local to their owner", async () => {
  const context = new Context(), views = new SessionPages(context);
  const a = views.create(1), b = views.create(2);
  const aEvents = [], bEvents = [];
  a.context.on("page", (p) => aEvents.push(p)); b.context.on("page", (p) => bEvents.push(p));
  const [p1, p2] = await Promise.all([a.context.newPage(), b.context.newPage()]);
  assert.deepEqual(a.context.pages(), [p1]); assert.deepEqual(b.context.pages(), [p2]);
  assert.equal(p1.context(), a.context);
  assert.deepEqual(p1.context().browser().contexts(), [a.context]);
  const popup = context.popup(context.allPages[0]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(a.context.pages().length, 2); assert.equal(b.context.pages().length, 1);
  assert.equal(await a.context.pages()[1].opener(), p1);
  assert.equal(aEvents.length, 2); assert.equal(bEvents.length, 1);
  assert.notEqual(a.context.pages()[1], popup);
  const localEvent = a.context.waitForEvent("page", { timeout: 200 });
  await b.context.newPage();
  const localPage = await a.context.newPage();
  assert.equal(await localEvent, localPage);
  await p1.context().browser().close();
  assert.equal(a.context.pages().length, 0); assert.equal(p2.isClosed(), false); assert.equal(context.closed, undefined);
});

function setupOwner({ reopen } = {}) {
  const context = new Context(); let backends = 0, disposed = 0;
  const owner = new BrowserOwner({ masterToken: "master", context, reopen, metadata: [{ name: "browser_tabs" }], shutdown: async () => { context.closed = true; }, makeBackend: async (view) => {
    backends++;
    let current;
    return { _context: { currentTab: () => current && { page: current } }, dynamicTools: () => [], dispose: async () => { disposed++; }, callTool: async (name, args, signal) => {
      if (name === "new") current = await view.newPage();
      if (name === "select") current = view.pages()[args.index];
      if (name === "wait") await new Promise((resolve) => setTimeout(resolve, 20));
      if (name === "abortable") await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
      if (name === "closeDuringAction") { await view.realBrowser.close(); throw new Error("Target page, context or browser has been closed"); }
      return { content: [{ type: "text", text: `${view.pages().length}` }] };
    } };
  } });
  return { owner, context, counts: () => ({ backends, disposed }) };
}
const register = (owner, id, token) => owner.handle({ token: "master", action: "register", sessionId: id, sessionToken: token });
const call = (owner, id, token, name, args = {}) => owner.handle({ token, sessionId: id, action: "mcp_call", name, arguments: args });

for (const closure of ["context", "disconnected", "connection-state"]) test(`closed browser (${closure}) reopens once for concurrent sessions and reports lost tabs without replay`, async () => {
  const replacement = new Context(); let launches = 0;
  const { owner, context, counts } = setupOwner({ reopen: async () => { launches++; await new Promise((r) => setImmediate(r)); return replacement; } });
  await register(owner, 1, "a"); await register(owner, 2, "b");
  await call(owner, 1, "a", "new"); await call(owner, 2, "b", "new");
  if (closure === "context") await context.close();
  else {
    context.closed = true;
    if (closure === "disconnected") context.realBrowser.emit("disconnected");
  }
  const results = await Promise.all([call(owner, 1, "a", "new"), call(owner, 2, "b", "new")]);
  assert.equal(launches, 1);
  assert.ok(results.every((result) => result.isError && result.content[0].text.includes("Previous tabs")));
  assert.equal(replacement.pages().length, 0); // Neither requested action was replayed.
  assert.deepEqual(counts(), { backends: 4, disposed: 2 });
  assert.equal((await call(owner, 1, "a", "new")).content[0].text, "1");
  assert.equal((await call(owner, 2, "b", "new")).content[0].text, "1");
  assert.equal(owner.pages.create(1).context.pages().length, 1);
  assert.equal(owner.pages.create(2).context.pages().length, 1);
  await owner.shutdown();
});

test("browser closure during an action reports uncertain outcome and waits for another call to reopen", async () => {
  let launches = 0;
  const { owner } = setupOwner({ reopen: async () => { launches++; return new Context(); } });
  await register(owner, 1, "a");
  const result = await call(owner, 1, "a", "closeDuringAction");
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /outcome is uncertain/);
  assert.equal(launches, 0);
  assert.equal((await call(owner, 1, "a", "new")).isError, true);
  assert.equal(launches, 1);
  assert.equal((await call(owner, 1, "a", "new")).content[0].text, "1");
  await owner.shutdown();
});

test("failed relaunch can retry and token revocation during relaunch prevents execution", async () => {
  let launches = 0, release;
  const { owner, context } = setupOwner({ reopen: async () => {
    if (++launches === 1) throw new Error("launch failed");
    await new Promise((resolve) => { release = resolve; });
    return new Context();
  } });
  await register(owner, 1, "a"); await context.close();
  await assert.rejects(call(owner, 1, "a", "new"), /launch failed/);
  const recovering = call(owner, 1, "a", "new");
  await new Promise((resolve) => setImmediate(resolve));
  await owner.handle({ token: "master", action: "revoke", sessionId: 1 });
  release();
  assert.deepEqual(await recovering, { status: "browser_unavailable" });
  assert.equal(owner.pages.context.pages().length, 0);
  await register(owner, 1, "a2");
  assert.equal((await call(owner, 1, "a2", "new")).isError, true);
  assert.equal((await call(owner, 1, "a2", "new")).content[0].text, "1");
  await owner.shutdown();
});

test("registration recovers a dead browser while discovery and authentication preserve reset notice", async () => {
  const { owner, context } = setupOwner({ reopen: async () => new Context() });
  await register(owner, 1, "a"); await call(owner, 1, "a", "new"); await context.close();
  await register(owner, 1, "a2");
  assert.deepEqual(await call(owner, 1, "a", "new"), { status: "browser_unavailable" });
  assert.ok((await owner.handle({ token: "a2", sessionId: 1, action: "mcp_list" })).tools.length);
  assert.deepEqual(await owner.handle({ token: "a2", sessionId: 1, action: "authenticate" }), { status: "browser_unavailable" });
  assert.equal((await call(owner, 1, "a2", "new")).isError, true);
  await call(owner, 1, "a2", "new");
  assert.equal((await owner.handle({ token: "a2", sessionId: 1, action: "status", loginOrigins: ["https://login.example"] })).status, "login_required");
  await owner.shutdown();
});

test("shutdown during recovery closes the replacement and never executes queued actions", async () => {
  let release, closes = 0;
  const { owner, context } = setupOwner({ reopen: async () => {
    await new Promise((resolve) => { release = resolve; });
    return new Context();
  } });
  owner.shutdownBrowser = async () => { closes++; };
  await register(owner, 1, "a"); await context.close();
  const recovering = call(owner, 1, "a", "new");
  // Attach before rejecting to avoid an unhandled rejection in the test runner.
  const rejected = assert.rejects(recovering, /owner is closed/);
  await new Promise((resolve) => setImmediate(resolve));
  const stopping = owner.shutdown();
  release();
  await rejected; await stopping;
  assert.equal(owner.closed, true);
  assert.ok(closes >= 1);
  assert.deepEqual(await call(owner, 1, "a", "new"), { status: "browser_unavailable" });
});

test("token revocation and rotation preserve live pages/backend, close only owns tabs", async () => {
  const { owner, context, counts } = setupOwner();
  await register(owner, 1, "a"); await register(owner, 2, "b");
  await call(owner, 1, "a", "new"); await call(owner, 2, "b", "new");
  const page = context.allPages[0];
  await owner.handle({ token: "master", action: "revoke", sessionId: 1 });
  assert.equal(page.closed, false);
  assert.deepEqual(await call(owner, 1, "a", "new"), { status: "browser_unavailable" });
  await register(owner, 1, "a2");
  assert.deepEqual(counts(), { backends: 2, disposed: 0 });
  assert.equal((await owner.handle({ token: "a2", action: "status", loginOrigins: ["https://login.example"] })).status, "login_required");
  await owner.handle({ token: "master", action: "close_session", sessionId: 1 });
  assert.equal(page.closed, true); assert.equal(context.allPages[1].closed, false);
  await call(owner, 2, "b", "browser_close");
  assert.equal(context.allPages[1].closed, true); assert.equal(context.closed, undefined);
  await call(owner, 2, "b", "new");
  await owner.shutdown(); assert.equal(context.closed, true);
});

test("authentication follows selected tab, rather than most recently created page", async () => {
  const { owner, context } = setupOwner();
  await register(owner, 1, "a");
  await call(owner, 1, "a", "new"); await call(owner, 1, "a", "new");
  context.allPages[0].address = "https://authenticated.example/";
  await call(owner, 1, "a", "select", { index: 0 });
  const state = await owner.handle({ token: "a", sessionId: 1, action: "status", authenticatedOrigins: ["https://authenticated.example"] });
  assert.equal(state.status, "already_authenticated");
  const auth = createPageAuthentication(() => null);
  assert.equal((await auth({ action: "status" })).status, "browser_unavailable");
  await owner.shutdown();
});

test("queued calls are denied after revoke and independent sessions can execute", async () => {
  const { owner } = setupOwner(); await register(owner, 1, "a"); await register(owner, 2, "b");
  const first = call(owner, 1, "a", "wait");
  const queued = call(owner, 1, "a", "new");
  await call(owner, 2, "b", "new");
  await owner.handle({ token: "master", action: "revoke", sessionId: 1 });
  await first; assert.deepEqual(await queued, { status: "browser_unavailable" });
  await owner.shutdown();
});

test("revoke aborts the active session call without disturbing other sessions", async () => {
  const { owner } = setupOwner(); await register(owner, 1, "a"); await register(owner, 2, "b");
  const active = call(owner, 1, "a", "abortable");
  await new Promise((resolve) => setImmediate(resolve));
  await owner.handle({ token: "master", action: "revoke", sessionId: 1 });
  await active;
  assert.equal((await call(owner, 2, "b", "new")).content[0].text, "1");
  await owner.shutdown();
});

test("wire sanitizer hides private browser values and capabilities", () => {
  const { owner } = setupOwner(); owner.privateValues.push("C:\\private\\profile");
  const response = { content: [{ type: "text", text: "master C:\\private\\profile" }] };
  const encoded = owner.sanitize(response);
  assert.equal(encoded.includes("master"), false); assert.equal(encoded.includes("profile"), false);
});

test("automatic full snapshots are inline while explicit filenames and none stay upstream", async () => {
  const invocations = [];
  const response = {
    setIncludeSnapshot() { invocations.push("automatic"); },
    setIncludeFullSnapshot(filename) { invocations.push(filename ?? "inline"); },
  };
  const automatic = inlineSnapshotTool({ handle: async (_context, _params, result) => result.setIncludeSnapshot() });
  await automatic.handle({ config: {} }, {}, response);
  await automatic.handle({ config: { snapshot: { mode: "none" } } }, {}, response);
  const explicit = inlineSnapshotTool({ handle: async (_context, _params, result) => result.setIncludeFullSnapshot("requested.yml") });
  await explicit.handle({ config: {} }, {}, response);
  assert.deepEqual(invocations, ["inline", "automatic", "requested.yml"]);
});
