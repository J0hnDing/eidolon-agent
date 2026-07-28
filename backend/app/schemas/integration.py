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


class IntegrationInvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)


class IntegrationInvocationResponse(BaseModel):
    output: dict[str, Any]


class IntegrationOperationSummary(BaseModel):
    operation: str
    title: str
    description: str
