"""One shared rate limiter for the whole process (docs/scanning-design.md).

The single most important rule in this file: **tier 1 (the background sweep)
and tier 2 (on-demand refresh) draw from the same budget.** Ten people zooming
the map at once queues behind the sweep instead of bursting, which is what
turns user-driven load from unbounded into bounded.

The rules this implements, verbatim from the design:

  * One request at a time. Never parallel, ever. — a single process-wide lock
    is held across each upstream call, so there is no way to have two in
    flight.
  * 6 seconds between requests for ReserveAmerica, 2 for RIDB. — per-host
    spacing, measured **end of one request to the start of the next**, so a
    slow response makes the gap longer, never shorter.
  * Stop dead on 403 or 429. Skip that provider for the cycle. — `block()`
    latches a host off for a cooldown; every later `slot()` on it raises
    instead of retrying into the block.

Spacing is keyed by **host**, not by provider, because that is what a rate
limiter on the other end actually measures. Two providers sharing a host share
its budget automatically.

Nothing here sleeps in a way tests have to endure: `sleep` and `clock` are
injectable, and `RateLimiter(delays={...}, min_gap=0)` runs instantly.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Seconds between consecutive requests to one host. Keyed by host suffix —
#: the longest matching suffix wins, so `oregonstateparks.reserveamerica.com`
#: picks up the ReserveAmerica figure without being listed itself.
HOST_DELAYS = {
    "reserveamerica.com": 6.0,      # guards its traffic harder than anything else (§13)
    "recreation.gov": 2.0,          # covers ridb.recreation.gov and www.recreation.gov
    "goingtocamp.com": 6.0,
    "bcparks.ca": 6.0,
    "usedirect.com": 6.0,
    "perfectmind.com": 6.0,
}

#: An unlisted host gets the slowest setting, not the fastest. Being wrong in
#: the polite direction costs minutes; being wrong the other way costs the
#: household's IP address.
DEFAULT_DELAY = 6.0

#: A floor between any two upstream requests, whatever their hosts. Round-robin
#: deliberately interleaves hosts to widen per-host gaps; this stops that from
#: turning into a burst as seen from the network.
GLOBAL_MIN_GAP = 1.0

#: How long a 403/429 latches a host off. Longer than one scan cycle on
#: purpose — "skip it for the cycle" is the floor, not the ceiling.
BLOCK_COOLDOWN_SECONDS = 3600.0

#: Plain-language copy for the progress widget. Lives here because it explains
#: this file's behaviour, and it must stay honest if the numbers above change.
PACING_NOTE = "Going slowly on purpose so the camping websites don't block us."


class Blocked(RuntimeError):
    """A host told us to stop (403/429). Never retried into — see `block()`."""


def host_key(url_or_host: str) -> str:
    """Normalize a URL or bare hostname to the key used for spacing."""
    value = (url_or_host or "").strip().lower()
    if "//" in value:
        value = urlparse(value).netloc or value
    return value.split("@")[-1].split(":")[0].strip("/")


class RateLimiter:
    """Serializes and spaces every upstream request in the process."""

    def __init__(
        self,
        delays: Optional[dict] = None,
        default_delay: float = DEFAULT_DELAY,
        min_gap: float = GLOBAL_MIN_GAP,
        cooldown: float = BLOCK_COOLDOWN_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._delays = {host_key(k): float(v) for k, v in (delays or HOST_DELAYS).items()}
        self._default_delay = float(default_delay)
        self._min_gap = float(min_gap)
        self._cooldown = float(cooldown)
        self._sleep = sleep
        self._clock = clock
        # Held across a whole upstream call: this is the "one at a time" rule.
        self._turn = threading.Lock()
        # Guards the bookkeeping below, and is never held across a sleep.
        self._book = threading.Lock()
        self._last: dict[str, float] = {}
        self._last_any = 0.0
        self._blocks: dict[str, tuple[float, str]] = {}
        self.requests = 0
        #: Optional callback(host, seconds, label) fired before a wait, so the
        #: interface can say *why* it is slow instead of showing a bare spinner.
        self.on_wait: Optional[Callable[[str, float, Optional[str]], None]] = None

    # -- spacing -----------------------------------------------------------

    def delay_for(self, host: str) -> float:
        """Longest-suffix match against `HOST_DELAYS`, else the default."""
        key = host_key(host)
        best: Optional[str] = None
        for candidate in self._delays:
            if key == candidate or key.endswith("." + candidate):
                if best is None or len(candidate) > len(best):
                    best = candidate
        return self._delays[best] if best else self._default_delay

    def wait_time(self, host: str) -> float:
        """Seconds the next request to `host` would have to wait right now.

        Read by the progress widget, so a queued user sees a real number
        rather than an unexplained pause.
        """
        with self._book:
            return self._wait_time(host_key(host), self._clock())

    def _wait_time(self, key: str, now: float) -> float:
        waits = [0.0]
        last = self._last.get(key)
        if last is not None:
            waits.append(self.delay_for(key) - (now - last))
        if self._last_any:
            waits.append(self._min_gap - (now - self._last_any))
        return max(waits)

    @contextmanager
    def slot(self, host: str, label: Optional[str] = None):
        """Hold the process's single request slot for one upstream call.

        Raises `Blocked` — before waiting, and again after the queue clears —
        if the host is latched off. Timestamps are written on the way *out*, so
        the gap is measured from when the last response landed.
        """
        key = host_key(host)
        self.check(key)
        with self._turn:
            # Re-checked here: a block can land while this call sits in the
            # queue behind another request to the same host.
            self.check(key)
            wait = self.wait_time(key)
            if wait > 0:
                if self.on_wait:
                    self.on_wait(key, wait, label)
                log.debug("pacing %s: waiting %.1fs", key, wait)
                self._sleep(wait)
            try:
                yield
            finally:
                with self._book:
                    now = self._clock()
                    self._last[key] = now
                    self._last_any = now
                    self.requests += 1

    # -- blocks ------------------------------------------------------------

    def block(self, host: str, reason: str) -> None:
        """Latch a host off after a 403/429. Never retry into a block (§13)."""
        key = host_key(host)
        with self._book:
            self._blocks[key] = (self._clock() + self._cooldown, reason)
        log.warning("pacing %s: blocked for %.0fs — %s", key, self._cooldown, reason)

    def blocked_reason(self, host: str) -> Optional[str]:
        key = host_key(host)
        with self._book:
            entry = self._blocks.get(key)
            if not entry:
                return None
            until, reason = entry
            if self._clock() >= until:
                del self._blocks[key]
                return None
            return reason

    def is_blocked(self, host: str) -> bool:
        return self.blocked_reason(host) is not None

    def check(self, host: str) -> None:
        reason = self.blocked_reason(host)
        if reason:
            raise Blocked(reason)

    def clear_blocks(self) -> None:
        with self._book:
            self._blocks.clear()

    # -- waiting for something other than a request ------------------------

    def pause(self, seconds: float) -> None:
        """Sleep through the limiter's own clock.

        The pause between round-robin rounds goes through here so that a test
        injecting `sleep` silences every wait in the process, not most of them.
        """
        if seconds > 0:
            self._sleep(seconds)


_shared: Optional[RateLimiter] = None
_shared_lock = threading.Lock()


def shared_limiter() -> RateLimiter:
    """The one limiter every provider in this process must go through."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = RateLimiter()
        return _shared


def set_shared_limiter(limiter: Optional[RateLimiter]) -> None:
    """Swap the shared limiter — for tests, and for a headless one-off run."""
    global _shared
    with _shared_lock:
        _shared = limiter
