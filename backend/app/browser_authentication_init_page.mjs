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

async function pageState(activePage, loginOrigins, authenticatedOrigins) {
  if (!activePage || activePage.isClosed()) return { status: "browser_unavailable", url: "" };
  const url = activePage.url();
  const origin = normalizeOrigin(url);
  if (authenticatedOrigins.includes(origin)) return { status: "already_authenticated", url };
  const text = await activePage.locator("body").innerText({ timeout: 2_000 }).catch(() => "");
  if (MFA_PATTERN.test(text)) return { status: "mfa_required", url };
  if (loginOrigins.includes(origin)) return { status: "login_required", url };
  return { status: "unsupported_origin", url };
}

async function firstVisible(activePage, selectors) {
  for (const selector of selectors) {
    const locator = activePage.locator(selector).first();
    if (await locator.isVisible({ timeout: 1_000 }).catch(() => false)) return locator;
  }
  return null;
}

function assertLoginOrigin(activePage, loginOrigins) {
  if (!activePage || activePage.isClosed() || !loginOrigins.includes(normalizeOrigin(activePage.url()))) {
    throw new Error("origin_changed");
  }
}

async function authenticate(activePage, request) {
  const loginOrigins = request.loginOrigins ?? [];
  const authenticatedOrigins = request.authenticatedOrigins ?? [];
  const initial = await pageState(activePage, loginOrigins, authenticatedOrigins);
  if (initial.status !== "login_required") return initial;
  assertLoginOrigin(activePage, loginOrigins);
  const username = await firstVisible(activePage, request.usernameSelectors ?? []);
  const password = await firstVisible(activePage, request.passwordSelectors ?? []);
  const submit = await firstVisible(activePage, request.submitSelectors ?? []);
  if (!username || !password || !submit) return { status: "authentication_failed", url: activePage.url() };
  assertLoginOrigin(activePage, loginOrigins);
  await username.fill(request.username);
  assertLoginOrigin(activePage, loginOrigins);
  await password.fill(request.password);
  assertLoginOrigin(activePage, loginOrigins);
  await submit.click();
  await activePage.waitForLoadState("domcontentloaded", { timeout: 10_000 }).catch(() => {});
  await activePage.waitForTimeout(750);
  const current = await pageState(activePage, loginOrigins, authenticatedOrigins);
  if (current.status === "already_authenticated" || current.status === "mfa_required") return current;
  const text = await activePage.locator("body").innerText({ timeout: 2_000 }).catch(() => "");
  if (FAILURE_PATTERN.test(text) || current.status === "login_required") {
    return { status: "authentication_failed", url: activePage.url() };
  }
  return { status: "user_action_required", url: activePage.url() };
}

export function createPageAuthentication(getPage) {
  return async (request) => {
    const page = await getPage();
    const loginOrigins = Array.isArray(request?.loginOrigins) ? request.loginOrigins : [];
    const authenticatedOrigins = Array.isArray(request?.authenticatedOrigins) ? request.authenticatedOrigins : [];
    if (request?.action === "status") return pageState(page, loginOrigins, authenticatedOrigins);
    if (request?.action === "authenticate") return authenticate(page, request);
    return { status: "browser_unavailable" };
  };
}

const pageAuthentication = createPageAuthentication(() => activePage);
async function handleRequest(request) {
  if (!request || request.token !== capability) return { status: "browser_unavailable" };
  return pageAuthentication(request);
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
