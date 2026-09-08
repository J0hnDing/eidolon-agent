from __future__ import annotations

from typing import Any

from .atlas import AtlasProviderAdapter
from .github import GitHubProviderAdapter
from .gmail import GmailProviderAdapter
from .google_calendar import GoogleCalendarProviderAdapter
from .huggingface import HuggingFaceProviderAdapter
from .notion import NotionProviderAdapter
from .outlook import OutlookProviderAdapter
from .telegram import TelegramProviderAdapter


def build_provider_adapters(compatibility_service: Any) -> tuple[object, ...]:
    """Deterministic checked-in provider composition for the staged migration."""

    return (
        GitHubProviderAdapter(compatibility_service),
        AtlasProviderAdapter(compatibility_service),
        NotionProviderAdapter(compatibility_service),
        GoogleCalendarProviderAdapter(compatibility_service),
        GmailProviderAdapter(compatibility_service),
        OutlookProviderAdapter(compatibility_service),
        TelegramProviderAdapter(compatibility_service),
        HuggingFaceProviderAdapter(compatibility_service),
    )


__all__ = [
    "AtlasProviderAdapter",
    "GmailProviderAdapter",
    "GitHubProviderAdapter",
    "GoogleCalendarProviderAdapter",
    "HuggingFaceProviderAdapter",
    "NotionProviderAdapter",
    "OutlookProviderAdapter",
    "TelegramProviderAdapter",
    "build_provider_adapters",
]
