from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InterfaceArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    created_paths: list[str]
    updated_paths: list[str]
    interfaces: dict[str, Any]
    contracts_for_children: list[str]
    known_limitations: list[str]

    @field_validator("created_paths", "updated_paths")
    @classmethod
    def validate_relative_paths(cls, paths: list[str]) -> list[str]:
        normalized_paths: list[str] = []
        for value in paths:
            normalized = value.replace("\\", "/").strip()
            if not normalized or normalized.startswith(("/", "~")) or ":" in normalized:
                raise ValueError("artifact paths must be non-empty relative paths")
            if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
                raise ValueError("artifact paths cannot traverse parent directories")
            if normalized == "interface_artifact.json":
                raise ValueError("interface_artifact.json cannot declare itself as a skill output")
            normalized_paths.append(normalized.removeprefix("./"))
        if len(normalized_paths) != len(set(normalized_paths)):
            raise ValueError("artifact paths cannot contain duplicates")
        return normalized_paths

    @field_validator("contracts_for_children", "known_limitations")
    @classmethod
    def validate_string_lists(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("artifact text lists cannot contain empty values")
        return values

    @model_validator(mode="after")
    def validate_path_groups(self) -> "InterfaceArtifact":
        overlap = set(self.created_paths) & set(self.updated_paths)
        if overlap:
            raise ValueError(f"created_paths and updated_paths overlap: {sorted(overlap)}")
        return self
