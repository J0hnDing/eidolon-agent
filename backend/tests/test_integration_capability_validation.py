from pathlib import Path

from app.services.capability_scanner import StaticCapabilityScanner


def scan(
    tmp_path: Path,
    source: str,
    *,
    runtime: str = "function",
    declared: set[str] | None = None,
    selected: set[str] | None = None,
):
    (tmp_path / "skill.py").write_text(source, encoding="utf-8")
    return StaticCapabilityScanner().scan(
        tmp_path,
        {"network": ["api.github.com"], "filesystem_write": [], "secrets": []},
        runtime=runtime,
        declared_integration_operations=declared or set(),
        selected_integration_operations=selected or set(),
    )


def test_literal_declared_selected_helper_call_is_allowed(tmp_path: Path) -> None:
    result = scan(
        tmp_path,
        "import integration_runtime_capabilities\n"
        "result = integration_runtime_capabilities.call(\n"
        "    operation='github.repository.get', input={'owner': 'octo', 'repository': 'demo'}\n"
        ")\n",
        declared={"github.repository.get"},
        selected={"github.repository.get"},
    )
    assert result.ok


def test_non_literal_operation_is_ambiguous_and_does_not_block(tmp_path: Path) -> None:
    result = scan(
        tmp_path,
        "import integration_runtime_capabilities\n"
        "GITHUB_OPERATION = 'github.repository.get'\n"
        "integration_runtime_capabilities.call(operation=GITHUB_OPERATION, input={})\n",
        declared={"github.repository.get"},
        selected={"github.repository.get"},
    )
    assert result.ok
    assert not any(finding.capability == "integration_operation" for finding in result.findings)


def test_literal_undeclared_and_unselected_operations_are_rejected(tmp_path: Path) -> None:
    undeclared = scan(
        tmp_path,
        "import integration_runtime_capabilities\n"
        "integration_runtime_capabilities.call(operation='github.issue.list', input={})\n",
        declared={"github.repository.get"},
        selected={"github.issue.list"},
    )
    assert any(finding.status == "undeclared" for finding in undeclared.findings)

    unselected = scan(
        tmp_path,
        "import integration_runtime_capabilities\n"
        "integration_runtime_capabilities.call(operation='github.repository.get', input={})\n",
        declared={"github.repository.get"},
        selected=set(),
    )
    assert any(finding.status == "blocked" for finding in unselected.findings)


def test_direct_github_secret_store_auth_and_internal_path_access_are_rejected(tmp_path: Path) -> None:
    result = scan(
        tmp_path,
        "import keyring\n"
        "import requests\n"
        "requests.get('https://api.github.com/repos/octo/demo', headers={'Authorization': 'Bearer bad'})\n"
        "path = '/integrations/capabilities/invoke'\n",
    )
    capabilities = {finding.capability for finding in result.findings}
    assert {
        "credential_store",
        "direct_github_access",
        "authentication_header",
        "integration_internal_path",
    }.issubset(capabilities)

    ctypes_result = scan(
        tmp_path,
        "import ctypes\n"
        "advapi = ctypes.WinDLL('Advapi32.dll')\n"
        "advapi.CredReadW('target', 1, 0, None)\n",
    )
    assert any(finding.capability == "credential_store" for finding in ctypes_result.findings)


def test_direct_notion_and_integration_settings_access_are_rejected(tmp_path: Path) -> None:
    result = scan(
        tmp_path,
        "import requests\n"
        "requests.post('https://api.notion.com/v1/pages')\n"
        "settings = '/settings/integrations/notion'\n"
        "token = os.environ['NOTION_TOKEN']\n",
    )
    capabilities = {finding.capability for finding in result.findings}
    assert {"direct_notion_access", "integration_settings_access", "secrets"}.issubset(capabilities)


def test_direct_google_calendar_and_oauth_secret_access_are_rejected(tmp_path: Path) -> None:
    result = scan(
        tmp_path,
        "import os\n"
        "import requests\n"
        "requests.get('https://www.googleapis.com/calendar/v3/calendars/primary/events')\n"
        "requests.get('https://gmail.googleapis.com/gmail/v1/users/me/messages')\n"
        "requests.post('https://api.telegram.org/bot-token/sendMessage')\n"
        "token = os.environ['GMAIL_OAUTH_REFRESH_TOKEN']\n",
    )
    capabilities = {finding.capability for finding in result.findings}
    assert {"direct_google_access", "direct_telegram_access", "secrets"}.issubset(capabilities)


def test_browser_integration_invocation_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("app = object()\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "fetch('/web-apps/capabilities/integrations/invoke', {method: 'POST'});",
        encoding="utf-8",
    )
    result = StaticCapabilityScanner().scan(
        tmp_path,
        {"network": [], "filesystem_write": [], "secrets": []},
        runtime="web_app",
        declared_integration_operations={"github.repository.get"},
        selected_integration_operations={"github.repository.get"},
    )
    assert any(finding.capability == "browser_integration" for finding in result.findings)
