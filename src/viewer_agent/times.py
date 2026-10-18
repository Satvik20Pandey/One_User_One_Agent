"""Time helpers. Everything is stored as UTC ISO strings; policy rules (quiet hours, daily caps) are in IST."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ist_day(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def in_quiet_hours(dt: datetime, start_hour: int = 22, end_hour: int = 8) -> bool:
    hour = dt.astimezone(IST).hour
    return hour >= start_hour or hour < end_hour


def next_send_slot(after: datetime, hour_ist: int) -> datetime:
    """The next `hour_ist` IST that is at least an hour away, so a message never lands in the session the viewer
    is already in: a morning open is followed up the same evening, an evening open the next day."""
    local = after.astimezone(IST)
    slot = local.replace(hour=hour_ist, minute=0, second=0, microsecond=0)
    if slot < local + timedelta(hours=1):
        slot += timedelta(days=1)
    return slot.astimezone(UTC)
