import net from "node:net";
import path from "node:path";
import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { timingSafeEqual } from "node:crypto";
import { createPageAuthentication } from "./browser_authentication_init_page.mjs";

export const SUPPORTED_MCP_VERSION = "0.0.83";
export const SUPPORTED_CORE_VERSION = "1.64.0-alpha-1790635538000";
const unavailable = () => ({ status: "browser_unavailable" });
const toolError = () => ({ isError: true, content: [{ type: "text", text: "Browser operation unavailable." }] });

function sameToken(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || !left || !right) return false;
  const a = Buffer.from(left), b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

// A concurrency adapter, not a JavaScript sandbox. All sessions deliberately share
// website cookies, storage and login. Public page/context discovery is session-local.
export class SessionPages {
  constructor(context) {
    this.context = context;
    this.owners = new WeakMap();
    this.sessions = new Map();
    context.on("page", (page) => {
      Promise.resolve(page.opener()).then((opener) => {
        const owner = opener && this.owners.get(opener);
        if (owner) this.claim(owner, page);
      }).catch(() => {});
    });
    context.on("close", () => {
      for (const session of this.sessions.values()) session.events.emit("close");
    });
  }

  claim(session, page) {
    if (session.closed) { page.close().catch(() => {}); return null; }
    const previous = this.owners.get(page);
    if (previous) return previous === session ? session.wrapped.get(page) : null;
    this.owners.set(page, session);
    const wrapped = new Proxy(page, {
      get: (target, key) => {
        if (key === "context") return () => session.context;
        if (key === "opener") return async () => {
          const opener = await target.opener();
          return opener && this.owners.get(opener) === session ? session.wrapped.get(opener) : null;
        };
        if (key === "waitForEvent") return async (event, options) => {
          const result = await target.waitForEvent(event, options);
          return event === "popup" ? this.claim(session, result) : result;
        };
        if (key === "on" || key === "once" || key === "addListener") return (event, listener) => {
          const callback = event === "popup" ? (popup) => listener(this.claim(session, popup)) : listener;
          target[key](event, callback);
          return wrapped;
        };
        const value = Reflect.get(target, key, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    session.wrapped.set(page, wrapped);
    session.pages.add(page);
    page.once("close", () => session.pages.delete(page));
    session.events.emit("page", wrapped);
    return wrapped;
  }

  create(sessionId) {
    if (this.sessions.has(sessionId)) return this.sessions.get(sessionId);
    const session = { sessionId, pages: new Set(), wrapped: new WeakMap(), events: new EventEmitter(), closed: false };
    const browser = this.context.browser();
    let browserProxy;
    if (browser) browserProxy = new Proxy(browser, {
      get: (target, key) => {
        if (key === "contexts") return () => [session.context];
        if (key === "close") return () => this.closePages(session);
        if (key === "newContext" || key === "newPage") return () => { throw new Error("Use the session context to open pages."); };
        const value = Reflect.get(target, key, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    session.context = new Proxy(this.context, {
      get: (target, key) => {
        if (key === "pages") return () => [...session.pages].map((p) => session.wrapped.get(p));
        if (key === "newPage") return async () => this.claim(session, await target.newPage());
        if (key === "browser") return () => browserProxy;
        if (key === "close") return () => this.closePages(session);
        if (key === "waitForEvent") return (event, options = {}) => new Promise((resolve, reject) => {
          const predicate = typeof options === "function" ? options : options.predicate;
          const timeout = typeof options === "object" ? options.timeout ?? 30_000 : 30_000;
          let timer;
          const finish = () => { clearTimeout(timer); session.events.off(event, listener); };
          const listener = (value) => { if (!predicate || predicate(value)) { finish(); resolve(value); } };
          session.events.on(event, listener);
          if (timeout) timer = setTimeout(() => { finish(); reject(new Error("Session browser event timed out.")); }, timeout);
        });
        if (["on", "once", "addListener", "removeListener", "off"].includes(key)) return (event, listener) => {
          session.events[key](event, listener);
          return session.context;
        };
        const value = Reflect.get(target, key, target);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    this.sessions.set(sessionId, session);
    return session;
  }

  async closePages(session) {
    await Promise.all([...session.pages].map((p) => p.close().catch(() => {})));
  }

  async close(sessionId) {
    const session = this.sessions.get(sessionId);
    if (!session) return;
    session.closed = true;
    await this.closePages(session);
    session.events.removeAllListeners();
    this.sessions.delete(sessionId);
  }
}

export class BrowserOwner {
  constructor({ masterToken, context, makeBackend, metadata, shutdown, privateValues = [] }) {
    this.masterToken = masterToken;
    this.pages = new SessionPages(context);
    this.makeBackend = makeBackend;
    this.metadata = metadata;
    this.shutdownBrowser = shutdown;
    this.sessions = new Map();
    this.closed = false;
    this.privateValues = privateValues;
  }

  sanitize(response) {
    let encoded = JSON.stringify(response);
    const values = [this.masterToken, ...this.privateValues, ...[...this.sessions.values()].map((session) => session.token)];
    for (const value of values) {
      if (typeof value === "string" && value) encoded = encoded.replaceAll(JSON.stringify(value).slice(1, -1), "[private browser value]");
    }
    return encoded;
  }

  serialize(session, operation) {
    const promise = session.pending.catch(() => {}).then(operation);
    session.pending = promise.catch(() => {});
    return promise;
  }

  async handle(request) {
    if (this.closed || !request || typeof request !== "object") return unavailable();
    const { action, sessionId } = request;
    if (sameToken(request.token, this.masterToken)) {
      if (action === "shutdown") { await this.shutdown(); return { ok: true }; }
      if (!Number.isSafeInteger(sessionId) || sessionId < 1) return unavailable();
      if (action === "register") {
        if (typeof request.sessionToken !== "string" || !request.sessionToken) return unavailable();
        let session = this.sessions.get(sessionId);
        if (!session) {
          // Publish before awaiting backend creation to coalesce simultaneous registration.
          session = { sessionId, token: request.sessionToken, pending: Promise.resolve() };
          this.sessions.set(sessionId, session);
          session.ready = this.makeBackend(this.pages.create(sessionId).context).then((backend) => {
            session.backend = backend;
            session.auth = createPageAuthentication(() => backend._context?.currentTab()?.page);
          });
        }
        if (!sameToken(session.token, request.sessionToken)) session.active?.abort();
        session.token = request.sessionToken;
        await session.ready;
        return { ok: true };
      }
      const session = this.sessions.get(sessionId);
      if (action === "revoke") {
        if (session) { session.token = null; session.active?.abort(); }
        return { ok: true };
      }
      if (action === "close_session") {
        if (session) {
          session.token = null;
          session.active?.abort();
          await this.serialize(session, async () => {
            await session.ready;
            await session.backend.dispose();
            await this.pages.close(sessionId);
          });
          this.sessions.delete(sessionId);
        }
        return { ok: true };
      }
      return unavailable();
    }
    const session = sessionId === undefined
      ? [...this.sessions.values()].find((candidate) => sameToken(request.token, candidate.token))
      : this.sessions.get(sessionId);
    if (!session || !sameToken(request.token, session.token)) return unavailable();
    return this.serialize(session, async () => {
      // A queued request cannot execute after its turn capability was revoked.
      if (!sameToken(request.token, session.token)) return unavailable();
      await session.ready;
      if (action === "mcp_list") return { tools: [...this.metadata, ...session.backend.dynamicTools()] };
      if (action === "status" || action === "authenticate") return session.auth(request);
      if (action !== "mcp_call" || typeof request.name !== "string") return unavailable();
      if (request.name === "browser_close") {
        await this.pages.closePages(this.pages.sessions.get(session.sessionId));
        return { content: [{ type: "text", text: "No open tabs. Navigate to a URL to create one." }] };
      }
      // Never return owner configuration/profile details through configuration tools.
      if (request.name === "browser_get_config") return toolError();
      const controller = new AbortController();
      session.active = controller;
      try {
        return await session.backend.callTool(request.name, request.arguments ?? {}, controller.signal);
      } finally {
        if (session.active === controller) session.active = null;
      }
    });
  }

  async shutdown() {
    if (this.closed) return;
    this.closed = true;
    for (const session of this.sessions.values()) { session.token = null; session.active?.abort(); }
    await Promise.all([...this.sessions.values()].map(async (session) => {
      await session.pending;
      await session.ready.catch(() => {});
      await session.backend?.dispose().catch(() => {});
    }));
    this.sessions.clear();
    await this.shutdownBrowser();
  }
}

// MCP 0.0.83 writes automatic full snapshots to files. Act has no shell reader,
// so use its explicit full-snapshot response method to retain inline references.
// Explicit browser_snapshot filenames and snapshot mode "none" retain upstream behavior.
export function inlineSnapshotTool(tool) {
  return {
    ...tool,
    handle: (context, params, response, signal) => {
      const inlineResponse = new Proxy(response, {
        get: (target, key) => {
          if (key === "setIncludeSnapshot" && context.config.snapshot?.mode !== "none") return () => {
            if (typeof target.setIncludeFullSnapshot !== "function") throw new Error("Unsupported browser adapter.");
            target.setIncludeFullSnapshot();
          };
          const value = Reflect.get(target, key, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      });
      return tool.handle(context, params, inlineResponse, signal);
    },
  };
}

export async function loadAdapter(modulePath, env = process.env) {
  if (!path.isAbsolute(modulePath)) throw new Error("Unsupported browser adapter.");
  const require = createRequire(modulePath);
  if (require("./package.json").version !== SUPPORTED_MCP_VERSION) throw new Error("Unsupported browser adapter.");
  const corePath = path.dirname(require.resolve("playwright-core/package.json"));
  if (require(path.join(corePath, "package.json")).version !== SUPPORTED_CORE_VERSION) throw new Error("Unsupported browser adapter.");
  const { tools } = require(path.join(corePath, "lib/coreBundle.js"));
  const { z } = require(path.join(corePath, "lib/utilsBundle.js"));
  if (!["resolveCLIConfigForMCP", "createBrowserWithInfo", "BrowserBackend", "filteredTools"].every((key) => typeof tools?.[key] === "function") || typeof z.toJSONSchema !== "function") throw new Error("Unsupported browser adapter.");
  const config = await tools.resolveCLIConfigForMCP({ userDataDir: env.EIDOLON_BROWSER_PROFILE, idleTimeout: 0, outputDir: path.join(env.EIDOLON_BROWSER_WORKSPACE, "browser-output"), headless: env.EIDOLON_BROWSER_HEADLESS === "1" }, {});
  config.browser.initPage = [];
  config.browser.initScript = [];
  config.saveSession = false;
  const clientInfo = { clientName: "Eidolon Act", clientVersion: "1", cwd: env.EIDOLON_BROWSER_WORKSPACE, roots: [{ uri: pathToFileURL(env.EIDOLON_BROWSER_WORKSPACE).href }] };
  const info = await tools.createBrowserWithInfo(config, clientInfo, {}, { title: "Eidolon Act", workspaceDir: clientInfo.cwd });
  const context = info.browser.contexts()[0];
  if (!context || typeof context.newPage !== "function") { await info.browser.close(); throw new Error("Unsupported browser adapter."); }
  const selected = tools.filteredTools(config).filter((tool) => tool.schema.name !== "browser_get_config").map(inlineSnapshotTool);
  const metadata = selected.map(({ schema }) => {
    const readOnly = ["readOnly", "assertion"].includes(schema.type);
    return { name: schema.name, description: schema.description, inputSchema: z.toJSONSchema(schema.inputSchema), annotations: { title: schema.title, readOnlyHint: readOnly, destructiveHint: !readOnly, openWorldHint: true } };
  });
  return new BrowserOwner({
    masterToken: env.EIDOLON_BROWSER_OWNER_TOKEN, context, metadata,
    privateValues: [env.EIDOLON_BROWSER_PROFILE, env.EIDOLON_BROWSER_AUTH_ENDPOINT],
    makeBackend: async (view) => {
      const backend = new tools.BrowserBackend(config, view, selected);
      await backend.initialize(clientInfo);
      if (typeof backend._context?.currentTab !== "function") { await backend.dispose(); throw new Error("Unsupported browser adapter."); }
      return backend;
    },
    shutdown: () => info.browser.close(),
  });
}

export async function startOwner(env = process.env) {
  env = { ...env };
  if (!["EIDOLON_BROWSER_AUTH_ENDPOINT", "EIDOLON_BROWSER_OWNER_TOKEN", "EIDOLON_BROWSER_PROFILE", "EIDOLON_BROWSER_WORKSPACE", "EIDOLON_PLAYWRIGHT_MODULE_PATH"].every((key) => env[key])) throw new Error("Browser configuration unavailable.");
  const owner = await loadAdapter(env.EIDOLON_PLAYWRIGHT_MODULE_PATH, env);
  // Upstream's unsafe code tool is intentionally enabled. It must not inherit
  // the private owner/MCP capabilities through process.env.
  for (const key of ["EIDOLON_BROWSER_OWNER_TOKEN", "EIDOLON_BROWSER_AUTH_TOKEN", "EIDOLON_BROWSER_PROFILE", "EIDOLON_BROWSER_AUTH_ENDPOINT"]) delete process.env[key];
  const server = net.createServer((socket) => {
    let buffer = Buffer.alloc(0), handled = false;
    socket.setTimeout(240_000, () => socket.destroy());
    socket.on("error", () => {});
    socket.on("data", (chunk) => {
      if (handled) return;
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > 1_048_576) { handled = true; socket.destroy(); return; }
      const newline = buffer.indexOf(10);
      if (newline < 0) return;
      handled = true;
      Promise.resolve().then(() => JSON.parse(buffer.subarray(0, newline).toString("utf8"))).then((request) => owner.handle(request)).catch(() => unavailable()).then((response) => {
        let encoded = owner.sanitize(response);
        if (Buffer.byteLength(encoded) > 16_777_216) encoded = JSON.stringify(toolError());
        socket.end(`${encoded}\n`);
      });
    });
  });
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(env.EIDOLON_BROWSER_AUTH_ENDPOINT, resolve); });
  let stopping;
  const stop = () => stopping ??= owner.shutdown().finally(() => server.close());
  process.stdin.resume();
  process.stdin.once("end", stop);
  process.once("SIGTERM", stop);
  process.once("SIGINT", stop);
  process.stdout.write(`${JSON.stringify({ ready: true })}\n`);
  return { owner, server, stop };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  startOwner().catch(() => { process.stderr.write("Act browser could not start.\n"); process.exitCode = 1; });
}
