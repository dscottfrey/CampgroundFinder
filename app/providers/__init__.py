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
from .goingtocamp import GoingToCampProvider
from .mock import MockProvider
from .reserveamerica import ReserveAmericaProvider

#: ReserveAmerica portals. One platform, many agencies — keyed by contract
#: code (docs/reserveamerica-clients.md). Only OR is verified.
RESERVEAMERICA_HOSTS = {
    "OR": ("oregonstateparks.reserveamerica.com", "OR"),   # verified 2026-07-27
}

#: GoingToCamp portals: (host, rec_area_id, region). Rec-area IDs read from
#: camply's rec_areas.py; WA verified live 2026-07-28 (79 campable locations).
GOINGTOCAMP_HOSTS = {
    "WA": ("washington.goingtocamp.com", 3, "WA"),          # verified 2026-07-28
    "TacomaPower": ("tacomapower.goingtocamp.com", 6, "WA"),
    "BC": ("camping.bcparks.ca", 12, "BC"),
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

    if family == "GoingToCamp":
        # Our own client, not camply's — camply's GoingToCamp raises
        # KeyError before returning anything (see goingtocamp.py).
        known = GOINGTOCAMP_HOSTS.get(instance)
        if not known:
            raise ValueError(
                f"unknown GoingToCamp portal {instance!r} — expected one of "
                f"{sorted(GOINGTOCAMP_HOSTS)} (see docs/goingtocamp-clients.md)"
            )
        host, rec_area_id, default_state = known
        return GoingToCampProvider(
            instance, options.get("host") or host, rec_area_id,
            state=state or default_state,
        )

    normalized = CamplyProvider._normalize(family)
    if normalized in CAMPLY_PROVIDERS:
        return CamplyProvider(normalized, state=state)

    raise NotImplementedError(
        f"provider {spec!r} is not implemented yet (PerfectMind → §7 / step 7)"
    )


def known_providers() -> list[str]:
    ra = {f"ReserveAmerica:{code}" for code in RESERVEAMERICA_HOSTS}
    gtc = {f"GoingToCamp:{code}" for code in GOINGTOCAMP_HOSTS}
    # camply's own GoingToCamp entry is deliberately shadowed by ours.
    return sorted((CAMPLY_PROVIDERS - {"GoingToCamp"}) | {"Mock"} | ra | gtc)


__all__ = [
    "Provider", "SearchRequest", "Campsite", "Campground",
    "CamplyProvider", "MockProvider", "ReserveAmericaProvider",
    "GoingToCampProvider", "GOINGTOCAMP_HOSTS",
    "build_provider", "known_providers", "CAMPLY_PROVIDERS",
    "RESERVEAMERICA_HOSTS",
    "STATUS_AVAILABLE", "STATUS_FULL", "STATUS_CLOSED", "STATUS_UNKNOWN", "STATUS_STALE",
]
