"""Does a rig fit? — reading unmeasured driveway data honestly.

## The problem

ReserveAmerica asks a park to state a driveway length for every site. A
Beverly Beach manager told Scott directly that when they went online they had
no staffing budget to measure, so most sites were entered at a default.

## Scott's tell, which the data confirms

**No real campground has most of its sites at the exact same length.** A
forested loop is not laid out that way — sites bend around trees and terrain.
So a value that repeats across most of a park is a *form default*, while a
value that appears rarely is one somebody actually looked at.

Beverly Beach loop A, as listed:

    20 Back-In   x21     <- default, physically implausible
    15 Back-In   x1      <- believable; the site really is 15 feet
    21 Back-In   x1
    30 Back-In   x1
    (blank)      x1      <- EMU, a MEETING HALL — not a campsite

Ground truth from Scott: A01 is listed 20 and is really **53 feet**. A15 is
listed 15 and really is 15.

## What that licenses

* A **default** value is a floor and a loose one. A rig longer than it may
  well still fit, so "too small" is *unknown*, never "no".
* A **specific** value was entered deliberately. Believe it: a rig longer than
  it probably does not fit, and saying so is honest rather than hiding.
* A **blank** means no driveway was stated. At Stub Stewart that lines up with
  the `WALK TO` sites exactly — but Beverly Beach's `EMU` is a `MEETING HALL`
  with a blank cell, so it can equally mean "not a campsite". Excluding it from
  a vehicle search is right either way; the *reason* is not always walk-to.

That is the §8g three-state rule again, with the twist that *how much to trust
a number depends on how often it repeats*.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

log = logging.getLogger(__name__)

#: A length occupying at least this share of a park's stated lengths is taken
#: to be a form default rather than a measurement. Beverly Beach loop A sits at
#: 21/24 = 0.88; a genuinely varied park should not come close.
DEFAULT_SHARE = 0.5

#: Below this many sites the share is meaningless — three sites reading 20 is
#: not evidence of anything.
MIN_SAMPLE = 6

FITS = True
DOES_NOT_FIT = False
UNKNOWN = None


@dataclass
class LengthReading:
    """One site's stated length, and how much it can be trusted."""

    feet: Optional[int]
    is_default: bool          # part of a suspiciously repeated value
    has_driveway: bool

    @property
    def trustworthy(self) -> bool:
        """A specific figure somebody actually entered for this site."""
        return self.feet is not None and not self.is_default


def default_lengths(stated: Iterable[Optional[int]]) -> set:
    """Which stated lengths in a park look like form defaults, not measurements.

    Returns the set of values that repeat too often to be real. Empty when the
    sample is too small to judge — an unknown default is better than a guessed
    one.
    """
    values = [f for f in stated if f is not None]
    if len(values) < MIN_SAMPLE:
        return set()
    counts = Counter(values)
    return {
        value for value, n in counts.items()
        if n / len(values) >= DEFAULT_SHARE
    }


def read_length(
    feet: Optional[int],
    has_driveway: bool,
    defaults: Optional[set] = None,
) -> LengthReading:
    return LengthReading(
        feet=feet,
        is_default=bool(defaults and feet in defaults),
        has_driveway=has_driveway,
    )


def fits(reading: LengthReading, length_needed: int):
    """Three-state: can a rig of `length_needed` feet use this site?

    * no driveway            -> False   (nothing drives in)
    * stated >= needed       -> True    (the floor already clears it)
    * specific, below needed -> False   (somebody measured; believe them)
    * default, below needed  -> None    (may be far longer, as A01 is)
    * no figure at all       -> None
    """
    if not reading.has_driveway:
        return DOES_NOT_FIT
    if reading.feet is None:
        return UNKNOWN
    if reading.feet >= length_needed:
        return FITS
    return DOES_NOT_FIT if reading.trustworthy else UNKNOWN


def describe(reading: LengthReading, length_needed: Optional[int] = None) -> str:
    """Plain language for the interface. Never states more than we know.

    Deliberately does NOT dress a default figure up as encouragement. Showing
    "minimum 20 ft — may be longer!" to someone with a 40-foot rig is
    misleading: the figure is evidence of nothing, and most sites carrying it
    really will be too small. Say that the length is unrecorded and leave the
    judgement to the person, who can phone the park.
    """
    if not reading.has_driveway:
        return "No driveway length given — may not be reachable by vehicle"
    if reading.feet is None:
        return "Driveway length not stated"
    if reading.is_default:
        if length_needed is not None and reading.feet < length_needed:
            return (
                f"Length not reliably recorded — this park entered {reading.feet} ft "
                f"for most of its sites, so it could be anything. Phone the park "
                f"if you need {length_needed} ft."
            )
        return (
            f"Listed at {reading.feet} ft, though this park entered the same "
            f"figure for most of its sites"
        )
    return f"Listed at {reading.feet} ft"


def filter_by_length(sites: list, length_needed: int, getter=None):
    """Split sites into THREE buckets: (fits, unknown, does_not_fit).

    Three, not two, on purpose. Merging "fits" with "we have no idea" would
    drop 21 sites listed at a default 20 ft into a 40-foot rig's results as if
    they were candidates — misleading, and mostly wrong, even though one of
    them really is 53 feet.

    So the unknowns are kept (§8g: unknown is shown, never hidden) but kept
    SEPARATE, for an interface to present as "21 more sites where the length
    isn't reliably recorded" rather than as matches.
    """
    getter = getter or (lambda s: (s.get("equipment_length") or ""))
    from .providers.reserveamerica import parse_driveway

    parsed = []
    for site in sites:
        raw = getter(site)
        feet, _manoeuvre = parse_driveway(raw)
        parsed.append((site, feet, bool((raw or "").strip())))

    defaults = default_lengths(f for _s, f, _d in parsed)
    buckets: dict = {FITS: [], UNKNOWN: [], DOES_NOT_FIT: []}
    for site, feet, has_driveway in parsed:
        reading = read_length(feet, has_driveway, defaults)
        buckets[fits(reading, length_needed)].append(site)
    return buckets[FITS], buckets[UNKNOWN], buckets[DOES_NOT_FIT]
