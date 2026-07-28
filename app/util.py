"""Small shared helpers: time, distance, date parsing."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

EARTH_RADIUS_MILES = 3958.7613


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_date(value: Any) -> Optional[date]:
    """Coerce str / datetime / date to a `date`."""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def haversine_miles(lat1, lon1, lat2, lon2) -> Optional[float]:
    """Great-circle distance in miles, or None if either point is unlocated.

    Returning None (not a huge number) is deliberate: unlocated sites must be
    excluded from distance filtering, never silently ranked last or dropped (§13).
    """
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def is_weekend_night(day: date) -> bool:
    """Friday or Saturday night — matches camply's weekends_only semantics."""
    return day.weekday() in (4, 5)


def date_range(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}
