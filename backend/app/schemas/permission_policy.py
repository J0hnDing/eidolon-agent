from typing import Any

from pydantic import BaseModel, ConfigDict


class WebAppPermissionPolicyRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported: list[str]
    blocked: list[str]


class PermissionPolicyRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    default_allowed: dict[str, Any]
    requires_approval: dict[str, Any]
    blocked: list[str]
    web_app: WebAppPermissionPolicyRead
