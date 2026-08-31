from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from app.services.act_workspace_service import AGENT_INSTRUCTIONS, ActWorkspace, ensure_act_workspace
from app.services.codex_app_server import CodexAppServerClient
from app.services.codex_mcp_settings_service import CodexMcpSettingsService
from app.services.product_manager_session_service import ProductManagerSessionService


class ActAppServerError(RuntimeError):
    pass


ACT_DEVELOPER_INSTRUCTIONS = f"""You are Eidolon Act, a persistent local action agent.

Your writable current working directory is the user's shared Act workspace. Do not
attempt to write outside it. The parent memory directory is reserved and must remain
empty. Use Eidolon MCP tools as your primary capabilities. Existing backend-owned
integration approvals remain authoritative. Use live web search only when it helps,
and use act.document.download for remote files.

Managed workspace policy:
{AGENT_INSTRUCTIONS}
"""


class ActAppServerService:
    def __init__(self, client: CodexAppServerClient | None = None) -> None:
        self.client = client or CodexAppServerClient(
            extra_args=(
                "-c",
                'mcp_servers.eidolon.default_tools_approval_mode="approve"',
                "--search",
            )
        )
        self.sessions = ProductManagerSessionService(self.client)
        self._ready_lock = threading.Lock()

    def ensure_ready(self, db: Session) -> ActWorkspace:
        status = CodexMcpSettingsService(db).status()
        if not status.enabled or not status.config_matches:
            raise ActAppServerError(
                "Eidolon Codex MCP tools must be installed and healthy before starting Act."
            )
        workspace = ensure_act_workspace()
        with self._ready_lock:
            try:
                self.client.start()
                self.client.request("config/mcpServer/reload", None, timeout=20)
                inventory = self.client.request(
                    "mcpServerStatus/list",
                    {"detail": "toolsAndAuthOnly", "limit": 100},
                    timeout=20,
                )
            except Exception as exc:
                raise ActAppServerError(f"Act App Server could not load Eidolon MCP: {exc}") from None
        servers = inventory.get("data")
        eidolon = next(
            (
                item
                for item in servers
                if isinstance(item, dict) and item.get("name") == "eidolon"
            ),
            None,
        ) if isinstance(servers, list) else None
        if not isinstance(eidolon, dict) or not isinstance(eidolon.get("tools"), dict) or not eidolon["tools"]:
            raise ActAppServerError("Act App Server started without any Eidolon MCP tools.")
        return workspace

    def start_thread(
        self,
        db: Session,
        *,
        model: str | None,
        reasoning_effort: str | None,
    ) -> str:
        workspace = self.ensure_ready(db)
        return self.sessions.start_thread(
            cwd=workspace.workspace,
            model=model,
            reasoning_effort=reasoning_effort,
            developer_instructions=ACT_DEVELOPER_INSTRUCTIONS,
            sandbox="workspace-write",
            approval_policy="never",
        )

    def resume_thread(
        self,
        db: Session,
        thread_id: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
    ) -> None:
        workspace = self.ensure_ready(db)
        if self.sessions.has_thread(thread_id):
            return
        self.sessions.resume_thread(
            thread_id,
            cwd=workspace.workspace,
            model=model,
            reasoning_effort=reasoning_effort,
            developer_instructions=ACT_DEVELOPER_INSTRUCTIONS,
            sandbox="workspace-write",
            approval_policy="never",
        )

    def stop(self) -> None:
        self.client.stop()


def is_missing_rollout_error(exc: BaseException) -> bool:
    return "no rollout found for thread id" in " ".join(str(exc).lower().split())


act_app_server_service = ActAppServerService()
