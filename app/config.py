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
    default_window_days: int = 60
    default_states: list[str] = field(default_factory=lambda: ["OR", "WA"])
    notify: dict = field(default_factory=dict)
    access: dict = field(default_factory=dict)
    sources: list[Source] = field(default_factory=list)
    watches: list[dict] = field(default_factory=list)
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
        default_window_days=int(data.get("default_window_days", 60)),
        default_states=_as_str_list(data.get("default_states")) or ["OR", "WA"],
        notify=data.get("notify") or {},
        access=data.get("access") or {},
        sources=sources,
        watches=data.get("watches") or [],
        raw=data,
    )


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        # An absent config is not fatal — the app still runs on the seed
        # catalog with defaults, which keeps `manage.py` usable pre-setup.
        return Config()
    return parse_config(_read_structured(path))
