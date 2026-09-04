from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class GitHubCredentialWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Length is checked in trusted service code so FastAPI validation errors
    # cannot reflect the submitted credential as an invalid input value.
    token: SecretStr


class GitHubConnectionStatus(BaseModel):
    provider: Literal["github"] = "github"
    connected: bool
    status: Literal["connected", "disconnected", "unavailable", "invalid"]
    account_login: str | None = None
    account_id: str | None = None
    last_validated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_type: str | None = None


class NotionCredentialWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr


class NotionDataSourcesWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_source_id: str = Field(min_length=1, max_length=256)
    report_data_source_id: str = Field(min_length=1, max_length=256)


class NotionConnectionStatus(BaseModel):
    provider: Literal["notion"] = "notion"
    connected: bool
    status: Literal["connected", "disconnected", "unavailable", "invalid"]
    bot_name: str | None = None
    bot_id: str | None = None
    workspace_name: str | None = None
    data_source_id: str | None = None
    report_data_source_id: str | None = None
    last_validated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_type: str | None = None


class QuercusCredentialWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr


class QuercusCourseSelectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_ids: list[str] = Field(max_length=500)


class QuercusProcessingWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["none", "marker_surya_llamacpp"]
    llama_cpp_directory: str | None = Field(default=None, max_length=2048)


class QuercusProcessingStatus(BaseModel):
    method: Literal["none", "marker_surya_llamacpp"]
    llama_cpp_directory: str | None = None
    llama_cpp_available: bool
    inference_url: str | None = None
    marker_available: bool
    inference_available: bool
    status: Literal["disabled", "idle", "pending", "running", "succeeded", "partial", "failed"]
    processed_file_count: int = 0
    failed_file_count: int = 0


class QuercusProcessingReprocessResult(QuercusProcessingStatus):
    queued_file_count: int = 0


class QuercusCourseRead(BaseModel):
    course_id: str
    name: str
    course_code: str | None = None
    term_name: str | None = None
    enrollment_state: str | None = None
    selected: bool = False
    retained: bool = False
    local_path: str | None = None
    last_sync_started_at: datetime | None = None
    last_sync_completed_at: datetime | None = None
    last_sync_status: str | None = None
    last_error_type: str | None = None
    skipped_file_count: int = 0
    last_processing_started_at: datetime | None = None
    last_processing_completed_at: datetime | None = None
    last_processing_status: str | None = None
    last_processing_error_type: str | None = None
    processed_file_count: int = 0
    failed_processing_count: int = 0


class QuercusConnectionStatus(BaseModel):
    provider: Literal["quercus"] = "quercus"
    connected: bool
    status: Literal["connected", "disconnected", "unavailable", "invalid"]
    account_name: str | None = None
    account_id: str | None = None
    last_validated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_type: str | None = None
    courses: list[QuercusCourseRead] = Field(default_factory=list)


class GoogleOAuthClientWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Trusted service code checks bounds so validation errors never reflect
    # either submitted OAuth client credential.
    client_id: SecretStr
    client_secret: SecretStr


class GoogleOAuthClientStatus(BaseModel):
    provider: Literal["google"] = "google"
    configured: bool
    status: Literal["configured", "not_configured", "unavailable", "conflict"]
    calendar_redirect_uri: str
    gmail_redirect_uri: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_type: str | None = None


class GoogleOAuthStartResponse(BaseModel):
    authorization_url: str


class GoogleCalendarConnectionStatus(BaseModel):
    provider: Literal["google_calendar"] = "google_calendar"
    connected: bool
    status: Literal["connected", "disconnected", "unavailable", "invalid"]
    account_email: str | None = None
    last_validated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_type: str | None = None
    oauth_redirect_uri: str


class GmailConnectionStatus(BaseModel):
    provider: Literal["gmail"] = "gmail"
    connected: bool
    status: Literal["connected", "disconnected", "unavailable", "invalid"]
    account_email: str | None = None
    last_validated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_type: str | None = None
    oauth_redirect_uri: str


class TelegramPairingStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr


class TelegramConnectionStatus(BaseModel):
    provider: Literal["telegram"] = "telegram"
    connected: bool
    status: Literal[
        "connected",
        "disconnected",
        "pairing",
        "unavailable",
        "invalid",
        "webhook_conflict",
    ]
    bot_username: str | None = None
    paired_chat_id: str | None = None
    paired_user_id: str | None = None
    pairing_expires_at: datetime | None = None
    last_validated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error_type: str | None = None


class TelegramPairingResponse(BaseModel):
    connection: TelegramConnectionStatus
    pairing_code: str
    expires_at: datetime


class IntegrationInvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)


class IntegrationInvocationResponse(BaseModel):
    output: dict[str, Any]
