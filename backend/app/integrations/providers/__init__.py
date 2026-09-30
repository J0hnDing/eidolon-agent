from __future__ import annotations

from typing import Any

from .atlas import AtlasProviderAdapter
from .github import GitHubProviderAdapter
from .gmail import GmailProviderAdapter
from .google_calendar import GoogleCalendarProviderAdapter
from .google_drive import GoogleDriveProviderAdapter
from .huggingface import HuggingFaceProviderAdapter
from .macro import MacroProviderAdapter
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
        GoogleDriveProviderAdapter(compatibility_service),
        GmailProviderAdapter(compatibility_service),
        OutlookProviderAdapter(compatibility_service),
        TelegramProviderAdapter(compatibility_service),
        HuggingFaceProviderAdapter(compatibility_service),
        MacroProviderAdapter("fred", compatibility_service),
        MacroProviderAdapter("bls", compatibility_service),
        MacroProviderAdapter("bea", compatibility_service),
        MacroProviderAdapter("eia", compatibility_service),
    )


__all__ = [
    "AtlasProviderAdapter",
    "GmailProviderAdapter",
    "GitHubProviderAdapter",
    "GoogleCalendarProviderAdapter",
    "GoogleDriveProviderAdapter",
    "HuggingFaceProviderAdapter",
    "MacroProviderAdapter",
    "NotionProviderAdapter",
    "OutlookProviderAdapter",
    "TelegramProviderAdapter",
    "build_provider_adapters",
]
