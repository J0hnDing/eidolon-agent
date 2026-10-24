"""The Assistant's daily session spans local 04:00 to the next local 04:00."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

REFRESH_TIME = "04:00"


def refresh_boundary(now: datetime, timezone: str, *, next_boundary: bool = False) -> datetime:
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    day = local.date()
    if local.time() < time(4):
        day -= timedelta(days=1)
    if next_boundary:
        day += timedelta(days=1)
    # Construct each day's wall-clock boundary separately so DST preserves 04:00.
    return datetime.combine(day, time(4), tzinfo=zone).astimezone(UTC)


def refresh_is_due(started_at: datetime | None, now: datetime, timezone: str) -> bool:
    if started_at is None:
        return True
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return started_at < refresh_boundary(now, timezone)
