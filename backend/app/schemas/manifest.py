from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RiskLevel = Literal["low", "medium", "high"]
SkillRuntime = Literal["function", "web_app"]
ScheduleType = Literal["daily", "weekly", "interval"]
IntervalUnit = Literal["minutes", "hours", "days"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
KNOWN_TIMEZONES = {
    "UTC",
    "America/Toronto",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
}


class ManifestCodexPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_response: bool = True
    internet_access: bool = False


class ManifestPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network: list[str]
    filesystem_read: list[str]
    filesystem_write: list[str]
    secrets: list[str]
    shell: bool
    codex: ManifestCodexPermissions = Field(default_factory=ManifestCodexPermissions)

    @field_validator("network")
    @classmethod
    def validate_network(cls, domains: list[str]) -> list[str]:
        for domain in domains:
            if not domain or domain == "*" or "*" in domain:
                raise ValueError("network permissions must use explicit domains")
            if "://" in domain or "/" in domain:
                raise ValueError("network permissions must be domains, not URLs")
        return domains

    @field_validator("filesystem_read", "filesystem_write")
    @classmethod
    def validate_relative_paths(cls, paths: list[str]) -> list[str]:
        for path in paths:
            normalized = path.replace("\\", "/")
            if not normalized:
                raise ValueError("filesystem permissions cannot contain empty paths")
            if normalized.startswith("/") or normalized.startswith("~") or ":" in normalized:
                raise ValueError("filesystem permissions must be relative paths")
            if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
                raise ValueError("filesystem permissions cannot traverse parent directories")
        return paths


class ManifestSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ScheduleType
    timezone: str = "America/Toronto"
    input: dict[str, Any] = Field(default_factory=dict)
    time: str | None = None
    day: Weekday | None = None
    every: int | None = None
    unit: IntervalUnit | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, timezone: str) -> str:
        if timezone not in KNOWN_TIMEZONES:
            raise ValueError("schedule timezone must be a supported IANA timezone")
        return timezone

    @field_validator("time")
    @classmethod
    def validate_time(cls, time: str | None) -> str | None:
        if time is None:
            return None
        parts = time.split(":")
        if len(parts) != 2:
            raise ValueError("schedule time must use HH:MM")
        hour, minute = (int(part) for part in parts)
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("schedule time must use HH:MM")
        return time

    @model_validator(mode="after")
    def validate_schedule_contract(self) -> "ManifestSchedule":
        if self.type == "daily":
            if not self.time:
                raise ValueError("daily schedules require time")
            if self.day is not None or self.every is not None or self.unit is not None:
                raise ValueError("daily schedules only support time, timezone, and input")
        if self.type == "weekly":
            if not self.time or not self.day:
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


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str = Field(min_length=1)
    runtime: SkillRuntime = "function"
    entrypoint: str = Field(min_length=1)
    instructions_path: str | None = Field(default=None, min_length=1)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    dependencies: list[str] = Field(default_factory=list)
    permissions: ManifestPermissions
    schedule: ManifestSchedule | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_backend_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        # Compatibility only: these legacy package fields are backend-owned state
        # and are deliberately absent from the canonical serialized manifest.
        normalized.pop("risk_level", None)
        normalized.pop("created_by", None)
        normalized.pop("enabled", None)
        if normalized.get("entrypoint") is None:
            raise ValueError("skills require entrypoint")
        return normalized

    @field_validator("instructions_path")
    @classmethod
    def validate_relative_file_path(cls, path: str | None) -> str | None:
        if path is None:
            return None
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("~") or ":" in normalized:
            raise ValueError("skill file paths must be relative")
        if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
            raise ValueError("skill file paths cannot traverse parent directories")
        return path

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies(cls, dependencies: list[str]) -> list[str]:
        for dependency in dependencies:
            normalized = dependency.strip()
            if not normalized:
                raise ValueError("dependencies cannot contain empty values")
            lowered = normalized.lower()
            if (
                "://" in normalized
                or "/" in normalized
                or "\\" in normalized
                or lowered.startswith("git+")
                or normalized.startswith("-")
                or "@" in normalized
                or ";" in normalized
            ):
                raise ValueError("dependencies must be package names or simple version specifiers")
        return dependencies

    @model_validator(mode="after")
    def validate_skill_contract(self) -> "SkillManifest":
        if self.runtime == "function":
            normalized = self.entrypoint.replace("\\", "/")
            if normalized.startswith("/") or normalized.startswith("~") or ":" in normalized:
                raise ValueError("function entrypoint must be a relative Python file")
            if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
                raise ValueError("function entrypoint cannot traverse parent directories")
            if not normalized.endswith(".py"):
                raise ValueError("function entrypoint must point to a Python file")
        else:
            module, separator, attribute = self.entrypoint.partition(":")
            module_parts = module.split(".")
            if (
                separator != ":"
                or not attribute.isidentifier()
                or not module_parts
                or any(not part.isidentifier() for part in module_parts)
            ):
                raise ValueError("web_app entrypoint must use importable module:attribute syntax")
            if self.schedule is not None:
                raise ValueError("web_app skills cannot declare bounded-run schedules")
        return self


def risk_rank(risk_level: RiskLevel) -> int:
    return {"low": 0, "medium": 1, "high": 2}[risk_level]


def classify_permission_risk(
    permissions: ManifestPermissions,
    dependencies: list[str] | None = None,
) -> RiskLevel:
    if permissions.shell or permissions.secrets:
        return "high"
    read_paths = {
        path.replace("\\", "/").removeprefix("./").rstrip("/")
        for path in permissions.filesystem_read
    }
    if read_paths - {"cache"}:
        return "medium"
    unsafe_writes = {
        path.replace("\\", "/").removeprefix("./").rstrip("/")
        for path in permissions.filesystem_write
    } - {"cache"}
    if unsafe_writes:
        return "medium"
    if permissions.network or permissions.codex.internet_access or dependencies:
        return "medium"
    return "low"
