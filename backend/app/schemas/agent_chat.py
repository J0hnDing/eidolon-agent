"""Shared title metadata for conversational agent response contracts."""

SESSION_TITLE_SCHEMA = {
    "anyOf": [
        {"type": "string", "minLength": 1, "maxLength": 80},
        {"type": "null"},
    ],
    "description": (
        "A concise, specific session title of 3-8 words, generated alongside the answer. "
        "Supply it when eidolon_session_metadata.needsTitle is true; otherwise return null."
    ),
}
