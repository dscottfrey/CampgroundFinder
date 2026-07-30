"""Config loading (§8c).

YAML is the documented format and needs PyYAML (in requirements.txt). JSON is
also accepted so the config layer is exercisable on a bare stdlib install.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG_PATH = Path("config.yaml")

#: The default basemap. **Not** plain OSM road tiles: the build plan (§8h) rules
#: them out because they show no trail detail, which is most of the point of
#: looking at a campground on a map. OpenTopoMap is topographic, free, and
#: OSM-derived. Its tile server is a volunteer service — fine for a household,
#: so the app must never scrape it in bulk or pre-cache regions.
DEFAULT_TILES = {
    "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    "attribution": (
        "Map data © OpenStreetMap contributors, SRTM — "
        "style © OpenTopoMap (CC-BY-SA)"
    ),
    "subdomains": "abc",
    # OpenTopoMap stops rendering above 17; asking for 18 returns blank tiles
    # rather than an error, which would read as "nothing is there".
    "max_zoom": 17,
}


@dataclass
class Source:
    label: str
    provider: str
    state: Optional[str] = None
    rec_area_ids: list[str] = field(default_factory=list)
    campground_ids: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    home_base: dict = field(default_factory=dict)
    scan_interval_minutes: int = 30
    #: Extra pause between round-robin rounds, on top of the per-host spacing
    #: the rate limiter enforces (docs/scanning-design.md). Nobody is waiting
    #: on the background sweep, so this costs nothing that matters.
    round_pause_seconds: float = 5.0
    default_window_days: int = 60
    default_states: list[str] = field(default_factory=lambda: ["OR", "WA"])
    notify: dict = field(default_factory=dict)
    access: dict = field(default_factory=dict)
    nights_options: list[int] = field(default_factory=lambda: [2, 3, 4])
    scan_regions: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    watches: list[dict] = field(default_factory=list)
    #: Basemap settings. Deliberately a swappable config value (§8h) — the tile
    #: source is not a code dependency, and changing it must never mean editing
    #: JavaScript.
    map: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def home_point(self) -> Optional[tuple[float, float]]:
        lat = self.home_base.get("latitude")
        lon = self.home_base.get("longitude")
        if lat is None or lon is None:
            return None
        return (lat, lon)

    @property
    def default_notify_targets(self) -> list[str]:
        return list(self.notify.get("default_targets") or [])

    @property
    def map_settings(self) -> dict:
        """What the browser needs to draw the basemap.

        Merged over `DEFAULT_TILES` key by key, so a config that overrides only
        `url` still gets a sane `max_zoom` rather than silently losing one.
        """
        tiles = dict(DEFAULT_TILES)
        tiles.update(self.map.get("tiles") or {})
        center = _as_point(self.map.get("center")) or self.home_point
        return {
            "tiles": tiles,
            "center": list(center) if center else None,
            "zoom": self.map.get("zoom", 7),
        }

    def sources_for(self, states: Optional[list[str]] = None) -> list[Source]:
        if not states:
            return list(self.sources)
        return [s for s in self.sources if s.state in states]


def _read_structured(path: Path) -> dict:
    text = path.read_text()
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise ImportError(
                f"reading {path} needs PyYAML — `pip install -r requirements.txt` "
                f"(or use a .json config)"
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text) or {}


def _as_point(value: Any) -> Optional[tuple[float, float]]:
    """Accept either `[lat, lon]` or `{latitude:, longitude:}`.

    Returns None for anything incomplete rather than a half-point — a map
    centred on (0, lon) is in the Gulf of Guinea, not a visible mistake.
    """
    if not value:
        return None
    if isinstance(value, dict):
        lat, lon = value.get("latitude"), value.get("longitude")
    else:
        try:
            lat, lon = value
        except (TypeError, ValueError):
            return None
    if lat is None or lon is None:
        return None
    return (float(lat), float(lon))


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (str, int)):
        return [str(value)]
    return [str(v) for v in value]


def parse_config(data: dict) -> Config:
    sources = []
    for entry in data.get("sources") or []:
        known = {"label", "provider", "state", "rec_area_ids", "campground_ids"}
        sources.append(
            Source(
                label=entry.get("label", entry.get("provider", "unnamed")),
                provider=entry["provider"],
                state=entry.get("state"),
                rec_area_ids=_as_str_list(entry.get("rec_area_ids")),
                campground_ids=_as_str_list(entry.get("campground_ids")),
                options={k: v for k, v in entry.items() if k not in known},
            )
        )
    return Config(
        home_base=data.get("home_base") or {},
        scan_interval_minutes=int(data.get("scan_interval_minutes", 30)),
        round_pause_seconds=float(data.get("round_pause_seconds", 5.0)),
        default_window_days=int(data.get("default_window_days", 60)),
        default_states=_as_str_list(data.get("default_states")) or ["OR", "WA"],
        nights_options=[int(n) for n in (data.get("nights_options") or [2, 3, 4])],
        # Falls back to default_states so an unset value never means "scan
        # everywhere" — widening scan scope should always be deliberate.
        scan_regions=(
            _as_str_list(data.get("scan_regions"))
            or _as_str_list(data.get("default_states"))
            or ["OR", "WA"]
        ),
        notify=data.get("notify") or {},
        access=data.get("access") or {},
        sources=sources,
        watches=data.get("watches") or [],
        map=data.get("map") or {},
        raw=data,
    )


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        # An absent config is not fatal — the app still runs on the seed
        # catalog with defaults, which keeps `manage.py` usable pre-setup.
        return Config()
    return parse_config(_read_structured(path))
