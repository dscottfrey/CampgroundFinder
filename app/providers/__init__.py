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
from .reserveamerica import ReserveAmericaProvider

#: ReserveAmerica portals. One platform, many agencies — keyed by contract
#: code (docs/reserveamerica-clients.md). Only OR is verified.
RESERVEAMERICA_HOSTS = {
    "OR": ("oregonstateparks.reserveamerica.com", "OR"),   # verified 2026-07-27
}

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

    family, _, instance = spec.partition(":")

    if family == "ReserveAmerica":
        host = options.get("host")
        contract = instance or options.get("contract_code")
        if not contract:
            raise ValueError(
                "ReserveAmerica needs a contract code, e.g. 'ReserveAmerica:OR'"
            )
        if not host:
            known = RESERVEAMERICA_HOSTS.get(contract)
            if not known:
                raise ValueError(
                    f"no known host for ReserveAmerica contract {contract!r} — "
                    f"set `host:` in the source config "
                    f"(see docs/reserveamerica-clients.md)"
                )
            host, default_state = known
            state = state or default_state
        return ReserveAmericaProvider(contract, host, state=state)

    normalized = CamplyProvider._normalize(family)
    if normalized in CAMPLY_PROVIDERS:
        return CamplyProvider(normalized, state=state)

    raise NotImplementedError(
        f"provider {spec!r} is not implemented yet (PerfectMind → §7 / step 7)"
    )


def known_providers() -> list[str]:
    ra = {f"ReserveAmerica:{code}" for code in RESERVEAMERICA_HOSTS}
    return sorted(CAMPLY_PROVIDERS | {"Mock"} | ra)


__all__ = [
    "Provider", "SearchRequest", "Campsite", "Campground",
    "CamplyProvider", "MockProvider", "ReserveAmericaProvider",
    "build_provider", "known_providers", "CAMPLY_PROVIDERS",
    "RESERVEAMERICA_HOSTS",
    "STATUS_AVAILABLE", "STATUS_FULL", "STATUS_CLOSED", "STATUS_UNKNOWN", "STATUS_STALE",
]
