"""Is there water at this campground? — derived, because nobody states it.

Scott asked for a "near water / lake" filter (2026-07-31). The obvious field
exists and is empty: ReserveAmerica publishes `Near Water` per site and
Oregon State Parks answered **`no` on all 5,313 sites that carry it** — zero
`yes`, including Beverly Beach and South Beach, which are on the ocean. A
filter built on that returns nothing forever.

So the fact has to be derived from things we do have. Three rules that follow
from everything else in this project:

**1. The answer is `yes` or `unknown`. Never `no`.**
Absence of evidence is not evidence of absence — a lakeside campground with a
dull name and no listed activities is unknown, not dry. Per §8g unknown is
shown and dimmed, never filtered away, so a false `no` would hide a real
answer while a `unknown` merely fails to promote it.

**2. Every `yes` carries its evidence, in words.**
`water_evidence` reads "named for Diamond Lake" or "the operator lists
BOATING, SWIMMING". A derived flag that can't say why it fired is the same
confident guess this project keeps getting punished for, and the evidence
string is what lets a wrong one be spotted and fixed.

**3. Derivation is stored beside the source data, never on top of it.**
`Near Water` stays exactly as the provider stated it. This is our column.

## The signals, strongest first

| signal | covers | why it's trusted |
|---|---|---|
| operator-listed activities | 545 federal | BOATING/SWIMMING/FISHING are stated by the agency that runs the place |
| water words in the name | all 803 | Scott's own suggestion: "any park with beach or river or lake in the name" |

A name match alone is weaker than an activity match, so the evidence string
says which fired, and both are recorded when both do.

## The human layer, and why it outranks both

Scott, 2026-07-31: *"you can make a list of the ambiguous ones and my looking
at a map and/or photos can answer the question. Also, there are not that many
campgrounds on the coast, I can do all of those manually if needed."*

So there is a third, highest-priority signal: **a person who looked.**
`data/seed/curated_water.json` is committed, hand-edited, and wins over
everything derived here. It is the same principle as painting our own
campground names on the map and holding our own photos — where the providers
are silent or wrong, we own the field.

**The curated layer is the one place `no` is legitimate.** Somebody looking at
a map and seeing no water is a measurement; our failure to find a keyword is
not. `review_list()` produces the queue to work through, ordered so the
worthwhile ones come first.

## Deliberately not done yet

* **Coastline distance from coordinates.** Correct and appealing — "obviously
  anything on the coast" — but it needs a coastline geometry we don't have and
  can't fetch inside the current network allowlist. Names carry most coastal
  parks anyway (Beach, Bay, Cove, Harbor, Shore).
* **ReserveAmerica park descriptions.** 65 pages of prose, one fetch each,
  using the session machinery `list_sites` already has. Worth doing next; it
  is the only signal that would catch an Oregon state park named for neither
  its water nor its activities.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Iterable, Optional

CURATED_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "data" / "seed" / "curated_water.json"
)

#: Activity names RIDB uses that only happen where there is water. Taken from
#: live records, not guessed: Hyatt Lake lists BOATING/FISHING/SWIMMING,
#: Diamond Lake adds CANOEING/KAYAKING, Pacific Lake has MOTOR BOAT and
#: ACCESSIBLE SWIMMING. Matched as substrings so the "ACCESSIBLE " and
#: "MOTOR " prefixes don't need enumerating.
WATER_ACTIVITY_MARKERS = (
    "BOAT",        # BOATING, MOTOR BOAT, BOAT LAUNCH
    "SWIM",        # SWIMMING, ACCESSIBLE SWIMMING
    "FISH",        # FISHING, FLY FISHING, ICE FISHING
    "CANOE",
    "KAYAK",
    "PADDL",       # PADDLING, PADDLESPORTS
    "RAFT",
    "SAIL",
    "SURF",
    "DIVING",
    "WATER SKI",
    "MARINA",
    "BEACHCOMB",
    # GoingToCamp's vocabulary, which is different and in places better:
    # "Moorage", "Lakes/Rivers/Beach", "Waterfalls". Added 2026-07-31 after
    # checking their live attribute list — a marker set tuned to one provider
    # silently drops the next one's terms.
    "MOORAGE",
    "LAKE",
    "RIVER",
    "BEACH",
    "WATERFALL",
)

#: Words in a campground or rec-area name that mean water. Scott's list, plus
#: the ones that showed up when it was run over the catalog.
#:
#: `fork`, `point` and `island` are deliberately **excluded** despite matching
#: 13, 13 and 9 campgrounds: a fork is usually a river's but sometimes a road's,
#: and points and islands are named after plenty of dry things. The cost of a
#: wrong `yes` here is a camper driving somewhere expecting a lake.
WATER_WORDS = (
    "lake", "lakes", "beach", "river", "creek", "bay", "cove", "shore",
    "shores", "pond", "reservoir", "falls", "springs", "harbor", "harbour",
    "sound", "inlet", "marina", "waterfront", "lagoon", "estuary", "slough",
    "seashore", "oceanside", "bayside", "lakeside", "riverside", "riverfront",
    "waterside", "brook", "narrows", "basin", "hot springs",
)

_WATER_WORD_RE = re.compile(r"\b(" + "|".join(WATER_WORDS) + r")\b", re.I)

WATER_YES = "yes"
WATER_NO = "no"            # curated verdicts only — see the module docstring
WATER_UNKNOWN = "unknown"


def load_curated(path: Optional[pathlib.Path] = None) -> dict:
    """Hand-checked verdicts, keyed "provider|id". Missing file is fine."""
    path = path or CURATED_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data.get("campgrounds", data)


def curated_key(provider: str, campground_id: str) -> str:
    return f"{provider}|{campground_id}"


def water_words_in(*texts: Optional[str]) -> list[str]:
    """Every water word found across a campground's name and rec area."""
    found: list[str] = []
    for text in texts:
        for word in _WATER_WORD_RE.findall(text or ""):
            lowered = word.lower()
            if lowered not in found:
                found.append(lowered)
    return found


def water_activities_in(activities: Iterable[str]) -> list[str]:
    """The operator-listed activities that imply water."""
    found: list[str] = []
    for activity in activities or ():
        name = (activity or "").strip().upper()
        if any(marker in name for marker in WATER_ACTIVITY_MARKERS):
            if name not in found:
                found.append(name)
    return found


def derive(
    name: Optional[str],
    rec_area: Optional[str] = None,
    activities: Optional[Iterable[str]] = None,
    curated: Optional[dict] = None,
) -> tuple[str, Optional[str]]:
    """`(status, evidence)` — `yes`/`no` with a reason, or `unknown` with none.

    Only a curated entry can produce `no`: see the module docstring. The
    evidence reads as a sentence fragment so the interface can print it
    verbatim under the campground — "the operator lists Boating, Swimming".
    """
    if curated:
        verdict = (curated.get("water") or "").strip().lower()
        if verdict in (WATER_YES, WATER_NO):
            note = curated.get("note") or "checked by hand"
            return verdict, note

    acts = water_activities_in(activities or ())
    words = water_words_in(name, rec_area)
    if not acts and not words:
        return WATER_UNKNOWN, None

    parts = []
    if acts:
        parts.append("the operator lists " + ", ".join(a.title() for a in acts))
    if words:
        parts.append("named for " + ", ".join(sorted(set(words))))
    return WATER_YES, "; ".join(parts)


def rederive_all(conn, provider: Optional[str] = None, now=None) -> dict:
    """Re-run the derivation over everything already stored. No network.

    Needed for three ordinary situations, all of which would otherwise mean
    re-fetching every provider:

    * **Scott edits `curated_water.json`.** His verdicts have to take effect,
      and they are the whole point of the review queue.
    * **The word list or activity markers change.** Adding "Moorage" for
      GoingToCamp should retroactively fix every record it would have caught.
    * **A provider that was never derived at all.** ReserveAmerica's 65 parks
      sat at NULL — not `unknown`, which is a claim, but nothing — because no
      pass had ever looked at them.

    Reads only what is already in the database, so it is instant and safe to
    run as often as wanted.
    """
    from . import store

    curated = load_curated()
    counts = {"yes": 0, "no": 0, "unknown": 0, "changed": 0}
    for cg in store.list_campgrounds(conn, provider=provider):
        status, evidence = derive(
            cg.name, cg.rec_area, _activity_names(cg),
            curated.get(curated_key(cg.provider, cg.id)),
        )
        counts[status] = counts.get(status, 0) + 1
        if status != cg.water_nearby or evidence != cg.water_evidence:
            counts["changed"] += 1
            store.set_water(conn, cg.provider, cg.id, status, evidence, now=now)
    return counts


def review_list(campgrounds: Iterable, curated: Optional[dict] = None) -> list[dict]:
    """The queue of campgrounds a person needs to look at.

    Everything still `unknown` after the automatic signals, with what a human
    needs to settle it quickly: the name, where it is, and a coordinate to
    drop on a map. Ordered **largest first** — deciding a 300-site state park
    is worth more than deciding a 2-site trailhead, and Scott's time is the
    scarce input here, not the campgrounds.

    Ones with no coordinate come last, flagged: those can't be answered from a
    map at all and need the operator's own page instead.
    """
    curated = curated if curated is not None else load_curated()
    queue = []
    for cg in campgrounds:
        key = curated_key(cg.provider, cg.id)
        status, _ = derive(
            cg.name, getattr(cg, "rec_area", None),
            _activity_names(cg), curated.get(key),
        )
        if status != WATER_UNKNOWN:
            continue
        queue.append({
            "key": key,
            "provider": cg.provider,
            "id": cg.id,
            "name": cg.name,
            "state": getattr(cg, "state", None),
            "rec_area": getattr(cg, "rec_area", None),
            "sites_total": getattr(cg, "sites_total", None) or 0,
            "latitude": getattr(cg, "latitude", None),
            "longitude": getattr(cg, "longitude", None),
            "map_url": (
                f"https://www.openstreetmap.org/?mlat={cg.latitude}"
                f"&mlon={cg.longitude}#map=14/{cg.latitude}/{cg.longitude}"
                if getattr(cg, "latitude", None) and getattr(cg, "longitude", None)
                else None
            ),
        })
    queue.sort(key=lambda r: (r["map_url"] is None, -r["sites_total"], r["name"] or ""))
    return queue


def _activity_names(campground) -> list[str]:
    raw = getattr(campground, "activities", None)
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return list(raw or [])
