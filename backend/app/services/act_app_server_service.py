from __future__ import annotations

import json
import os
import sys
import threading
import tomllib
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import ActSession
from app.services.act_workspace_service import ensure_act_workspace
from app.services.agent_policy_service import AgentPolicyService
from app.services.codex_app_server import CodexAppServerClient
from app.services.product_manager_session_service import ProductManagerSessionService


class ActAppServerError(RuntimeError):
    pass


ACT_INSTRUCTIONS = """You are Act, Eidolon's execution agent. Your main job is to carry out the user's requests using all capabilities available to you, including eidolon functions, files available in your managed root and web search. You should first read relevant context, then make sure user intent is well understood. When needed, ask user for context before acting . Also reject unrealistic/undoable actions. When intent is sufficiently clear, execute the task end-to-end, make reasonable low-consequence decisions yourself, and verify important results when possible. Use `knowledge/` as read-only context and `workspace/` for working files. Only write persistent memory when explicitly requested or approved."""

OBSERVER_INSTRUCTIONS = """You are Observer, Eidolon's read-only analysis agent. Your job is to help the user understand their information, situation, and options. Use relevant Eidolon context to identify connections, patterns, inconsistencies, changes, tradeoffs, and important missing information. Distinguish evidence from inference and give concrete conclusions when justified. You may not modify any files."""

ASSISTANT_INSTRUCTIONS_TEMPLATE = r"""You are Eidolon Assistant, a proactive personal assistant that helps identify useful work Act can perform for the user.

You may read managed root file but have no write/modify file access.

When prompted for an assessment:

Identify concrete, worthwhile ways Act could help the user, based on the user's todos and goals as well as user's broader situation.

First, gather relevant context(s) from Eidolon functions, workspace files (notably: `knowledge\assistant`), internet search. Stop when further context is less likely to meaningfully contribute.

Consider, when useful:

- current and recent todos, goals, commitments, and deadlines
- recent conversations, decisions, interests, and unresolved threads
- ongoing activities and changes in the user's situation
- relevant external information from the internet
- prior Assistant proposal history

Treat todos and goals as important indicators of the user's priorities, not as the only source of possible actions.

Infer the context and intent behind the todo before proposing anything. Do not act on an isolated todo, note, or fact if its meaning is ambiguous. A proposal should only be made when you have enough evidence to be reasonably confident that:

1. you understand the user's situation correctly,
2. the proposed work is actually useful now,
3. the expected benefit justifies interrupting the user.

Do not treat inferred intentions as established facts. State any material assumptions in the proposal.

When context is needed, ask a concise clarification only when the answer is critical enough to unlock a meaningful Act. Otherwise, defer the proposal.

Look for opportunities such as:

- advancing a goal by adding sub goals, completing sub goals or add todo's to advance sub goals.
- preparing for something the user is likely to need soon.
- completing a specific todo, like sending email, when context is sufficient.
- Broader personal recommendations only when grounded in the user's expressed priorities and circumstances. This should be relatively rare.
- researching opportunities that can meaningfully benefit user and advance his goals.
- identifying an emerging issues or risks

Use your own judgment. Do not force a proposal merely because something could theoretically be done. Prefer high-value, timely, specific interventions over generic productivity suggestions.

You should make sure the proposed action is within Act agent's capability.

Before proposing anything, read the Assistant proposal history. Do not repeat an existing or materially similar proposal unless circumstances have materially changed. If replacing a previous proposal, include `replaces_proposal_id` and clearly state the `material_change` that makes the new proposal warranted.

Use the private plan approval request only when you have a concrete and useful plan for Act to execute, including the exact instruction Act should receive after approval.

Create at most 5 new proposals in this entire thread. Replacements using `replaces_proposal_id` and `material_change` do not count toward this limit and are unlimited.

If you do not find a sufficiently useful, well-grounded opportunity, finish quietly without submitting a proposal.



Backend stores proposals and outcomes. Do not write history yourself.

Attach source items in references as `todo:<Notion page ID>` or `goal:<Atlas goal ID>`.

The following catalog describes Act capabilities, not tools you can call:
[ACT_CAPABILITY_CATALOG]
"""

AGENT_INSTRUCTION_TEMPLATES = {
    "act": ACT_INSTRUCTIONS,
    "observer": OBSERVER_INSTRUCTIONS,
    "assistant": ASSISTANT_INSTRUCTIONS_TEMPLATE,
}


def render_agent_instructions(agent_id: str, act_catalog: object) -> str:
    try:
        instructions = AGENT_INSTRUCTION_TEMPLATES[agent_id]
    except KeyError:
        raise ActAppServerError(f"Unsupported managed agent: {agent_id}") from None
    return instructions.replace("[ACT_CAPABILITY_CATALOG]", json.dumps(act_catalog))


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + "=" + _toml(item) for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    return json.dumps(value)


def managed_config(agent_id: str, root: Path) -> dict:
    # No shared config writes. Disable all inherited MCP servers/plugins before
    # starting the managed process, including the public trusted-user MCP server.
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = home / "config.toml"
    inherited = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    filesystem = {":minimal": "read", str(root): "read"}
    if agent_id == "act":
        filesystem[str(root / "workspace")] = "write"
        filesystem[str(root / "memory")] = "write"
    return {
        "permissions.eidolon_agent": {"filesystem": filesystem, "network": {"enabled": False}},
        "default_permissions": "eidolon_agent",
        "approval_policy": "never",
        "windows.sandbox": "elevated",
        "mcp_servers": {name: {**entry, "enabled": False} for name, entry in inherited.get("mcp_servers", {}).items()},
        "plugins": {name: {"enabled": False} for name in inherited.get("plugins", {})},
        "apps._default.enabled": False,
        "features.apps": False,
        "features.multi_agent": False,
        "features.hooks": False,
        "features.js_repl": False,
        "features.remote_control": False,
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
                {plugin_id: {"enabled": False} for plugin_id in installed_plugins}
            )
            for name in discovered_mcp_names:
                entry = {**config["mcp_servers"].get(name, {})}
                # Plugin-owned MCP servers do not necessarily have a matching
                # user-config entry. A disabled placeholder is sufficient to
                # override that inherited surface without executing a command.
                if not entry:
                    entry["command"] = "disabled"
                entry["enabled"] = False
                config["mcp_servers"][name] = entry
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
                if server_info is not None or tools or resources or templates:
                    raise ActAppServerError(
                        "Managed agent startup refused an inherited non-Eidolon MCP surface"
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

    def _thread_options(self, db: Session, session_id: int) -> dict:
        workspace = self.ensure_ready(db)
        policy = AgentPolicyService(db)
        token = policy.issue(self.agent_id, session_id)
        # The token belongs only to the trusted MCP child, never the shell env.
        config = {"mcp_servers.eidolon": {
            "command": sys.executable,
            "args": ["-m", "app.mcp_server", "--agent"],
            "cwd": str(Path(__file__).resolve().parents[2]),
            "env": {"EIDOLON_AGENT_TOKEN": token},
            "enabled": True, "required": True,
            "default_tools_approval_mode": "approve",
            "startup_timeout_sec": 30, "tool_timeout_sec": 180,
        }}
        instructions = render_agent_instructions(self.agent_id, policy.act_catalog())
        return {"cwd": workspace.root, "permissions": "eidolon_agent", "approval_policy": "never", "config": config, "developer_instructions": instructions}

    def start_thread(self, db: Session, *, model: str | None, reasoning_effort: str | None, session_id: int) -> str:
        with self._ready_lock:
            self.stop()
            self._session_id = session_id
            try:
                thread_id = self.sessions.start_thread(
                    model=model,
                    reasoning_effort=reasoning_effort,
                    **self._thread_options(db, session_id),
                )
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
                raise ActAppServerError(
                    f"Could not start the managed agent thread: {type(exc).__name__}"
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
