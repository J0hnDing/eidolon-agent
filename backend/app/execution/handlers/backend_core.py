from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy.orm import Session

from app.execution.context import InvocationContext
from app.execution.types import InvocationExecutionError, InvocationOutcome, InvocationTargetRef
from app.models import InvocationApproval
from app.schemas.skill_codex import SkillCodexRequest, SkillCodexResponse
from app.services.act_download_service import ActDownloadError, download_document
from app.services.codex_service import CodexGenerationError
from app.services.skill_codex_runtime_service import (
    SkillCodexInvalidRequest,
    SkillCodexRuntimeService,
    SkillCodexUnavailable,
)


class BackendCoreHandler:
    def __init__(self, db: Session, *, project_root=None) -> None:
        self.db = db
        self.project_root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parents[4]

    def execute(
        self,
        target: InvocationTargetRef,
        input_json: dict,
        context: InvocationContext,
    ) -> InvocationOutcome:
        if target.target_id == "act.document.download":
            try:
                Draft202012Validator(ACT_DOCUMENT_DOWNLOAD_INPUT_SCHEMA).validate(input_json)
                output = download_document(input_json)
                Draft202012Validator(ACT_DOCUMENT_DOWNLOAD_OUTPUT_SCHEMA).validate(output)
            except ValidationError as exc:
                raise InvocationExecutionError("invalid_input", "Document download input is invalid") from exc
            except ActDownloadError as exc:
                raise InvocationExecutionError("download_failed", str(exc)) from None
            return InvocationOutcome(status="succeeded", output=output)
        if target.target_id == "backend.codex.call":
            try:
                payload = SkillCodexRequest.model_validate(input_json)
                output = SkillCodexRuntimeService(
                    self.db,
                    project_root=self.project_root,
                ).call_context(context, payload)
                output = SkillCodexResponse.model_validate(output).model_dump(mode="json")
            except SkillCodexInvalidRequest as exc:
                raise InvocationExecutionError("invalid_input", str(exc)) from None
            except SkillCodexUnavailable as exc:
                raise InvocationExecutionError("function_unavailable", str(exc)) from None
            except CodexGenerationError as exc:
                raise InvocationExecutionError("codex_failed", str(exc)) from None
            return InvocationOutcome(status="succeeded", output=output)
        raise InvocationExecutionError("not_exposed", "Backend-core function has no registered handler")

    def execute_approved(
        self,
        approval: InvocationApproval,
        context: InvocationContext,
    ) -> InvocationOutcome:
        raise InvocationExecutionError("invalid_target", "Backend-core functions do not use this approval path")


ACT_DOCUMENT_DOWNLOAD_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "const": "url"},
                "url": {"type": "string", "minLength": 1, "maxLength": 2048},
            },
            "required": ["kind", "url"],
            "additionalProperties": False,
        },
        "filename": {"type": "string", "minLength": 1, "maxLength": 180},
    },
    "required": ["source"],
    "additionalProperties": False,
}

ACT_DOCUMENT_DOWNLOAD_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "filename": {"type": "string"},
        "bytes": {"type": "integer", "minimum": 0},
        "media_type": {"type": "string"},
    },
    "required": ["path", "filename", "bytes", "media_type"],
    "additionalProperties": False,
}
