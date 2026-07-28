"""Deterministic in-memory provider — no network, no external deps.

Used by tests/test_core.py and by `manage.py scan-once --provider Mock` to
exercise the whole scan → store → watch → notify path offline.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from .base import (
    STATUS_AVAILABLE,
    STATUS_FULL,
    Campground,
    Campsite,
    Provider,
    SearchRequest,
)


class MockProvider(Provider):
    """Yields a fixed, reproducible set of campsites and campgrounds.

    `available_keys` (optional) restricts which campsites are reported as
    available, so a test can simulate a site opening up or being booked.
    """

    name = "Mock"

    #: (campground_id, campground_name, state, lat, lon)
    CAMPGROUNDS = [
        ("mock-cg-1", "Mock Riverside Camp", "OR", 45.30, -121.90),
        ("mock-cg-2", "Mock Ridgeline Camp", "WA", 46.70, -121.50),
        ("mock-cg-3", "Mock Unlocatable Camp", "OR", None, None),
    ]

    #: (campsite_id, campground_id, site_name, loop, type)
    CAMPSITES = [
        ("mock-site-a", "mock-cg-1", "A01", "River Loop", "TENT ONLY"),
        ("mock-site-b", "mock-cg-1", "B07", "River Loop", "STANDARD NONELECTRIC"),
        ("mock-site-c", "mock-cg-2", "C12", "Ridge Loop", "STANDARD ELECTRIC"),
    ]

    def __init__(
        self,
        available_keys: Optional[set[str]] = None,
        state: Optional[str] = None,
    ):
        self.available_keys = available_keys
        self.state = state

    def _state_for(self, campground_id: str) -> Optional[str]:
        for cg_id, _name, cg_state, _lat, _lon in self.CAMPGROUNDS:
            if cg_id == campground_id:
                return cg_state
        return None

    def _coords(self, campground_id: str):
        for cg_id, _name, _st, lat, lon in self.CAMPGROUNDS:
            if cg_id == campground_id:
                return lat, lon
        return None, None

    def _name_for(self, campground_id: str) -> Optional[str]:
        for cg_id, name, _st, _lat, _lon in self.CAMPGROUNDS:
            if cg_id == campground_id:
                return name
        return None

    def search(self, req: SearchRequest) -> list[Campsite]:
        out: list[Campsite] = []
        day = req.start_date
        while day <= req.end_date - timedelta(days=req.nights - 1):
            if req.weekends_only and day.weekday() not in (4, 5):
                day += timedelta(days=1)
                continue
            for site_id, cg_id, site_name, loop, site_type in self.CAMPSITES:
                if req.campground_ids and cg_id not in req.campground_ids:
                    continue
                if req.campsite_ids and site_id not in req.campsite_ids:
                    continue
                lat, lon = self._coords(cg_id)
                site = Campsite(
                    provider=self.name,
                    campsite_id=site_id,
                    available_date=day,
                    nights=req.nights,
                    site_name=site_name,
                    loop=loop,
                    campsite_type=site_type,
                    status=STATUS_AVAILABLE,
                    rec_area="Mock National Forest",
                    rec_area_id="mock-ra-1",
                    facility_name=self._name_for(cg_id),
                    facility_id=cg_id,
                    booking_url=f"https://example.invalid/book/{site_id}?d={day}",
                    state=self.state or self._state_for(cg_id),
                    latitude=lat,
                    longitude=lon,
                )
                if self.available_keys is not None and site.key not in self.available_keys:
                    continue
                out.append(site)
            day += timedelta(days=1)
        return out

    def list_campgrounds(
        self,
        state: Optional[str] = None,
        rec_area_ids: Optional[list[str]] = None,
    ) -> list[Campground]:
        out = []
        for cg_id, name, cg_state, lat, lon in self.CAMPGROUNDS:
            if state and cg_state != state:
                continue
            out.append(
                Campground(
                    provider=self.name,
                    id=cg_id,
                    name=name,
                    rec_area="Mock National Forest",
                    state=cg_state,
                    latitude=lat,
                    longitude=lon,
                )
            )
        return out


def first_night(start: date, offset_days: int = 0) -> date:
    """Small helper so tests can express dates relative to a search window."""
    return start + timedelta(days=offset_days)


__all__ = ["MockProvider", "first_night", "STATUS_AVAILABLE", "STATUS_FULL"]
