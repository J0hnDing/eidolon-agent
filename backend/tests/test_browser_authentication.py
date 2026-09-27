import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import BrowserAuthenticationSecret
from app.services.browser_authentication_service import (
    BROWSER_AUTHENTICATION_IDENTITIES,
    BrowserAuthenticationCapability,
    BrowserAuthenticationError,
    BrowserAuthenticationService,
    normalized_origin,
)
from app.services.secret_store import FakeSecretStore

USERNAME = "BROWSER_USERNAME_SENTINEL_22d1"
PASSWORD = "BROWSER_PASSWORD_SENTINEL_9a67"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


class CountingSecretStore(FakeSecretStore):
    get_count: int = 0

    def get(self, reference: str, *, namespace: str = "github") -> str:
        self.get_count += 1
        return super().get(reference, namespace=namespace)


class FakeBridge:
    def __init__(self, *, url: str, status: str = "login_required", result: str = "authenticated") -> None:
        self.url = url
        self.status_value = status
        self.result = result
        self.credentials = None

    def status(self, identity):  # noqa: ANN001, ANN201
        del identity
        return {"status": self.status_value, "url": self.url}

    def authenticate(self, identity, credentials):  # noqa: ANN001, ANN201
        del identity
        self.credentials = credentials
        return {"status": self.result}


def test_secret_lifecycle_keeps_credentials_out_of_sqlite_and_status(db: Session) -> None:
    store = FakeSecretStore()
    service = BrowserAuthenticationService(db, secret_store=store)

    status = service.put("uoft", USERNAME, PASSWORD)

    assert status["configured"] is True
    assert USERNAME not in repr(status)
    assert PASSWORD not in repr(status)
    row = db.scalar(select(BrowserAuthenticationSecret))
    assert row is not None
    assert USERNAME not in repr(row.__dict__)
    assert PASSWORD not in repr(row.__dict__)
    stored = json.loads(store.values[row.secret_reference])
    assert stored == {"username": USERNAME, "password": PASSWORD}
    assert store.namespaces[row.secret_reference] == "browser_authentication"

    service.delete("uoft")
    assert db.get(BrowserAuthenticationSecret, "uoft") is None
    assert store.values == {}


def test_failed_replacement_preserves_previous_secret(db: Session) -> None:
    store = FakeSecretStore()
    service = BrowserAuthenticationService(db, secret_store=store)
    service.put("uoft", USERNAME, PASSWORD)
    first = db.get(BrowserAuthenticationSecret, "uoft").secret_reference
    store.fail_put = True

    with pytest.raises(BrowserAuthenticationError) as exc_info:
        service.put("uoft", "replacement", "replacement-password")

    assert exc_info.value.error_type == "secret_store_unavailable"
    assert db.get(BrowserAuthenticationSecret, "uoft").secret_reference == first
    assert json.loads(store.values[first]) == {"username": USERNAME, "password": PASSWORD}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://idpz.utorauth.utoronto.ca/idp/login", "https://idpz.utorauth.utoronto.ca"),
        ("https://weblogin.utoronto.ca/path", "https://weblogin.utoronto.ca"),
        ("https://WEBLOGIN.UTORONTO.CA:443/path", "https://weblogin.utoronto.ca"),
        ("https://weblogin.utoronto.ca:444/path", "https://weblogin.utoronto.ca:444"),
        ("https://weblogin.utoronto.ca.evil.example/path", "https://weblogin.utoronto.ca.evil.example"),
        ("javascript:alert(1)", None),
    ],
)
def test_origin_normalization_is_exact(url: str, expected: str | None) -> None:
    assert normalized_origin(url) == expected


def test_capability_refuses_deceptive_origin_before_reading_secret(db: Session) -> None:
    store = CountingSecretStore()
    BrowserAuthenticationService(db, secret_store=store).put("uoft", USERNAME, PASSWORD)
    bridge = FakeBridge(url="https://weblogin.utoronto.ca.evil.example/login")

    result = BrowserAuthenticationCapability(db, bridge=bridge, secret_store=store).authenticate("uoft")

    assert result == {"identity": "uoft", "status": "unsupported_origin"}
    assert store.get_count == 0
    assert bridge.credentials is None


def test_capability_reuses_session_without_reading_secret(db: Session) -> None:
    store = CountingSecretStore()
    BrowserAuthenticationService(db, secret_store=store).put("uoft", USERNAME, PASSWORD)
    bridge = FakeBridge(url="https://q.utoronto.ca/courses/1", status="already_authenticated")

    result = BrowserAuthenticationCapability(db, bridge=bridge, secret_store=store).authenticate("uoft")

    assert result == {"identity": "uoft", "status": "already_authenticated"}
    assert store.get_count == 0


def test_capability_surfaces_mfa_and_sanitizes_failure_states(db: Session) -> None:
    store = CountingSecretStore()
    BrowserAuthenticationService(db, secret_store=store).put("uoft", USERNAME, PASSWORD)
    mfa = FakeBridge(url="https://weblogin.utoronto.ca/login", status="mfa_required")
    assert BrowserAuthenticationCapability(db, bridge=mfa, secret_store=store).authenticate("uoft") == {
        "identity": "uoft",
        "status": "mfa_required",
    }
    assert store.get_count == 0

    failed = FakeBridge(
        url="https://weblogin.utoronto.ca/login",
        result="authentication_failed",
    )
    result = BrowserAuthenticationCapability(db, bridge=failed, secret_store=store).authenticate("uoft")
    assert result == {"identity": "uoft", "status": "authentication_failed"}
    assert failed.credentials.username == USERNAME
    assert failed.credentials.password == PASSWORD
    assert USERNAME not in repr(result)
    assert PASSWORD not in repr(result)


def test_uoft_identity_has_only_exact_https_login_origins() -> None:
    identity = BROWSER_AUTHENTICATION_IDENTITIES["uoft"]
    assert identity.login_origins == (
        "https://idpz.utorauth.utoronto.ca",
        "https://weblogin.utoronto.ca",
    )


def test_init_page_bridge_fills_same_page_and_returns_only_sanitized_state() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Playwright bridge test")
    module = Path(__file__).resolve().parents[1] / "app" / "browser_authentication_init_page.mjs"
    endpoint = rf"\\.\pipe\eidolon-browser-auth-test-{uuid4().hex}"
    script = r"""
import net from 'node:net';
process.env.EIDOLON_BROWSER_AUTH_ENDPOINT = process.argv[1];
process.env.EIDOLON_BROWSER_AUTH_TOKEN = 'bridge-token';
const bridge = await import(new URL(`file:///${process.argv[2].replaceAll('\\', '/')}`));
const fills = [];
const page = {
  currentUrl: 'https://idpz.utorauth.utoronto.ca/idp/login',
  bodyText: '',
  nextUrl: 'https://q.utoronto.ca/courses/1',
  url() { return this.currentUrl; },
  isClosed() { return false; },
  locator(selector) {
    if (selector === 'body') return { innerText: async () => page.bodyText };
    return {
      first() { return this; },
      isVisible: async () => ['#username', '#password', '#submit'].includes(selector),
      fill: async (value) => fills.push([selector, value]),
      click: async () => { page.currentUrl = page.nextUrl; },
    };
  },
  waitForLoadState: async () => {},
  waitForTimeout: async () => {},
};
await bridge.default({ page });
await new Promise(resolve => setTimeout(resolve, 50));
const request = payload => new Promise((resolve, reject) => {
  const client = net.createConnection(process.argv[1]);
  let response = '';
  client.setEncoding('utf8');
  client.on('connect', () => client.write(`${JSON.stringify({ token: 'bridge-token', ...payload })}\n`));
  client.on('data', chunk => { response += chunk; });
  client.on('end', () => resolve(JSON.parse(response)));
  client.on('error', reject);
});
const common = {
  identity: 'uoft',
  loginOrigins: ['https://idpz.utorauth.utoronto.ca', 'https://weblogin.utoronto.ca'],
  authenticatedOrigins: ['https://q.utoronto.ca'],
};
const result = await request({
  ...common,
  action: 'authenticate',
  usernameSelectors: ['#username'],
  passwordSelectors: ['#password'],
  submitSelectors: ['#submit'],
  username: 'NODE_USERNAME_SENTINEL',
  password: 'NODE_PASSWORD_SENTINEL',
});
page.currentUrl = 'https://idpz.utorauth.utoronto.ca/idp/login';
page.nextUrl = 'https://markus.teach.cs.toronto.edu/markus/';
page.bodyText = 'Authentication was successful but no corresponding user was found in the MarkUs database.';
const failed = await request({
  ...common,
  action: 'authenticate',
  usernameSelectors: ['#username'],
  passwordSelectors: ['#password'],
  submitSelectors: ['#submit'],
  username: 'FAILURE_USERNAME_SENTINEL',
  password: 'FAILURE_PASSWORD_SENTINEL',
});
page.currentUrl = 'https://weblogin.utoronto.ca.evil.example/login';
page.bodyText = '';
const refused = await request({
  ...common,
  action: 'authenticate',
  usernameSelectors: ['#username'],
  passwordSelectors: ['#password'],
  submitSelectors: ['#submit'],
  username: 'SECOND_USERNAME_SENTINEL',
  password: 'SECOND_PASSWORD_SENTINEL',
});
console.log(JSON.stringify({ result, failed, refused, fillCount: fills.length }));
process.exit(0);
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script, endpoint, str(module)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = json.loads(completed.stdout)
    assert output["result"]["status"] == "already_authenticated"
    assert output["failed"]["status"] == "authentication_failed"
    assert output["refused"]["status"] == "unsupported_origin"
    assert output["fillCount"] == 4
    assert "NODE_USERNAME_SENTINEL" not in completed.stdout
    assert "NODE_PASSWORD_SENTINEL" not in completed.stdout
    assert "FAILURE_USERNAME_SENTINEL" not in completed.stdout
    assert "FAILURE_PASSWORD_SENTINEL" not in completed.stdout
