"""Semantic contracts for Atlas's discoverable, read-only data capability."""

from app.integrations.types import (
    IntegrationEffect,
    IntegrationOperationSpec,
    OperationPresentation,
    ResourceSpec,
    RiskLevel,
)


def _object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_ID = {"type": "string", "minLength": 1, "maxLength": 128}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}
_CURSOR = {"type": "string", "minLength": 1, "maxLength": 1024}
_JSON_OBJECT = {"type": "object"}
_PAGE = _object({
    "items": {"type": "array", "maxItems": 100, "items": _JSON_OBJECT},
    "next_cursor": {"type": ["string", "null"]},
    "total": {"type": "integer", "minimum": 0},
}, ("items", "next_cursor", "total"))
_ERRORS = (
    "atlas_locked", "invalid_input", "not_found", "stale_revision", "provider_unavailable",
    "provider_timeout", "response_too_large", "operation_undeclared", "authorization_missing_or_stale",
)


def _read(name: str, description: str, inputs: dict, output: dict, example: dict,
          required: tuple[str, ...] = (), identity: tuple[str, ...] = (),
          version: int = 1) -> IntegrationOperationSpec:
    return IntegrationOperationSpec(
        id=f"atlas.{name}", title=f"Atlas {name.replace('.', ' ')}", description=description,
        input_schema=_object(inputs, required), output_schema=output,
        effects=frozenset({IntegrationEffect.READ}), resource=ResourceSpec(f"atlas.{name}", identity),
        risk=RiskLevel.LOW,
        contract_version=version,
        presentation=OperationPresentation(
            usage_example={"operation": f"atlas.{name}", "input": example}, normalized_errors=_ERRORS,
        ),
    )


ATLAS_READ_SPECS = (
    _read("describe", "Discover current Atlas schemas, categories, model catalogs, custom fields and read guidance. "
          "Use overview first, then api_reference (optionally one documented path), model or custom_fields. "
          "Schemas describe data; they do not grant access to arbitrary endpoints.",
          {"section": {"enum": ["overview", "api_reference", "model", "custom_fields"]},
           "category": _ID, "path": {"type": "string", "minLength": 1, "maxLength": 200}},
          _object({"description": _JSON_OBJECT}, ("description",)), {}),
    _read("records.search", "Search/list any current Atlas record category, including all custom fields. "
          "Returns compact identities by default; set include_data for complete records or use records.get. "
          "Follow next_cursor; restart search if the source changed. Includes trashed records only when selected.",
          {"category": _ID, "query": {"type": "string", "maxLength": 1000},
           "trashed": {"enum": ["false", "true", "all"]}, "include_data": {"type": "boolean"},
           "limit": _LIMIT, "cursor": _CURSOR}, _PAGE, {"category": "person", "limit": 10}),
    _read("records.get", "Read a complete native Atlas record, including data, custom fields, provenance, "
          "outgoing links and backlinks. This explicit read includes sensitive Person fields. "
          "Person completeness and attachment content have separate reads.",
          {"id": _ID}, _object({"record": _JSON_OBJECT}, ("record",)), {"id": "record-id"}, ("id",), ("id",)),
    _read("links.get", "Read the complete current Atlas relationship/link record by link ID.",
          {"id": _ID}, _object({"link": _JSON_OBJECT}, ("link",)), {"id": "link-id"}, ("id",), ("id",)),
    _read("person.completeness.get", "Read Atlas's own overall and per-area Person information completeness "
          "percentages, criteria, and available missing-field lists; uses the same calculation as Atlas's UI.",
          {}, _object({"completeness": _JSON_OBJECT}, ("completeness",)), {}),
    _read("states.get", "Read complete current Atlas state estimates plus per-record goal, interest and "
          "relationship satisfaction targets, confidence, evidence and elapsed time since relevant events. "
          "The existing goal_progression combines Goals and Interests; close_connection combines personal "
          "Relationships. Exactly seven top-level States remain, with canonical settings and component coverage. "
          "Optionally read at an ISO timestamp or read configuration including per-record decay rates. "
          "A prior-only estimate is uncertainty, not evidence of dissatisfaction. This does not save or change states.",
          {"view": {"enum": ["current", "configuration"]},
           "at": {"type": "string", "minLength": 1, "maxLength": 64}},
          _object({"states": _JSON_OBJECT}, ("states",)), {}, version=3),
    _read("personality.get", "Read the complete Atlas Personality projection, spectra, and exact imported "
          "Valence model where present, preserving native precision and unknown values.",
          {}, _object({"personality": _JSON_OBJECT}, ("personality",)), {}),
    _read("history.list", "Read paginated complete record revisions, link revisions, or States event/manual "
          "estimate history. record/link require id; states does not. Follow next_cursor.",
          {"kind": {"enum": ["record", "link", "states"]}, "id": _ID, "limit": _LIMIT, "cursor": _CURSOR},
          _PAGE, {"kind": "record", "id": "record-id"}, ("kind",)),
    _read("graph.get", "Read Atlas's complete motivational graph or the graph focused on one Goal, "
          "including Ends, Beliefs, connections and derived grounding status.",
          {"goal_id": _ID}, _object({"graph": _JSON_OBJECT}, ("graph",)), {}),
    _read("knowledge.read", "Read complete native Knowledge nodes (paged), one node, or the tree with its "
          "connections. Use nodes when the complete tree exceeds the bounded response limit.",
          {"view": {"enum": ["nodes", "node", "tree"]},
           "node_id": {"type": "integer", "minimum": 1}, "limit": _LIMIT, "cursor": _CURSOR},
          _object({"data": {}, "next_cursor": {"type": ["string", "null"]},
                   "total": {"type": ["integer", "null"]}}, ("data", "next_cursor", "total")), {}),
    _read("attachments.list", "Read native attachment metadata for one record: Experience images or Person "
          "resume. Read attachment content explicitly with attachments.read.",
          {"record_id": _ID}, _object({"images": {"type": "array", "items": _JSON_OBJECT},
                                      "resume": {"type": ["object", "null"]}}, ("images", "resume")),
          {"record_id": "record-id"}, ("record_id",), ("record_id",)),
    _read("attachments.read", "Read an Atlas image (id=image ID) or resume (id=Person record ID). "
          "Returns paged base64 bytes, or extracted resume text when format=text. Follow next_offset; "
          "supply returned sha256 on subsequent chunks to detect replacement. No file is saved.",
          {"kind": {"enum": ["image", "resume"]}, "id": _ID,
           "format": {"enum": ["base64", "text"]},
           "offset": {"type": "integer", "minimum": 0},
           "limit": {"type": "integer", "minimum": 1, "maximum": 131072, "default": 65536},
           "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
          _object({"content": {"type": "string"}, "encoding": {"enum": ["base64", "text"]},
                   "media_type": {"type": "string"}, "total": {"type": "integer", "minimum": 0},
                   "next_offset": {"type": ["integer", "null"]}, "sha256": {"type": "string"}},
                  ("content", "encoding", "media_type", "total", "next_offset", "sha256")),
          {"kind": "resume", "id": "person-id", "format": "text"}, ("kind", "id"), ("id",)),
)
