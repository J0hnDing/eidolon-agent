import net from "node:net";

const endpoint = process.env.EIDOLON_BROWSER_AUTH_ENDPOINT;
const capability = process.env.EIDOLON_BROWSER_AUTH_TOKEN;
const MFA_PATTERN = /\b(duo|multi[- ]?factor|two[- ]?factor|verification code|approve (?:the )?(?:sign[- ]?in|login)|authentication request|security key)\b/i;
const FAILURE_PATTERN = /\b(invalid|incorrect|failed|unable to log in|authentication error|no corresponding user was found)\b/i;
let activePage = null;
let server = null;

function normalizeOrigin(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
    return parsed.origin.toLowerCase();
  } catch {
    return null;
  }
}

async function pageState(loginOrigins, authenticatedOrigins) {
  if (!activePage || activePage.isClosed()) return { status: "browser_unavailable", url: "" };
  const url = activePage.url();
  const origin = normalizeOrigin(url);
  if (authenticatedOrigins.includes(origin)) return { status: "already_authenticated", url };
  const text = await activePage.locator("body").innerText({ timeout: 2_000 }).catch(() => "");
  if (MFA_PATTERN.test(text)) return { status: "mfa_required", url };
  if (loginOrigins.includes(origin)) return { status: "login_required", url };
  return { status: "unsupported_origin", url };
}

async function firstVisible(selectors) {
  for (const selector of selectors) {
    const locator = activePage.locator(selector).first();
    if (await locator.isVisible({ timeout: 1_000 }).catch(() => false)) return locator;
  }
  return null;
}

function assertLoginOrigin(loginOrigins) {
  if (!activePage || activePage.isClosed() || !loginOrigins.includes(normalizeOrigin(activePage.url()))) {
    throw new Error("origin_changed");
  }
}

async function authenticate(request) {
  const loginOrigins = request.loginOrigins ?? [];
  const authenticatedOrigins = request.authenticatedOrigins ?? [];
  const initial = await pageState(loginOrigins, authenticatedOrigins);
  if (initial.status !== "login_required") return initial;
  assertLoginOrigin(loginOrigins);
  const username = await firstVisible(request.usernameSelectors ?? []);
  const password = await firstVisible(request.passwordSelectors ?? []);
  const submit = await firstVisible(request.submitSelectors ?? []);
  if (!username || !password || !submit) return { status: "authentication_failed", url: activePage.url() };
  assertLoginOrigin(loginOrigins);
  await username.fill(request.username);
  assertLoginOrigin(loginOrigins);
  await password.fill(request.password);
  assertLoginOrigin(loginOrigins);
  await submit.click();
  await activePage.waitForLoadState("domcontentloaded", { timeout: 10_000 }).catch(() => {});
  await activePage.waitForTimeout(750);
  const current = await pageState(loginOrigins, authenticatedOrigins);
  if (current.status === "already_authenticated" || current.status === "mfa_required") return current;
  const text = await activePage.locator("body").innerText({ timeout: 2_000 }).catch(() => "");
  if (FAILURE_PATTERN.test(text) || current.status === "login_required") {
    return { status: "authentication_failed", url: activePage.url() };
  }
  return { status: "user_action_required", url: activePage.url() };
}

async function handleRequest(request) {
  if (!request || request.token !== capability) return { status: "browser_unavailable" };
  const loginOrigins = Array.isArray(request.loginOrigins) ? request.loginOrigins : [];
  const authenticatedOrigins = Array.isArray(request.authenticatedOrigins) ? request.authenticatedOrigins : [];
  if (request.action === "status") return pageState(loginOrigins, authenticatedOrigins);
  if (request.action === "authenticate") return authenticate(request);
  return { status: "browser_unavailable" };
}

function startServer() {
  if (server || !endpoint || !capability) return;
  server = net.createServer((socket) => {
    socket.setEncoding("utf8");
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk;
      if (buffer.length > 16_384) socket.destroy();
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      const raw = buffer.slice(0, newline);
      buffer = "";
      Promise.resolve()
        .then(() => JSON.parse(raw))
        .then(handleRequest)
        .catch(() => ({ status: "browser_unavailable" }))
        .then((response) => socket.end(`${JSON.stringify(response)}\n`));
    });
  });
  server.on("error", () => {});
  server.listen(endpoint);
}

export default async ({ page }) => {
  activePage = page;
  startServer();
};

export { normalizeOrigin };
