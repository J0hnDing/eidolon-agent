from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WhatsAppHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1, max_length=128)


class WhatsAppMusePermissionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    account_id: str | None = Field(default=None, max_length=128)


class WhatsAppStatus(BaseModel):
    provider: Literal["whatsapp"] = "whatsapp"
    connected: bool = False
    status: str = "disconnected"
    account_id: str | None = None
    account_login: str | None = None
    qr_data_url: str | None = None
    sync_status: str = "not_started"
    last_synced_at: str | None = None
    oldest_message_at: str | None = None
    retention_cutoff: str | None = None
    error_type: str | None = None
    remote_logout_failed: bool = False
    muse_enabled: bool = False
    muse_grant_id: int | None = None
