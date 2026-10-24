"""Small durable contracts shared by conversation, agenda tools, and the UI."""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AssistantSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(default=None, strict=True)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Unknown timezone") from None
        return value

    @field_validator("enabled", "timezone")
    @classmethod
    def supplied_values_are_not_null(cls, value):
        if value is None:
            raise ValueError("Setting cannot be null")
        return value


class AgendaRecurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequency: Literal["daily", "weekly", "monthly"]
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=64)
    weekdays: list[int] | None = None
    day_of_month: int | None = Field(default=None, ge=1, le=31)

    _timezone = field_validator("timezone")(AssistantSettingsUpdate.known_timezone.__func__)

    @model_validator(mode="after")
    def valid_calendar(self):
        if self.frequency == "weekly":
            if not self.weekdays or any(isinstance(day, bool) or day not in range(7) for day in self.weekdays):
                raise ValueError("Weekly recurrence requires weekdays from 0 (Monday) to 6 (Sunday)")
            self.weekdays = sorted(set(self.weekdays))
        elif self.weekdays is not None:
            raise ValueError("Weekdays apply only to weekly recurrence")
        if (self.frequency == "monthly") != (self.day_of_month is not None):
            raise ValueError("Monthly recurrence requires day_of_month; other recurrences must omit it")
        return self


class AgendaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["task", "question"] = "task"
    question_category: Literal["common", "important", "temporal"] = "common"
    title: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=12000)
    status: Literal["open", "waiting", "done", "dismissed"] = "open"
    due_at: datetime | None = None
    recurrence: AgendaRecurrence | None = None
    completion_requirement: Literal["interaction_today"] | None = None

    @model_validator(mode="after")
    def recurring_task(self):
        if self.recurrence is not None and self.kind != "task":
            raise ValueError("Only tasks can recur")
        if self.completion_requirement is not None and (self.kind != "task" or self.recurrence is None):
            raise ValueError("Completion requirements apply to recurring tasks")
        if self.recurrence is not None and self.due_at is not None:
            raise ValueError("Recurring task activation time is managed by its recurrence")
        return self

    @field_validator("due_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Activation time requires an explicit timezone")
        return value


class AgendaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1)
    kind: Literal["task", "question"] | None = None
    question_category: Literal["common", "important", "temporal"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=12000)
    status: Literal["open", "waiting", "done", "dismissed"] | None = None
    due_at: datetime | None = None
    recurrence: AgendaRecurrence | None = None
    completion_requirement: Literal["interaction_today"] | None = None

    _timezone_required = field_validator("due_at")(AgendaCreate.timezone_required.__func__)

    @field_validator("kind", "question_category", "title", "notes", "status")
    @classmethod
    def supplied_values_are_not_null(cls, value):
        if value is None:
            raise ValueError("Agenda field cannot be null")
        return value


class AgendaRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
