from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import ActSession, AgentCredential, AgentPolicy
from app.schemas.agents import AgentPolicyUpdate
from app.services.function_catalog_service import FunctionCatalogService

AGENTS = {
    "act": ("Act", "A persistent action agent that executes your requests using approved capabilities."),
    "observer": ("Observer", "A conversational observer with read-only access by default."),
    "assistant": ("Assistant", "Assesses goals and todos, researches useful actions, and proposes plans for approval."),
}
PLAN_TOOL_ID = "plan_approval_request"
RISK = {"low": 0, "medium": 1, "high": 2}


class AgentPermissionError(ValueError):
    error_type = "agent_permission_denied"


class AgentPolicyService:
    def __init__(self, db: Session):
        self.db = db

    def policy(self, agent_id: str) -> AgentPolicyUpdate:
        self.require_agent(agent_id)
        row = self.db.get(AgentPolicy, agent_id)
        return (
            AgentPolicyUpdate.model_validate(row.policy_json) if row else AgentPolicyUpdate(read_only=agent_id != "act")
        )

    @staticmethod
    def require_agent(agent_id: str) -> None:
        if agent_id not in AGENTS:
            raise AgentPermissionError("Unknown agent")

    def update(self, agent_id: str, policy: AgentPolicyUpdate) -> dict:
        self.require_agent(agent_id)
        known = {entry["id"] for entry in FunctionCatalogService(self.db).list_entries()}
        known.add(PLAN_TOOL_ID)
        for function_id in policy.allowed_functions + policy.banned_functions:
            if function_id not in known:
                raise AgentPermissionError(f"Unknown function: {function_id}")
        if agent_id != "assistant" and PLAN_TOOL_ID in policy.allowed_functions:
            raise AgentPermissionError("Plan approval requests are private to Assistant")
        row = self.db.get(AgentPolicy, agent_id)
        if row is None:
            row = AgentPolicy(id=agent_id, policy_json=policy.model_dump())
            self.db.add(row)
        else:
            row.policy_json = policy.model_dump()
            row.revision += 1
        self.db.commit()
        return self.describe(agent_id)

    def decision(self, agent_id: str, entry: dict) -> tuple[bool, str]:
        policy = self.policy(agent_id)
        function_id = entry["id"]
        if function_id in policy.banned_functions:
            return False, "Explicitly banned"
        if function_id == PLAN_TOOL_ID:
            return (agent_id == "assistant", "Assistant-only plan request")
        if entry.get("availability") != "available" or entry.get("mcp_exposed") is not True:
            return False, "Unavailable to agents"
        if function_id in policy.allowed_functions:
            return True, "Explicitly allowed"
        if RISK.get(entry.get("risk_level"), 99) > RISK[policy.max_risk]:
            return False, "Exceeds risk limit"
        if policy.read_only and entry.get("mcp_read_only") is not True:
            return False, "Write-capable function"
        return True, "Allowed by default policy"

    def require_function(self, agent_id: str, function_id: str) -> None:
        entry = next(
            (item for item in FunctionCatalogService(self.db).list_entries() if item["id"] == function_id), None
        )
        if function_id == PLAN_TOOL_ID:
            entry = {"id": PLAN_TOOL_ID}
        if entry is None:
            raise AgentPermissionError("Function unavailable")
        allowed, reason = self.decision(agent_id, entry)
        if not allowed:
            raise AgentPermissionError(reason)

    def describe(self, agent_id: str) -> dict:
        policy = self.policy(agent_id)
        name, description = AGENTS[agent_id]
        functions = []
        for entry in FunctionCatalogService(self.db).list_entries():
            allowed, reason = self.decision(agent_id, entry)
            functions.append({**entry, "allowed": allowed, "reason": reason})
        if agent_id == "assistant":
            functions.append(
                {
                    "id": PLAN_TOOL_ID,
                    "title": "Request plan approval",
                    "description": "Privately propose work for a new Act session.",
                    "risk_level": "low",
                    "mcp_read_only": False,
                    "allowed": PLAN_TOOL_ID not in policy.banned_functions,
                    "reason": "Assistant-only plan request",
                }
            )
        return {
            "id": agent_id,
            "name": name,
            "description": description,
            "policy": policy.model_dump(),
            "permissions": {
                "filesystem": "Read shared root; write workspace and explicit memory"
                if agent_id == "act"
                else "Read shared root",
                "web_search": agent_id != "observer",
            },
            "functions": functions,
        }

    def issue(self, agent_id: str, session_id: int) -> str:
        self.require_agent(agent_id)
        self.require_session(agent_id, session_id)
        self.revoke(session_id)
        token = secrets.token_urlsafe(32)
        self.db.add(
            AgentCredential(
                token_hash=hashlib.sha256(token.encode()).hexdigest(), agent_id=agent_id, session_id=session_id
            )
        )
        self.db.commit()
        return token

    def require_session(self, agent_id: str, session_id: int) -> None:
        session = self.db.get(ActSession, session_id)
        if session is None or session.agent_id != agent_id or session.status != "active":
            raise AgentPermissionError("Agent session is unavailable")

    def authenticate(self, token: str) -> tuple[str, int]:
        self.db.expire_all()
        row = self.db.get(AgentCredential, hashlib.sha256(token.encode()).hexdigest())
        if row is None or row.revoked:
            raise AgentPermissionError("Agent credential is invalid or revoked")
        session = self.db.get(ActSession, row.session_id)
        if session is None or session.agent_id != row.agent_id or session.status != "active":
            raise AgentPermissionError("Agent session is unavailable")
        return row.agent_id, row.session_id

    def revoke(self, session_id: int) -> None:
        self.db.execute(update(AgentCredential).where(AgentCredential.session_id == session_id).values(revoked=True))

    def act_catalog(self) -> list[dict]:
        return [
            {
                key: entry.get(key)
                for key in ("id", "title", "description", "risk_level", "requires_invocation_approval")
            }
            for entry in FunctionCatalogService(self.db).list_entries()
            if self.decision("act", entry)[0]
        ]
