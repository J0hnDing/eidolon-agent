"""Full-size binary transfers against an isolated native Atlas, plus file boundaries."""

import base64
import hashlib

import pytest
from jsonschema import Draft202012Validator

from app.integrations.atlas_attachments import ATLAS_ATTACHMENT_SPECS
from app.services import act_workspace_service
from app.services import atlas_provider as provider_module
from app.services.act_workspace_service import ensure_act_workspace
from app.services.atlas_attachment_provider import AtlasAttachmentProvider
from app.services.atlas_provider import FakeAtlasProviderAdapter, UrllibAtlasProviderAdapter
from app.services.github_provider import IntegrationProviderError
from app.services.integration_registry import OPERATIONS
from tests.test_atlas_native_read_contract import synthetic_atlas  # noqa: F401


@pytest.fixture
def attachments(tmp_path, monkeypatch, synthetic_atlas):  # noqa: F811
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path / "agents")
    monkeypatch.setattr(provider_module, "ATLAS_BASE_URL", synthetic_atlas["url"])
    provider = UrllibAtlasProviderAdapter()

    def call(name, payload):
        spec = OPERATIONS[name]
        Draft202012Validator(spec.input_schema).validate(payload)
        result = provider.execute(spec, payload)
        Draft202012Validator(spec.output_schema).validate(result)
        return result

    return call


def file_reference(name, raw):
    path = ensure_act_workspace().downloads / name
    path.write_bytes(raw)
    return {"path": f"workspace/downloads/{name}", "filename": name, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def test_native_image_upload_download_delete_preserves_original_bytes(attachments, synthetic_atlas):  # noqa: F811
    # Native accepts the PNG signature plus original bytes. Exceed MCP's input
    # limit to demonstrate that file transfer is independent of JSON limits.
    raw = base64.b64decode(synthetic_atlas["image"]) + b"\0" * 300_000
    reference = file_reference("photo with space.png", raw)
    record_id = synthetic_atlas["ids"]["experience"]
    created = attachments("atlas.attachments.upload", {"kind": "image", "record_id": record_id,
                                                      "file": reference})["attachment"]
    assert created["filename"] == reference["filename"]
    assert created["byteLength"] == len(raw)
    first = attachments("atlas.attachments.download", {"kind": "image", "id": created["id"]})["file"]
    second = attachments("atlas.attachments.download", {"kind": "image", "id": created["id"]})["file"]
    assert first["path"] != second["path"]
    for result in (first, second):
        assert ensure_act_workspace().root.joinpath(result["path"]).read_bytes() == raw
        assert result["sha256"] == reference["sha256"]
    copied = attachments("atlas.attachments.upload", {"kind": "image", "record_id": record_id,
                                                     "file": first})["attachment"]
    assert copied["byteLength"] == len(raw)
    attachments("atlas.attachments.delete", {"kind": "image", "id": copied["id"]})
    assert attachments("atlas.attachments.delete", {"kind": "image", "id": created["id"]}) == {"result": {"deleted": True}}
    with pytest.raises(IntegrationProviderError) as missing:
        attachments("atlas.attachments.download", {"kind": "image", "id": created["id"]})
    assert missing.value.error_type == "not_found"


def test_native_resume_replace_download_and_delete_without_record_revision(attachments, synthetic_atlas):  # noqa: F811
    record_id = synthetic_atlas["ids"]["person"]
    before = attachments("atlas.records.get", {"id": record_id})["record"]
    original = base64.b64decode(synthetic_atlas["resume"])
    for name, raw in (("resume.pdf", original), ("updated.pdf", original + b"\n% changed synthetic resume\n")):
        reference = file_reference(name, raw)
        metadata = attachments("atlas.attachments.upload", {"kind": "resume", "record_id": record_id,
                                                           "file": reference})["attachment"]
        assert metadata["recordId"] == record_id
        assert metadata["filename"] == name
        result = attachments("atlas.attachments.download", {"kind": "resume", "id": record_id})["file"]
        assert ensure_act_workspace().root.joinpath(result["path"]).read_bytes() == raw
    assert attachments("atlas.records.get", {"id": record_id})["record"]["revision"] == before["revision"]
    assert attachments("atlas.attachments.delete", {"kind": "resume", "id": record_id})["result"]["deleted"] is True
    assert attachments("atlas.attachments.list", {"record_id": record_id})["resume"] is None


@pytest.mark.parametrize("relative", ["/etc/passwd", "workspace/downloads/../secret.pdf",
    "workspace/downloads//file.pdf", "workspace\\downloads\\file.pdf", "memory/facts-outside-atlas/secret.pdf",
    "workspace/downloads/file.pdf:stream", "workspace/downloads/./file.pdf"])
def test_upload_rejects_path_escape_before_transport(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path / "agents")

    def no_transport(*args, **kwargs):
        pytest.fail("Invalid file reached native transport")

    with pytest.raises(IntegrationProviderError) as error:
        AtlasAttachmentProvider(no_transport).execute("atlas.attachments.upload", {
            "kind": "resume", "record_id": "person", "file": {"path": relative, "filename": "file.pdf",
            "bytes": 1, "sha256": "a" * 64}})
    assert error.value.error_type == "invalid_input"


def test_upload_requires_exact_file_hash_and_native_valid_content(attachments, synthetic_atlas):  # noqa: F811
    reference = file_reference("corrupt.png", b"invalid image")
    requested = {"kind": "image", "record_id": synthetic_atlas["ids"]["experience"], "file": reference}
    with pytest.raises(IntegrationProviderError) as mismatch:
        attachments("atlas.attachments.upload", {**requested, "file": {**reference, "sha256": "0" * 64}})
    assert mismatch.value.error_type == "stale_revision"
    with pytest.raises(IntegrationProviderError) as invalid:
        attachments("atlas.attachments.upload", requested)
    assert invalid.value.error_type == "invalid_input"


def test_upload_rejects_symlinks_or_junctions(tmp_path, monkeypatch):
    monkeypatch.setattr(act_workspace_service, "ACT_ROOT", tmp_path / "agents")
    workspace = ensure_act_workspace()
    external = tmp_path / "external.pdf"
    external.write_bytes(b"%PDF-1.4\n")
    linked = workspace.downloads / "linked.pdf"
    try:
        linked.symlink_to(external)
    except OSError:
        # Windows without Developer Mode: use a directory junction instead.
        import subprocess
        import sys

        if sys.platform != "win32":
            raise
        workspace.downloads.rmdir()
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        (external_dir / "linked.pdf").write_bytes(external.read_bytes())
        subprocess.run(["cmd", "/c", "mklink", "/J", str(workspace.downloads), str(external_dir)],
                       capture_output=True, check=True)
    reference = {"path": "workspace/downloads/linked.pdf", "filename": "linked.pdf", "bytes": external.stat().st_size,
                 "sha256": hashlib.sha256(external.read_bytes()).hexdigest()}
    try:
        with pytest.raises(IntegrationProviderError) as error:
            AtlasAttachmentProvider(lambda *args, **kwargs: pytest.fail("Link reached transport")).execute(
                "atlas.attachments.upload", {"kind": "resume", "record_id": "person", "file": reference})
        assert error.value.error_type == "invalid_input"
    finally:
        if hasattr(workspace.downloads, "is_junction") and workspace.downloads.is_junction():
            workspace.downloads.rmdir()


def test_every_new_contract_has_valid_example_and_fake_output():
    from app.integrations.atlas_lifecycle import ATLAS_LIFECYCLE_SPECS
    from app.integrations.atlas_state_write import ATLAS_STATE_WRITE_SPECS

    for spec in (*ATLAS_ATTACHMENT_SPECS, *ATLAS_LIFECYCLE_SPECS, *ATLAS_STATE_WRITE_SPECS):
        Draft202012Validator.check_schema(spec.input_schema)
        requested = spec.presentation.usage_example["input"]
        Draft202012Validator(spec.input_schema).validate(requested)
        output = FakeAtlasProviderAdapter().execute(spec, requested)
        Draft202012Validator(spec.output_schema).validate(output)
