from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import tomllib
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import ActSession
from app.services.act_workspace_service import ensure_act_workspace
from app.services.agent_policy_service import AgentPolicyService
from app.services.browser_authentication_bridge import browser_authentication_bridge_endpoint
from app.services.codex_app_server import CodexAppServerClient, CodexAppServerError
from app.services.product_manager_session_service import ProductManagerSessionService


class ActAppServerError(RuntimeError):
    pass


def _managed_start_error_detail(exc: BaseException, thread_options: dict | None) -> str:
    detail = " ".join(str(exc).split())
    sensitive_values: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                normalized_key = str(child_key).lower().replace("-", "_")
                if any(marker in normalized_key for marker in ("token", "secret", "password", "credential", "api_key")):
                    if isinstance(child, str) and len(child) >= 8:
                        sensitive_values.add(child)
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(thread_options or {})
    for sensitive_value in sorted(sensitive_values, key=len, reverse=True):
        detail = detail.replace(sensitive_value, "[redacted]")
    detail = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", detail)
    return detail[:320] or "No App Server error detail was returned"


ACT_INSTRUCTIONS = """You are Act, Eidolon's execution agent. Your main job is to carry out the user's requests using all capabilities available to you, including eidolon functions, files available in your managed root and web search. You should first read relevant context, then make sure user intent is well understood. When needed, ask user for context before acting. When intent is sufficiently clear, attempt the requested work end-to-end, make reasonable low-consequence decisions yourself, and verify important results when possible. Reject a request only when an applicable policy or explicit rule prohibits it, it is infeasible with available capabilities, or an actual failure prevents completion. Do not assume that graded coursework is prohibited merely because it is graded; if the user asks you to complete or submit it, attempt the task unless a specific applicable restriction prevents it. If blocked, explain the concrete restriction or failure and complete any permitted, feasible portion. Use `knowledge/` as read-only context and `workspace/` for working files. You may add to memory/chat_memory if you believe information is valuable enough to be recorded and reused.

Browser automation is provided by the available Playwright MCP browser tools. Use those tools for browser workflows. When a supported website requires login, navigate to its real login page and use the Browser authenticate tool with the configured identity; credentials remain backend-only, and interactive MFA must be completed by the user. Do not test for or import `@oai/sky` from the shell: that package belongs to a separate desktop Computer Use runtime and is not Act's browser capability."""

OBSERVER_INSTRUCTIONS = """You are Observer, Eidolon's read-only analysis agent. Your job is to help the user understand their information, situation, and options. Use relevant Eidolon context to identify connections, patterns, inconsistencies, changes, tradeoffs, and important missing information. Distinguish evidence from inference and give concrete conclusions when justified. You may not modify any files."""

ASSISTANT_INSTRUCTIONS_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "agent_instructions" / "assistant.txt"
).read_text(encoding="utf-8").rstrip("\n")

AGENT_INSTRUCTION_TEMPLATES = {
    "act": ACT_INSTRUCTIONS,
    "observer": OBSERVER_INSTRUCTIONS,
    "assistant": ASSISTANT_INSTRUCTIONS_TEMPLATE,
}

ACT_ALLOWED_INHERITED_MCP_SERVERS = frozenset({"playwright"})
MANAGED_ALLOWED_INHERITED_MCP_SERVERS = frozenset(
    {
        "codex-security",
        "openai-api-key-local-confirmation",
        "openaiDeveloperDocs",
    }
)
OPENAI_DEVELOPER_DOCS_MCP_URL = "https://developers.openai.com/mcp"
MANAGED_ALLOWED_INHERITED_MCP_TOOLS = {
    "openai-api-key-local-confirmation": frozenset(
        {"confirm_openai_api_key_local_destination"}
    )
}


def _is_allowed_managed_plugin(plugin_id: str) -> bool:
    return plugin_id == "codex-security" or plugin_id.startswith("codex-security@")


def render_agent_instructions(agent_id: str, act_catalog: object, deterministic_catalog: object = ()) -> str:
    try:
        instructions = AGENT_INSTRUCTION_TEMPLATES[agent_id]
    except KeyError:
        raise ActAppServerError(f"Unsupported managed agent: {agent_id}") from None
    return (instructions.replace("[ACT_CAPABILITY_CATALOG]", json.dumps(act_catalog))
            .replace("[DETERMINISTIC_FUNCTION_CATALOG]", json.dumps(deterministic_catalog)))


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + "=" + _toml(item) for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    return json.dumps(value)


def managed_config(agent_id: str, root: Path) -> dict:
    # No shared config writes. Disable inherited MCP servers/plugins before
    # starting the managed process, except for Act's explicit browser allowlist.
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = home / "config.toml"
    inherited = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    docs_mcp = inherited.get("mcp_servers", {}).get("openaiDeveloperDocs")
    if docs_mcp is not None and docs_mcp.get("url") != OPENAI_DEVELOPER_DOCS_MCP_URL:
        raise ActAppServerError(
            "Managed agents only allow the official OpenAI Developer Docs MCP endpoint"
        )
    filesystem = {":root": "read", ":minimal": "read", str(root): "read"}
    if agent_id == "act":
        filesystem[str(root / "workspace")] = "write"
        filesystem[str(root / "memory")] = "write"
    mcp_servers = {
        name: {
            **entry,
            "enabled": (
                name in MANAGED_ALLOWED_INHERITED_MCP_SERVERS
                or agent_id == "act" and name in ACT_ALLOWED_INHERITED_MCP_SERVERS
            ),
        }
        for name, entry in inherited.get("mcp_servers", {}).items()
    }
    if agent_id == "act":
        for name in ACT_ALLOWED_INHERITED_MCP_SERVERS & mcp_servers.keys():
            mcp_servers[name]["default_tools_approval_mode"] = "approve"
            mcp_servers[name]["required"] = True
    return {
        "permissions.eidolon_agent": {
            "filesystem": filesystem,
            "network": {"enabled": agent_id == "act"},
        },
        "default_permissions": "eidolon_agent",
        "approval_policy": "never",
        "windows.sandbox": "elevated",
        "mcp_servers": mcp_servers,
        "plugins": {
            name: {"enabled": _is_allowed_managed_plugin(name)}
            for name in inherited.get("plugins", {})
        },
        "apps._default.enabled": False,
        "features.apps": False,
        "features.multi_agent": False,
        "features.hooks": False,
        "features.js_repl": False,
        "features.remote_control": False,
        # A stale installed remote plugin can fail Codex config loading even
        # when the backend disables its MCP server and plugin by name.
        "features.remote_plugin": False,
        "web_search": "disabled" if agent_id == "observer" else "live",
        "shell_environment_policy": {"inherit": "core", "set": {}, "exclude": ["*TOKEN*", "*SECRET*", "*KEY*", "*PASSWORD*", "*CREDENTIAL*"]},
    }


class ActAppServerService:
    def __init__(self, client: CodexAppServerClient | None = None, *, agent_id: str = "act") -> None:
        self.agent_id = agent_id
        self.client = client or CodexAppServerClient()
        self.sessions = ProductManagerSessionService(self.client)
        self._ready_lock = threading.RLock()
        self._session_id: int | None = None
        self._thread_tool_config: dict[str, object] = {}

    def ensure_ready(self, db: Session):
        workspace = ensure_act_workspace()
        with self._ready_lock:
            config = managed_config(self.agent_id, workspace.root)
            self._apply_process_config(config)
            try:
                self.client.start()
                installed_plugins = self._installed_plugin_ids()
                discovered_mcp_names = {
                    entry["name"]
                    for entry in self._mcp_inventory()
                    if entry["name"] != "eidolon"
                }
            finally:
                self.client.stop()
            config["plugins"].update(
                {
                    plugin_id: {"enabled": _is_allowed_managed_plugin(plugin_id)}
                    for plugin_id in installed_plugins
                }
            )
            for name in discovered_mcp_names:
                entry = {**config["mcp_servers"].get(name, {})}
                if (
                    name in MANAGED_ALLOWED_INHERITED_MCP_SERVERS
                    or self.agent_id == "act"
                    and name in ACT_ALLOWED_INHERITED_MCP_SERVERS
                ):
                    continue
                # Plugin-owned MCP servers do not necessarily have a matching
                # user-config entry. A disabled placeholder is sufficient to
                # override that inherited surface without executing a command.
                if not entry:
                    entry["command"] = "disabled"
                entry["enabled"] = False
                config["mcp_servers"][name] = entry
            self._thread_tool_config = {
                **{
                    # Codex validates transport even for disabled thread overrides.
                    f"mcp_servers.{name}": {"command": "disabled", "enabled": False}
                    for name, entry in config["mcp_servers"].items()
                    if name != "eidolon"
                    and name not in MANAGED_ALLOWED_INHERITED_MCP_SERVERS
                    and not (
                        self.agent_id == "act"
                        and name in ACT_ALLOWED_INHERITED_MCP_SERVERS
                    )
                },
                **{
                    f"plugins.{plugin_id}": {"enabled": False}
                    for plugin_id in config["plugins"]
                    if not _is_allowed_managed_plugin(plugin_id)
                },
                "apps._default.enabled": False,
                **{
                    key: value
                    for key, value in config.items()
                    if key.startswith("features.")
                },
            }
            self._apply_process_config(config)
            self.client.start()
            self._verify_mcp_inventory(require_private_eidolon=False)
        return workspace

    def _apply_process_config(self, config: dict) -> None:
        self.client.extra_args = tuple(
            part
            for key, value in config.items()
            for part in ("-c", key + "=" + _toml(value))
        )

    def _installed_plugin_ids(self) -> set[str]:
        try:
            result = self.client.request("plugin/installed", {}, timeout=30)
        except Exception as exc:
            raise ActAppServerError(
                f"Could not inspect installed plugins: {type(exc).__name__}"
            ) from None
        marketplaces = result.get("marketplaces")
        if not isinstance(marketplaces, list):
            raise ActAppServerError("Codex returned an invalid installed-plugin inventory")
        plugin_ids: set[str] = set()
        for marketplace in marketplaces:
            if not isinstance(marketplace, dict) or not isinstance(
                marketplace.get("plugins"), list
            ):
                raise ActAppServerError("Codex returned an invalid installed-plugin inventory")
            for plugin in marketplace["plugins"]:
                if not isinstance(plugin, dict) or plugin.get("installed") is not True:
                    continue
                plugin_id = plugin.get("id")
                if not isinstance(plugin_id, str) or not plugin_id:
                    raise ActAppServerError(
                        "Codex returned an invalid installed-plugin inventory"
                    )
                plugin_ids.add(plugin_id)
        return plugin_ids

    def _mcp_inventory(self, *, thread_id: str | None = None) -> list[dict]:
        inventory: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, str] = {}
            if thread_id is not None:
                params["threadId"] = thread_id
            if cursor is not None:
                params["cursor"] = cursor
            try:
                result = self.client.request("mcpServerStatus/list", params, timeout=30)
            except Exception as exc:
                raise ActAppServerError(
                    f"Could not verify the managed MCP inventory: {type(exc).__name__}"
                ) from None
            page = result.get("data")
            if not isinstance(page, list):
                raise ActAppServerError("Codex returned an invalid managed MCP inventory")
            for entry in page:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise ActAppServerError(
                        "Codex returned an invalid managed MCP inventory"
                    )
                inventory.append(entry)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return inventory
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise ActAppServerError("Codex returned an invalid managed MCP inventory")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _verify_mcp_inventory(
        self,
        *,
        require_private_eidolon: bool,
        thread_id: str | None = None,
    ) -> None:
        inventory = self._mcp_inventory(thread_id=thread_id)
        eidolon_ready = False
        act_browser_ready = False
        for entry in inventory:
            tools = entry.get("tools")
            resources = entry.get("resources")
            templates = entry.get("resourceTemplates")
            server_info = entry.get("serverInfo")
            if (
                not isinstance(tools, dict)
                or not isinstance(resources, list)
                or not isinstance(templates, list)
                or (server_info is not None and not isinstance(server_info, dict))
            ):
                raise ActAppServerError("Codex returned an invalid managed MCP inventory")
            if entry["name"] != "eidolon":
                if entry["name"] in MANAGED_ALLOWED_INHERITED_MCP_SERVERS:
                    allowed_tools = MANAGED_ALLOWED_INHERITED_MCP_TOOLS.get(
                        entry["name"]
                    )
                    if allowed_tools is not None and (
                        not set(tools).issubset(allowed_tools)
                        or resources
                        or templates
                        or (
                            server_info is not None
                            and server_info.get("name") != "OpenAI Developers MCP"
                        )
                    ):
                        raise ActAppServerError(
                            "Managed agent startup refused an unexpected tool on "
                            f"allowlisted MCP server: {entry['name']}"
                        )
                    continue
                if (
                    self.agent_id == "act"
                    and entry["name"] in ACT_ALLOWED_INHERITED_MCP_SERVERS
                ):
                    if server_info is not None and tools:
                        act_browser_ready = True
                    continue
                if server_info is not None or tools or resources or templates:
                    raise ActAppServerError(
                        "Managed agent startup refused an inherited non-Eidolon MCP "
                        f"surface: {entry['name']}"
                    )
                continue
            if server_info is not None:
                if server_info.get("name") != "eidolon-agent":
                    raise ActAppServerError(
                        "Managed agent startup refused a non-private Eidolon MCP server"
                    )
                eidolon_ready = True
        if require_private_eidolon and not eidolon_ready:
            raise ActAppServerError("The private Eidolon MCP server did not start safely")
        if self.agent_id == "act" and not act_browser_ready:
            raise ActAppServerError(
                "The Act browser automation MCP server did not start safely"
            )

    def _thread_options(self, db: Session, session_id: int) -> dict:
        workspace = self.ensure_ready(db)
        policy = AgentPolicyService(db)
        token = policy.issue(self.agent_id, session_id)
        eidolon_env = {"EIDOLON_AGENT_TOKEN": token}
        browser_auth_token = ""
        browser_auth_endpoint = ""
        if self.agent_id == "act":
            browser_auth_token = secrets.token_urlsafe(32)
            browser_auth_endpoint = browser_authentication_bridge_endpoint(
                browser_auth_token,
                workspace.root,
            )
            eidolon_env.update(
                {
                    "EIDOLON_BROWSER_AUTH_ENDPOINT": browser_auth_endpoint,
                    "EIDOLON_BROWSER_AUTH_TOKEN": browser_auth_token,
                }
            )
        # The token belongs only to the trusted MCP child, never the shell env.
        config = {
            **self._thread_tool_config,
            "mcp_servers.eidolon": {
                "command": sys.executable,
                "args": ["-m", "app.mcp_server", "--agent"],
                "cwd": str(Path(__file__).resolve().parents[2]),
                "env": eidolon_env,
                "enabled": True,
                "required": True,
                "default_tools_approval_mode": "approve",
                "startup_timeout_sec": 30,
                "tool_timeout_sec": 180,
            },
        }
        if self.agent_id == "act":
            inherited = managed_config(self.agent_id, workspace.root)["mcp_servers"]
            for name in ACT_ALLOWED_INHERITED_MCP_SERVERS:
                browser = inherited.get(name)
                if browser is None or browser.get("enabled") is not True:
                    raise ActAppServerError(
                        "Act browser automation is not configured on this Codex host"
                    )
                browser = {**browser}
                browser_args = list(browser.get("args", []))
                init_page = str(Path(__file__).resolve().parents[1] / "browser_authentication_init_page.mjs")
                if "--init-page" not in browser_args:
                    browser_args.extend(["--init-page", init_page])
                browser["args"] = browser_args
                browser["env"] = {
                    **browser.get("env", {}),
                    "EIDOLON_BROWSER_AUTH_ENDPOINT": browser_auth_endpoint,
                    "EIDOLON_BROWSER_AUTH_TOKEN": browser_auth_token,
                }
                config[f"mcp_servers.{name}"] = browser
        instructions = render_agent_instructions(
            self.agent_id, policy.act_catalog(),
            policy.deterministic_catalog() if self.agent_id == "assistant" else (),
        )
        return {"cwd": workspace.root, "permissions": "eidolon_agent", "approval_policy": "never", "config": config, "developer_instructions": instructions}

    def start_thread(self, db: Session, *, model: str | None, reasoning_effort: str | None, session_id: int) -> str:
        with self._ready_lock:
            self.stop()
            self._session_id = session_id
            phase = "managed-thread configuration"
            thread_options = None
            try:
                thread_options = self._thread_options(db, session_id)
                phase = "Codex thread/start"
                thread_id = self.sessions.start_thread(
                    model=model,
                    reasoning_effort=reasoning_effort,
                    **thread_options,
                )
                phase = "managed MCP verification"
                self._verify_mcp_inventory(
                    require_private_eidolon=True,
                    thread_id=thread_id,
                )
                return thread_id
            except Exception as exc:
                self.stop()
                AgentPolicyService(db).revoke(session_id)
                db.commit()
                if isinstance(exc, ActAppServerError):
                    raise
                if is_missing_rollout_error(exc):
                    raise ActAppServerError("No rollout found for thread id") from None
                detail = ""
                if isinstance(exc, CodexAppServerError):
                    detail = f": {_managed_start_error_detail(exc, thread_options)}"
                raise ActAppServerError(
                    f"Could not start the managed agent thread during {phase}: "
                    f"{type(exc).__name__}{detail}"
                ) from None

    def resume_thread(self, db: Session, thread_id: str, *, model: str | None, reasoning_effort: str | None, session_id: int | None = None) -> None:
        if session_id is None:
            from sqlalchemy import select
            session_id = db.scalar(select(ActSession.id).where(ActSession.codex_thread_id == thread_id))
        if session_id is None:
            raise ActAppServerError("Agent session identity is required")
        with self._ready_lock:
            # Recreate the process at turn boundaries so live threads cannot retain
            # stale tool discovery, credentials or configuration after policy edits.
            self.stop()
            self._session_id = session_id
            try:
                self.sessions.resume_thread(
                    thread_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    **self._thread_options(db, session_id),
                )
                self._verify_mcp_inventory(
                    require_private_eidolon=True,
                    thread_id=thread_id,
                )
            except Exception as exc:
                self.stop()
                AgentPolicyService(db).revoke(session_id)
                db.commit()
                if isinstance(exc, ActAppServerError):
                    raise
                if is_missing_rollout_error(exc):
                    raise ActAppServerError("No rollout found for thread id") from None
                raise ActAppServerError(
                    f"Could not resume the managed agent thread: {type(exc).__name__}"
                ) from None

    def stop(self) -> None:
        self.client.stop()
        self.sessions = ProductManagerSessionService(self.client)


def is_missing_rollout_error(exc: BaseException) -> bool:
    return "no rollout found for thread id" in " ".join(str(exc).lower().split())


act_app_server_service = ActAppServerService()
agent_app_servers = {
    "act": act_app_server_service,
    "observer": ActAppServerService(agent_id="observer"),
    "assistant": ActAppServerService(agent_id="assistant"),
}
