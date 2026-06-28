from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RiskLevel = Literal["low", "medium", "high"]
SkillType = Literal["instruction", "automation", "hybrid"]


class ManifestPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network: list[str]
    filesystem_read: list[str]
    filesystem_write: list[str]
    secrets: list[str]
    shell: bool

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


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1)
    skill_type: SkillType = "automation"
    entrypoint: str | None = Field(default=None, min_length=1)
    instructions_path: str | None = Field(default=None, min_length=1)
    risk_level: RiskLevel
    permissions: ManifestPermissions
    schedule: None = None
    created_by: str = Field(min_length=1)
    enabled: bool

    @field_validator("entrypoint", "instructions_path")
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

    @model_validator(mode="after")
    def validate_skill_contract(self) -> "SkillManifest":
        if self.skill_type == "instruction":
            if not self.instructions_path:
                raise ValueError("instruction skills require instructions_path")
            if self.entrypoint is not None:
                raise ValueError("instruction skills cannot declare entrypoint")
            if not has_no_permissions(self.permissions):
                raise ValueError("instruction skills must request no permissions")
            return self

        if self.skill_type == "automation":
            if not self.entrypoint:
                raise ValueError("automation skills require entrypoint")
            if not self.entrypoint.endswith(".py"):
                raise ValueError("entrypoint must point to a Python file")

        if self.skill_type == "hybrid":
            if not self.instructions_path:
                raise ValueError("hybrid skills require instructions_path")
            if not self.entrypoint:
                raise ValueError("hybrid skills require entrypoint")
            if not self.entrypoint.endswith(".py"):
                raise ValueError("entrypoint must point to a Python file")

        required_risk = classify_permission_risk(self.permissions)
        if risk_rank(self.risk_level) < risk_rank(required_risk):
            raise ValueError(f"risk_level must be at least {required_risk} for requested permissions")
        return self


def risk_rank(risk_level: RiskLevel) -> int:
    return {"low": 0, "medium": 1, "high": 2}[risk_level]


def has_no_permissions(permissions: ManifestPermissions) -> bool:
    return (
        permissions.network == []
        and permissions.filesystem_read == []
        and permissions.filesystem_write == []
        and permissions.secrets == []
        and permissions.shell is False
    )


def classify_permission_risk(permissions: ManifestPermissions) -> RiskLevel:
    if permissions.shell or permissions.secrets:
        return "high"
    if permissions.filesystem_read:
        return "medium"
    unsafe_writes = {
        path.replace("\\", "/").removeprefix("./").rstrip("/")
        for path in permissions.filesystem_write
    } - {"cache"}
    if unsafe_writes:
        return "medium"
    return "low"
