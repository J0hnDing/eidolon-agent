from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MemoryFactBase(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1)
    # Compatibility metadata only; there are no preference/convention folders.
    category: str = Field(default="facts", min_length=1, max_length=64)
    source_message_id: int | None = None
    sensitivity: str = Field(default="normal", min_length=1, max_length=32)
    expires_at: datetime | None = None
    user_editable: bool = True


class MemoryFactCreate(MemoryFactBase):
    pass


class MemoryFactUpdate(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=128)
    value: str | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, min_length=1, max_length=64)
    source_message_id: int | None = None
    sensitivity: str | None = Field(default=None, min_length=1, max_length=32)
    expires_at: datetime | None = None
    user_editable: bool | None = None


class MemoryFactRead(MemoryFactBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    provenance_json: dict | None = None
