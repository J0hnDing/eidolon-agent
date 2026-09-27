# Act Browser Authentication

Browser authentication is a trusted backend/runtime capability for Act. It is not an Atlas feature, generated skill, Eidolon function, integration-registry operation, or public MCP tool.

## Identity and secret boundary

The backend owns a checked-in identity registry. Each identity fixes its display name, exact HTTPS login origins, authenticated origins, and login-form selectors. Callers supply only the identity id; they cannot supply an origin, URL, selector, username, or password.

Settings stores each identity's username and password as one bounded JSON value in Windows Credential Manager. SQLite stores only the identity id, secret-store implementation id, opaque reference, and timestamps. The username, password, reference, authenticated request, and form-fill result are excluded from prompts, model context, tool arguments and outputs, logs, audits, runtime files, Atlas, and public MCP.

The first identity is `uoft`:

- login origins: `https://idpz.utorauth.utoronto.ca` and `https://weblogin.utoronto.ca`;
- authenticated Quercus origin: `https://q.utoronto.ca`;
- intended use: U of T Weblogin and Quercus.

## Runtime flow

Act navigates with its existing Playwright MCP tools. Playwright's backend-configured init-page module holds the actual current `Page` and exposes a per-turn authenticated local pipe only to the private Eidolon MCP child. `browser.authenticate` first asks that bridge for the real current URL. The backend canonicalizes its origin and refuses unsupported schemes, ports, hosts, deceptive suffixes, or caller-supplied alternatives before reading the secret.

Act must begin from the exact service link supplied by the user or an authenticated source such as the relevant course page. The capability authenticates the current page; it does not discover or guess tenant-, course-, or site-specific application instances.

For an allowed login page, the backend loads the secret and sends it over the per-turn pipe. The bridge independently rechecks the page origin immediately before filling the username, password, and submit control. It never logs or returns the payload. The private tool returns only the identity and one bounded state.

The persistent Playwright profile retains cookies and storage. If the page is already on an authenticated origin, the capability reports `already_authenticated` without loading or reinjecting the secret.

## MFA and failures

MFA is never bypassed. A recognized interactive challenge returns `mfa_required`; the visible browser remains available for the user to complete it. Act can continue after the browser reaches an authenticated page, including after a managed-process restart through the same persistent profile.

Failures are normalized to safe states or fixed errors such as unsupported origin, missing/unavailable credential, unavailable browser bridge, authentication failure, or additional user action. Provider text, locators, page content, submitted values, and low-level bridge errors are not returned.

## Settings API

- `GET /settings/integrations/browser-authentication`
- `PUT /settings/integrations/browser-authentication/{identityId}`
- `DELETE /settings/integrations/browser-authentication/{identityId}`

Only registry identities are accepted. There is no API for creating identity entries.
