import pytest

from app.services.manifest_validator import ManifestValidationError, validate_manifest


def valid_manifest() -> dict:
    return {
        "name": "ai_news_digest",
        "description": "Summarizes AI infrastructure news from approved public sources.",
        "entrypoint": "skill.py",
        "risk_level": "low",
        "permissions": {
            "network": ["reuters.com", "apnews.com"],
            "filesystem_read": [],
            "filesystem_write": ["./cache"],
            "secrets": [],
            "shell": False,
        },
        "schedule": None,
        "created_by": "codex",
        "enabled": False,
    }


def test_valid_low_risk_manifest_passes() -> None:
    manifest = validate_manifest(valid_manifest())

    assert manifest.name == "ai_news_digest"
    assert manifest.permissions.network == ["reuters.com", "apnews.com"]


def test_manifest_requires_explicit_permission_fields() -> None:
    data = valid_manifest()
    del data["permissions"]["shell"]

    with pytest.raises(ManifestValidationError, match="shell"):
        validate_manifest(data)


def test_manifest_rejects_wildcard_network_access() -> None:
    data = valid_manifest()
    data["permissions"]["network"] = ["*"]

    with pytest.raises(ManifestValidationError, match="explicit domains"):
        validate_manifest(data)


def test_manifest_rejects_url_network_permissions() -> None:
    data = valid_manifest()
    data["permissions"]["network"] = ["https://example.com/feed"]

    with pytest.raises(ManifestValidationError, match="domains"):
        validate_manifest(data)


def test_manifest_rejects_parent_directory_entrypoint() -> None:
    data = valid_manifest()
    data["entrypoint"] = "../skill.py"

    with pytest.raises(ManifestValidationError, match="traverse"):
        validate_manifest(data)


def test_manifest_rejects_parent_directory_filesystem_permission() -> None:
    data = valid_manifest()
    data["permissions"]["filesystem_write"] = ["../outside"]

    with pytest.raises(ManifestValidationError, match="traverse"):
        validate_manifest(data)


def test_manifest_requires_risk_level_to_match_permissions() -> None:
    data = valid_manifest()
    data["permissions"]["filesystem_read"] = ["selected_notes"]

    with pytest.raises(ManifestValidationError, match="at least medium"):
        validate_manifest(data)


def test_manifest_accepts_declared_medium_risk_for_filesystem_read() -> None:
    data = valid_manifest()
    data["risk_level"] = "medium"
    data["permissions"]["filesystem_read"] = ["selected_notes"]

    manifest = validate_manifest(data)

    assert manifest.risk_level == "medium"
