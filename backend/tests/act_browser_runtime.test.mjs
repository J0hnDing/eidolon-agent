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
  constructor() { super(); this.allPages = []; this.realBrowser = new EventEmitter(); this.realBrowser.contexts = () => [this]; this.realBrowser.close = async () => { this.closed = true; }; }
  pages() { return this.allPages.filter((p) => !p.closed); }
  browser() { return this.realBrowser; }
  async newPage() { const page = new Page(this); this.allPages.push(page); this.emit("page", page); return page; }
  popup(opener) { const page = new Page(this, opener); this.allPages.push(page); this.emit("page", page); opener.emit("popup", page); return page; }
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

function setupOwner() {
  const context = new Context(); let backends = 0, disposed = 0;
  const owner = new BrowserOwner({ masterToken: "master", context, metadata: [{ name: "browser_tabs" }], shutdown: async () => { context.closed = true; }, makeBackend: async (view) => {
    backends++;
    let current;
    return { _context: { currentTab: () => current && { page: current } }, dynamicTools: () => [], dispose: async () => { disposed++; }, callTool: async (name, args, signal) => {
      if (name === "new") current = await view.newPage();
      if (name === "select") current = view.pages()[args.index];
      if (name === "wait") await new Promise((resolve) => setTimeout(resolve, 20));
      if (name === "abortable") await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
      return { content: [{ type: "text", text: `${view.pages().length}` }] };
    } };
  } });
  return { owner, context, counts: () => ({ backends, disposed }) };
}
const register = (owner, id, token) => owner.handle({ token: "master", action: "register", sessionId: id, sessionToken: token });
const call = (owner, id, token, name, args = {}) => owner.handle({ token, sessionId: id, action: "mcp_call", name, arguments: args });

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
