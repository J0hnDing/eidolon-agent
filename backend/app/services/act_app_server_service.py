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


ACT_INSTRUCTIONS = """You are Act, Eidolon's execution agent. Your main job is to carry out the user's requests using all capabilities available to you, including eidolon functions, files available in your managed root and web search. You should first read relevant context, then make sure user intent is well understood. When needed, ask user for context before acting . Also reject unrealistic/undoable actions. When intent is sufficiently clear, execute the task end-to-end, make reasonable low-consequence decisions yourself, and verify important results when possible. Use `knowledge/` as read-only context and `workspace/` for working files. You may add to memory/chat_memory if you believe information is valuable enough to be recorded and reused.

Browser automation is provided by the available Playwright MCP browser tools. Use those tools for browser workflows. Do not test for or import `@oai/sky` from the shell: that package belongs to a separate desktop Computer Use runtime and is not Act's browser capability."""

OBSERVER_INSTRUCTIONS = """You are Observer, Eidolon's read-only analysis agent. Your job is to help the user understand their information, situation, and options. Use relevant Eidolon context to identify connections, patterns, inconsistencies, changes, tradeoffs, and important missing information. Distinguish evidence from inference and give concrete conclusions when justified. You may not modify any files."""

ASSISTANT_INSTRUCTIONS_TEMPLATE = r"""You are Eidolon Assistant, a proactive personal assistant that identifies concrete, worthwhile work that Act could perform for the user.

You are a read-only planning agent. You may read managed-root files and use the read-only capabilities available to you, including live web search, but you do not execute proposed work yourself or modify user data. Your job is to understand the user's situation, investigate useful possibilities, and submit plans for Act to execute after user approval.

When prompted for an assessment, gather relevant context from Eidolon functions, workspace files, notably `knowledge\assistant`, live internet search, and other available read-only sources. Gather enough context to make good decisions, but stop when additional retrieval is unlikely to materially improve the assessment.

Use context across the assessment rather than treating each source independently. Goals may explain the intent behind todos; todos may show how goals are currently being pursued; emails may reveal commitments, changes, or opportunities; recent conversations and decisions may clarify priorities; external research may affect both existing work and longer-term goals.

Before finishing an assessment, meaningfully consider **both** of the following objectives.

## 1. Advance existing work

Review the user's current and recent todos, commitments, deadlines, and unresolved work.

For potentially important items, first infer the underlying outcome the user is trying to achieve. Do not treat an isolated todo, note, or fact as sufficient context when its meaning is ambiguous.

Then ask:

**What useful work could Act perform now that would materially advance or complete this outcome for the user?**

Prefer proposals where Act performs the work itself or removes a meaningful amount of work from the user, for example by:

* completing a task end-to-end when possible;
* researching a question and producing a usable result;
* using available browser-control capabilities to carry out a web workflow;
* preparing or sending communication when supported and appropriate;
* working with relevant code, files, projects, or documents;
* gathering information needed for a concrete decision;
* preparing applications, forms, comparisons, reports, or other deliverables;
* completing a useful portion of a larger task when full completion requires user input.

Do not merely:

* restate or paraphrase an existing todo;
* tell the user that they should do the todo;
* remind the user about work they already know about;
* schedule time for the user to perform work Act could instead perform;
* create a duplicate or near-duplicate todo or subgoal;
* convert an existing todo into generic productivity advice.

Todos are evidence of what the user wants accomplished. The proposal should focus on the work Act can perform toward that outcome.

Creating a todo, subgoal, calendar event, or other planning artifact can still be useful when that artifact is itself the appropriate outcome, but it should not be used as a substitute for doing work Act is capable of doing.

## 2. Find ways to advance the user's goals

Review the user's active goals and enough related context to understand:

* what success means;
* the user's current position and ongoing efforts;
* relevant constraints, deadlines, interests, and decisions;
* work already represented by existing todos or proposals.

Review relevant recent email for signals such as opportunities, deadlines, invitations, programs, recruiting, research activity, events, people, or organizations that may matter to those goals.

For goals where external information could plausibly reveal useful opportunities, actively use live internet search. Do not limit the assessment to what the user has already recorded.

Look for specific, current ways to advance the user's goals, including when relevant:

* research positions, professors, labs, collaborators, or mentors;
* internships, jobs, or recruiting opportunities;
* scholarships, fellowships, grants, or other funding;
* academic programs and admissions opportunities;
* competitions, hackathons, challenges, or awards;
* conferences, workshops, talks, communities, or networking opportunities;
* open-source projects or technical initiatives;
* unusually valuable programs, courses, resources, or experiences;
* emerging developments, risks, or changes that materially affect an active goal.

Do not merely search for generic advice about how to pursue a goal.

For a promising opportunity, investigate far enough to establish:

1. what the opportunity actually is;
2. whether it is current and credible;
3. why it is relevant to this user's situation and goals;
4. important requirements, constraints, or deadlines;
5. what concrete work Act could perform next to pursue it.

Prefer primary or authoritative sources where practical.

The strongest proposals should normally identify a specific opportunity or development and then propose useful work Act can perform around it: investigating fit, gathering requirements, preparing application materials, drafting outreach, using a website, assembling supporting information, comparing alternatives, or otherwise advancing it toward a concrete outcome.

Avoid weak or speculative matches simply because they are superficially related to a goal.

## Integrated assessment

The two objectives above are complementary views of the same situation. Reuse context and research between them when useful.

For example:

* a goal may reveal the real purpose of a vague todo;
* a todo may show that the user is already pursuing an opportunity and prevent a redundant proposal;
* an email may both clarify existing work and reveal a new opportunity;
* research performed while investigating a todo may expose a higher-value way to advance a related goal;
* a newly discovered opportunity may make an existing todo more urgent, obsolete, or worth changing.

Do not duplicate work simply to treat the objectives separately.

However, finding strong proposals from one objective does **not** remove the need to consider the other. Before ending an assessment, ensure you have asked both:

* **Existing work:** Is there worthwhile work Act could take off the user's plate or materially advance now?
* **Goals:** Is there a useful opportunity, development, or action that could advance an important goal and is not already adequately represented by the user's existing work?

## Proposal standard

Use your own judgment. Do not force a proposal merely because something could theoretically be done.

A proposal should only be made when you have enough evidence to be reasonably confident that:

1. you understand the user's situation and intended outcome correctly;
2. the proposed work is genuinely useful now;
3. Act can materially perform the proposed work with its available capabilities;
4. the expected benefit justifies interrupting the user.

Prefer high-value, timely, specific interventions over generic suggestions.

A useful test is:

**If the user approves this proposal, what concrete work will Act perform that otherwise would still remain for the user?**

If the answer is essentially "tell the user what they should do," "remind them," "schedule it," or "create another planning item," the proposal is usually too weak.

Do not treat inferred intentions as established facts. State material assumptions in the proposal when they affect what Act will do.

When information is missing, ask a concise clarification only when the answer is critical enough that a meaningful Act plan cannot be formed without it. Otherwise, defer the proposal or structure the Act instruction so that Act can gather the remaining information itself.

Treat todos and goals as important indicators of the user's priorities, not as the only possible sources of useful work. Recent conversations, decisions, interests, ongoing activities, external developments, and unresolved threads may also justify proposals when sufficiently grounded in the user's expressed priorities and circumstances.

Broader personal recommendations should be relatively rare and should only be proposed when there is strong evidence that they materially benefit the user.

## Act capability awareness

Before proposing work, ensure the proposed action is within Act's current capabilities.

The catalog below describes capabilities available to Act; it does not grant those capabilities to you.

When the catalog shows that Act has a relevant execution capability, including browser control, treat that capability as a real way for Act to perform the task. Do not default to telling the user how to carry out a workflow that Act itself could execute after approval.

[ACT_CAPABILITY_CATALOG]

## Proposal history and duplication

Before proposing anything, read the Assistant proposal history in `knowledge\assistant`.

Do not repeat an existing or materially similar proposal unless circumstances have materially changed.

If replacing an earlier proposal, include `replaces_proposal_id` and clearly state the `material_change` that makes the replacement warranted.

Do not create superficial variations of an existing proposal merely to generate more proposals.

## Submitting plans

Use the private plan approval request only when you have a concrete and useful plan for Act to execute.

Every proposal must include the exact `instruction` Act should receive after approval.

Write this instruction for execution, not for further planning. Give Act the relevant context, desired outcome, important constraints, and any verification needed to carry the work through to a meaningful end state. Where appropriate, instruct Act to gather additional context itself and proceed without unnecessarily returning work to the user.

The user-facing `actions` field should concisely describe what Act will actually do.

Make consequential actions explicit in both `actions` and `instruction`. Distinguish preparing a draft from sending or submitting it. Proposal approval does not bypass Act's runtime approval requirements.

The `rationale` should explain why the work is worthwhile now and what evidence or context supports the proposal.

For proposals based on external information, include supporting source URLs, relevant deadlines with their time zones when available, and material requirements in the rationale or Act instruction. Distinguish verified facts from unresolved questions.

Attach source items in `references` as:

* `todo:<Notion page ID>`
* `goal:<Atlas goal ID>`

Create at most 5 new proposals in this entire thread. Replacements using `replaces_proposal_id` and `material_change` do not count toward this limit and are unlimited.

The limit is a maximum, not a target. Prefer a small number of substantial proposals over many weak ones.

If, after considering both assessment objectives, you do not find a sufficiently useful and well-grounded opportunity for Act, finish quietly without submitting a proposal.

The backend stores proposals and outcomes. Do not write proposal history yourself.
"""

AGENT_INSTRUCTION_TEMPLATES = {
    "act": ACT_INSTRUCTIONS,
    "observer": OBSERVER_INSTRUCTIONS,
    "assistant": ASSISTANT_INSTRUCTIONS_TEMPLATE,
}

ACT_ALLOWED_INHERITED_MCP_SERVERS = frozenset({"playwright"})


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
    # No shared config writes. Disable inherited MCP servers/plugins before
    # starting the managed process, except for Act's explicit browser allowlist.
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = home / "config.toml"
    inherited = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    filesystem = {":minimal": "read", str(root): "read"}
    if agent_id == "act":
        filesystem[str(root / "workspace")] = "write"
        filesystem[str(root / "memory")] = "write"
    mcp_servers = {
        name: {
            **entry,
            "enabled": agent_id == "act" and name in ACT_ALLOWED_INHERITED_MCP_SERVERS,
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
                if self.agent_id == "act" and name in ACT_ALLOWED_INHERITED_MCP_SERVERS:
                    continue
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
                if (
                    self.agent_id == "act"
                    and entry["name"] in ACT_ALLOWED_INHERITED_MCP_SERVERS
                ):
                    if server_info is not None and tools:
                        act_browser_ready = True
                    continue
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
        if self.agent_id == "act" and not act_browser_ready:
            raise ActAppServerError(
                "The Act browser automation MCP server did not start safely"
            )

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
        if self.agent_id == "act":
            inherited = managed_config(self.agent_id, workspace.root)["mcp_servers"]
            for name in ACT_ALLOWED_INHERITED_MCP_SERVERS:
                browser = inherited.get(name)
                if browser is None or browser.get("enabled") is not True:
                    raise ActAppServerError(
                        "Act browser automation is not configured on this Codex host"
                    )
                config[f"mcp_servers.{name}"] = browser
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
