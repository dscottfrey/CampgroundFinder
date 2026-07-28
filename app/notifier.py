"""Alert delivery (§11). Apprise when installed; a recording stub otherwise."""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from .providers.base import Campsite

log = logging.getLogger(__name__)


def format_alert(site: Campsite) -> str:
    where = site.facility_name or site.rec_area or site.provider
    line = f"{where} — site {site.site_name} open {site.available_date} ({site.nights}n)"
    if site.reservation_type == "first_come":
        # FCFS has no booking link by definition (§4) — say so rather than
        # emitting a dead link.
        return f"{line} [first-come, first-served — no booking link]"
    if site.booking_url:
        return f"{line}\n{site.booking_url}"
    return line


def format_digest(sites: Sequence[Campsite], title: str) -> str:
    """Batched digest (§8b) so a popular weekend opening isn't a ping storm."""
    body = "\n\n".join(format_alert(s) for s in sites)
    return f"{title} — {len(sites)} new\n\n{body}"


class Notifier:
    """Sends via Apprise. Falls back to logging when Apprise isn't installed."""

    def __init__(self, default_targets: Optional[Iterable[str]] = None):
        self.default_targets = list(default_targets or [])
        self.sent: list[tuple[tuple[str, ...], str]] = []  # inspectable in tests

    def _apprise(self, targets: Sequence[str]):
        try:
            import apprise
        except ImportError:
            return None
        client = apprise.Apprise()
        for target in targets:
            client.add(target)
        return client

    def send(
        self,
        message: str,
        targets: Optional[Sequence[str]] = None,
        title: str = "CampgroundFinder",
    ) -> bool:
        targets = list(targets or self.default_targets)
        self.sent.append((tuple(targets), message))
        if not targets:
            log.warning("no notify targets configured; dropping alert: %s", message)
            return False
        client = self._apprise(targets)
        if client is None:
            log.info("[apprise not installed] would notify %s: %s", targets, message)
            return False
        return bool(client.notify(body=message, title=title))

    def send_sites(
        self,
        sites: Sequence[Campsite],
        targets: Optional[Sequence[str]] = None,
        batch: bool = False,
        title: str = "CampgroundFinder",
    ) -> int:
        """Returns how many alerts were dispatched (1 for a batched digest)."""
        if not sites:
            return 0
        if batch:
            self.send(format_digest(sites, title), targets, title=title)
            return 1
        for site in sites:
            self.send(format_alert(site), targets, title=title)
        return len(sites)
