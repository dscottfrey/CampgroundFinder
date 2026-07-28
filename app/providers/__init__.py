"""Provider registry — add a source by registering one class (§4c)."""

from __future__ import annotations

from typing import Optional

from .base import (
    STATUS_AVAILABLE,
    STATUS_CLOSED,
    STATUS_FULL,
    STATUS_STALE,
    STATUS_UNKNOWN,
    Campground,
    Campsite,
    Provider,
    SearchRequest,
)
from .camply_provider import CamplyProvider
from .mock import MockProvider

#: camply provider names verified present in samples/camply-main (19 classes).
CAMPLY_PROVIDERS = {
    "RecreationDotGov",
    "RecreationDotGovDailyTicket",
    "RecreationDotGovDailyTimedEntry",
    "RecreationDotGovTicket",
    "RecreationDotGovTimedEntry",
    "GoingToCamp",
    "Yellowstone",
    "ReserveCalifornia",
    "NorthernTerritory",
    "AlabamaStateParks",
    "ArizonaStateParks",
    "FairfaxCountyParks",
    "FloridaStateParks",
    "MaricopaCountyParks",
    "MinnesotaStateParks",
    "MissouriStateParks",
    "OhioStateParks",
    "OregonMetro",
    "VirginiaStateParks",
}


def build_provider(spec: str, state: Optional[str] = None, **options) -> Provider:
    """Instantiate a provider from a config `provider:` string.

    Custom providers use a "Family:Instance" form, e.g.
    "PerfectMind:SanJuanCoWA" — those land in later build steps (§7, §4d).
    """
    if spec in ("Mock", "MockProvider"):
        return MockProvider(state=state)

    family = spec.split(":", 1)[0]
    normalized = CamplyProvider._normalize(family)
    if normalized in CAMPLY_PROVIDERS:
        return CamplyProvider(normalized, state=state)

    raise NotImplementedError(
        f"provider {spec!r} is not implemented yet "
        f"(PerfectMind → §7 / step 7, ReserveAmerica → §4d / step 8)"
    )


def known_providers() -> list[str]:
    return sorted(CAMPLY_PROVIDERS | {"Mock"})


__all__ = [
    "Provider", "SearchRequest", "Campsite", "Campground",
    "CamplyProvider", "MockProvider",
    "build_provider", "known_providers", "CAMPLY_PROVIDERS",
    "STATUS_AVAILABLE", "STATUS_FULL", "STATUS_CLOSED", "STATUS_UNKNOWN", "STATUS_STALE",
]
