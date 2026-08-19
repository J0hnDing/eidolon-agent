from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AtlasDirectoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: str = Field(min_length=1, max_length=2048)


class AtlasPassphraseWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: SecretStr


class AtlasSettingsStatus(BaseModel):
    provider: Literal["atlas"] = "atlas"
    directory: str
    running: bool
    process_ownership: Literal["owned", "external", "none"]
    initialized: bool | None = None
    locked: bool | None = None
    passphrase_configured: bool
    startup_error: str | None = None
    error_type: str | None = None
