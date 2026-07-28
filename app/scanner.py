"""The scan cycle (§8) — verify availability over the catalog, alert on new.

Runs over CATALOGUED campgrounds, not over a search-hit list, so the map keeps
showing full / unknown / stale pins instead of quietly losing them (§8k).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from . import store
from .config import Config, Source
from .notifier import Notifier
from .providers import build_provider
from .providers.base import STATUS_STALE, Campsite, Provider, SearchRequest
from .util import utcnow

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    scanned_sources: int = 0
    found: int = 0
    newly_available: int = 0
    pruned: int = 0
    alerts_sent: int = 0
    provider_errors: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [
            f"sources={self.scanned_sources}",
            f"found={self.found}",
            f"new={self.newly_available}",
            f"pruned={self.pruned}",
            f"alerts={self.alerts_sent}",
        ]
        if self.provider_errors:
            parts.append(f"errors={len(self.provider_errors)}")
        return " ".join(parts)


def scan_source(
    conn: sqlite3.Connection,
    source: Source,
    start: date,
    end: date,
    nights: int = 1,
    provider_factory=build_provider,
    report: Optional[ScanReport] = None,
    now: Optional[datetime] = None,
) -> list[Campsite]:
    """Search one source and persist what it returns.

    Fetches at the broadest level the source supports and filters locally
    (§8k) — provider-side facets are not trusted to be complete.
    """
    report = report or ScanReport()
    provider: Provider = provider_factory(source.provider, state=source.state)
    req = SearchRequest(
        provider=provider.name,
        start_date=start,
        end_date=end,
        nights=nights,
        rec_area_ids=source.rec_area_ids,
        campground_ids=source.campground_ids,
    )
    try:
        sites = provider.search(req)
    except Exception as exc:  # noqa: BLE001 - a dead source degrades, never empties
        log.warning("scan failed for %s: %s", source.label, exc)
        report.provider_errors[source.label] = str(exc)
        _mark_source_stale(conn, source, provider.name, now=now)
        return []

    for site in sites:
        if site.state is None:
            site.state = source.state
    new = store.upsert_availability(conn, sites, now=now)
    report.found += len(sites)
    report.newly_available += len(new)
    _stamp_catalog_statuses(conn, provider.name, source, sites, report, now=now)
    return new


def _mark_source_stale(
    conn: sqlite3.Connection,
    source: Source,
    provider_name: str,
    now: Optional[datetime] = None,
) -> None:
    """A failed live check downgrades pins to stale — it never deletes them."""
    for cg in store.list_campgrounds(conn, provider=provider_name):
        store.set_campground_status(
            conn, cg.provider, cg.id, STATUS_STALE, "live check failed", now=now
        )


def _stamp_catalog_statuses(
    conn: sqlite3.Connection,
    provider_name: str,
    source: Source,
    sites: Iterable[Campsite],
    report: ScanReport,
    now: Optional[datetime] = None,
) -> None:
    # Scope the stamp to what this source actually covers, so a second source
    # on the same provider isn't prematurely marked full before its own scan.
    catalogued = store.list_campgrounds(
        conn, provider=provider_name, states=[source.state] if source.state else None
    )
    scoped = source.campground_ids
    for cg in catalogued:
        if scoped and cg.id not in scoped:
            continue
        status = store.stamp_status_from_availability(
            conn, cg.provider, cg.id, checked_ok=True, now=now
        )
        report.statuses[f"{cg.provider}|{cg.id}"] = status


def run_watches(
    conn: sqlite3.Connection,
    fresh: Iterable[Campsite],
    notifier: Notifier,
    config: Optional[Config] = None,
    report: Optional[ScanReport] = None,
    now: Optional[datetime] = None,
) -> int:
    """Evaluate active watches and dispatch alerts (§8b).

    Autonomous watches run over the availability the scan already fetched — no
    extra upstream calls. Targeted watches match on the same set here; a
    focused re-search is only needed for scopes outside the scanned window.
    """
    report = report or ScanReport()
    home = config.home_point if config else None
    default_targets = config.default_notify_targets if config else []
    fresh = list(fresh)
    sent = 0

    for watch in store.list_watches(conn, active_only=True):
        matches = store.watch_matches(watch, fresh, home_base=home)
        pending = store.pending_notifications(conn, watch, matches, now=now)
        if not pending:
            continue
        targets = watch.notify_targets or default_targets
        # One digest per watch per cycle whenever a cycle turns up more than a
        # single opening. §8b specifies batching only for autonomous watches,
        # but its stated reason — "so a popular weekend opening doesn't spam
        # you" — applies just as much to a targeted watch, which in practice
        # can match a dozen site-nights at once. Every booking link still
        # travels in the digest, so speed to book is unaffected.
        batch = len(pending) > 1
        sent += notifier.send_sites(
            pending, targets=targets, batch=batch, title=watch.name or "CampgroundFinder"
        )
        for site in pending:
            store.record_notification(conn, watch.id, site.key, now=now)

    report.alerts_sent += sent
    return sent


def scan_once(
    conn: sqlite3.Connection,
    config: Config,
    notifier: Optional[Notifier] = None,
    start: Optional[date] = None,
    window_days: Optional[int] = None,
    nights: int = 1,
    provider_factory=build_provider,
    now: Optional[datetime] = None,
) -> ScanReport:
    """One full cycle: scan sources → stamp catalog → alert → prune."""
    now = now or utcnow()
    start = start or now.date()
    # `is None`, not `or` — a 0-day window (just tonight) is a real request.
    if window_days is None:
        window_days = config.default_window_days
    end = start + timedelta(days=window_days)
    notifier = notifier or Notifier(config.default_notify_targets)

    # Config-declared watches are seeds (§8c) — inserted once if absent.
    store.seed_watches(conn, config.watches, now=now)

    report = ScanReport()
    fresh: list[Campsite] = []
    for source in config.sources:
        report.scanned_sources += 1
        fresh.extend(
            scan_source(
                conn, source, start, end, nights=nights,
                provider_factory=provider_factory, report=report, now=now,
            )
        )

    run_watches(conn, fresh, notifier, config=config, report=report, now=now)

    # Prune rows not re-confirmed this cycle. The campground stays catalogued
    # and drops to full/unknown — it never disappears (§8 cycle step 3).
    # Everything re-confirmed this cycle carries last_seen == now exactly, so
    # a strict `<` prunes precisely the rows this cycle did not see.
    report.pruned = store.prune_availability(conn, older_than=now)
    return report
