from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.common import WebAppInstanceStatus, WebAppSessionStatus


class WebAppContainmentPolicy(BaseModel):
    iframe_sandbox: str
    content_security_policy: str
    permissions_policy: str
    browser_network: str
    origin_isolation: str
    websocket_support: str
    runner_isolation: str
    server_network_enforcement: str


class WebAppInstanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    skill_id: int
    version_id: int
    status: WebAppInstanceStatus
    runner_mode: str
    container_id: str | None
    relay_container_id: str | None
    process_id: int | None
    error_message: str | None
    logs: str | None
    created_at: datetime
    started_at: datetime | None
    ready_at: datetime | None
    last_accessed_at: datetime | None
    stopped_at: datetime | None
    updated_at: datetime


class WebAppSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    instance_id: str
    skill_id: int
    status: WebAppSessionStatus
    gateway_host: str
    created_at: datetime
    last_accessed_at: datetime
    expires_at: datetime
    closed_at: datetime | None


class WebAppOpenResponse(BaseModel):
    instance: WebAppInstanceRead
    session: WebAppSessionRead
    embed_url: str
    containment: WebAppContainmentPolicy


class WebAppAuditRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    instance_id: str
    session_id: str | None
    operation: str
    status: str
    request_json: dict
    response_json: dict
    error_message: str | None
    started_at: datetime
    ended_at: datetime | None
