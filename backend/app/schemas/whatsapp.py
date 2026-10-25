from typing import Literal

from pydantic import BaseModel


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
