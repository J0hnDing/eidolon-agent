from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import ScheduleStatus, ScheduleType

IntervalUnit = Literal["minutes", "hours", "days"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class SchedulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ScheduleType
    timezone: str = "America/Toronto"
    input: dict[str, Any] = Field(default_factory=dict)
    time: str | None = None
    day: Weekday | None = None
    every: int | None = None
    unit: IntervalUnit | None = None

    @field_validator("time")
    @classmethod
    def validate_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("time must use HH:MM")
        hour, minute = (int(part) for part in parts)
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("time must use HH:MM")
        return value

    @model_validator(mode="after")
    def validate_schedule(self) -> "SchedulePayload":
        if self.type == "daily":
            if not self.time:
                raise ValueError("daily schedules require time")
            if self.day is not None or self.every is not None or self.unit is not None:
                raise ValueError("daily schedules only support time, timezone, and input")
        if self.type == "weekly":
            if not self.day or not self.time:
                raise ValueError("weekly schedules require day and time")
            if self.every is not None or self.unit is not None:
                raise ValueError("weekly schedules only support day, time, timezone, and input")
        if self.type == "interval":
            if self.every is None or self.unit is None:
                raise ValueError("interval schedules require every and unit")
            if self.every < 1:
                raise ValueError("interval every must be at least 1")
            if self.time is not None or self.day is not None:
                raise ValueError("interval schedules only support every, unit, timezone, and input")
        return self


class ScheduleUpdate(BaseModel):
    name: str = Field(default="Service schedule", min_length=1, max_length=128)
    schedule: SchedulePayload


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    schedule_kind: Literal["service", "platform"] = "service"
    service_id: str | None = None
    read_only: bool = False
    skill_id: int | None
    skill_name: str | None = None
    skill_enabled: bool | None = None
    is_running: bool = False
    name: str
    status: ScheduleStatus
    schedule_type: ScheduleType
    schedule_json: dict[str, Any]
    input_json: dict[str, Any]
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    created_at: datetime
    updated_at: datetime
