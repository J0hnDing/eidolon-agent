import re
from typing import Any, Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RiskLevel = Literal["low", "medium", "high"]
SkillRuntime = Literal["function", "web_app", "service"]
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

    call_response: bool = False
    internet_access: bool = False


class ManifestPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Canonical manifests contain approval-gated requests only. Defaults keep
    # older explicit manifests readable and provide the effective runner view.
    network: list[str] = Field(default_factory=list)
    filesystem_read: list[str] = Field(default_factory=lambda: ["./cache"])
    filesystem_write: list[str] = Field(default_factory=lambda: ["./cache"])
    secrets: list[str] = Field(default_factory=list)
    shell: bool = False
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


def manifest_permission_requests(permissions: ManifestPermissions) -> dict[str, Any]:
    """Return only non-default permission requests declared by a manifest."""

    requested: dict[str, Any] = {}
    if permissions.network:
        requested["network"] = list(permissions.network)
    non_default_reads = [
        path
        for path in permissions.filesystem_read
        if path.replace("\\", "/").removeprefix("./").rstrip("/") != "cache"
    ]
    non_default_writes = [
        path
        for path in permissions.filesystem_write
        if path.replace("\\", "/").removeprefix("./").rstrip("/") != "cache"
    ]
    if non_default_reads:
        requested["filesystem_read"] = non_default_reads
    if non_default_writes:
        requested["filesystem_write"] = non_default_writes
    if permissions.secrets:
        requested["secrets"] = list(permissions.secrets)
    if permissions.shell:
        requested["shell"] = True
    codex = {
        key: True
        for key in ("call_response", "internet_access")
        if getattr(permissions.codex, key)
    }
    if codex:
        requested["codex"] = codex
    return requested


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


class ManifestIntegrationResourceScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repositories: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, repositories: list[str]) -> list[str]:
        normalized: list[str] = []
        for repository in repositories:
            value = repository.strip().lower()
            parts = value.split("/")
            if (
                len(parts) != 2
                or not all(parts)
                or any("*" in part for part in parts)
                or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38})", parts[0]) is None
                or re.fullmatch(r"[a-z0-9_.-]{1,100}", parts[1]) is None
            ):
                raise ValueError("repository scope must use exact owner/repository entries")
            normalized.append(value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("integration repository scope cannot contain duplicates")
        return normalized


class ManifestIntegrationRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["github", "atlas", "notion"]
    operations: list[str] = Field(min_length=1, max_length=20)
    resource_scope: ManifestIntegrationResourceScope = Field(default_factory=ManifestIntegrationResourceScope)

    @model_validator(mode="after")
    def validate_requirement(self) -> "ManifestIntegrationRequirement":
        from app.services.integration_registry import OPERATIONS

        if len(self.operations) != len(set(self.operations)):
            raise ValueError("integration requirement operations cannot contain duplicates")
        unknown = [operation_id for operation_id in self.operations if operation_id not in OPERATIONS]
        if unknown:
            raise ValueError(f"unknown integration operations: {unknown}")
        if any(OPERATIONS[operation_id].provider != self.provider for operation_id in self.operations):
            raise ValueError("integration operations must match their declared provider")
        repository_operations = [
            operation_id
            for operation_id in self.operations
            if OPERATIONS[operation_id].resource_scope == "repository"
        ]
        if repository_operations and not self.resource_scope.repositories:
            raise ValueError("repository-scoped integration operations require exact repository scope")
        if not repository_operations and self.resource_scope.repositories:
            raise ValueError("non-repository integration operations cannot declare repository scope")
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
    function_requirements: list[str] = Field(default_factory=list)
    integration_requirements: list[ManifestIntegrationRequirement] = Field(default_factory=list)
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

    @field_validator("input_schema", "output_schema")
    @classmethod
    def validate_json_schema(cls, schema: dict[str, Any] | None) -> dict[str, Any] | None:
        if schema is None:
            return None
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
        return schema

    @field_validator("function_requirements")
    @classmethod
    def validate_function_requirements(cls, requirements: list[str]) -> list[str]:
        for requirement in requirements:
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", requirement) is None:
                raise ValueError("function requirements must contain installed function names")
        return requirements

    @model_validator(mode="after")
    def validate_skill_contract(self) -> "SkillManifest":
        if len(self.function_requirements) != len(set(self.function_requirements)):
            raise ValueError("function_requirements cannot contain duplicate function names")
        if self.name in self.function_requirements:
            raise ValueError("a skill cannot require itself as a function")
        providers = [requirement.provider for requirement in self.integration_requirements]
        if len(providers) != len(set(providers)):
            raise ValueError("integration_requirements cannot contain duplicate providers")
        if self.runtime in {"function", "service"}:
            normalized = self.entrypoint.replace("\\", "/")
            if normalized.startswith("/") or normalized.startswith("~") or ":" in normalized:
                raise ValueError(f"{self.runtime} entrypoint must be a relative Python file")
            if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
                raise ValueError(f"{self.runtime} entrypoint cannot traverse parent directories")
            if not normalized.endswith(".py"):
                raise ValueError(f"{self.runtime} entrypoint must point to a Python file")
            for schema_name, schema in (
                ("input_schema", self.input_schema),
                ("output_schema", self.output_schema),
            ):
                if self.runtime == "service" and schema is None:
                    raise ValueError(f"service {schema_name} is required")
                if schema is not None and schema.get("type") != "object":
                    raise ValueError(f"{self.runtime} {schema_name} must declare type object")
            if self.runtime == "function" and self.schedule is not None:
                raise ValueError("function skills cannot declare schedules")
            if self.runtime == "service" and self.schedule is None:
                raise ValueError("service skills require a schedule")
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
                raise ValueError("web_app skills cannot declare schedules")
        return self


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
