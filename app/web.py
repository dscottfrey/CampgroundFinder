"""Web server + JSON API.

Built on the standard library's `http.server` rather than FastAPI, because
nothing extra can be installed here yet. The routes and JSON shapes match what
a FastAPI version would serve, so swapping the server out later touches only
this file.

Run:  python3 scripts/manage.py serve
"""

from __future__ import annotations

import json
import logging
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as config_mod
from . import db, equipment, store
from .util import iso

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_state(conn, states=None, cfg=None) -> dict:
    """Everything the page needs, in one payload."""
    pins = store.map_view(conn, states=states)
    # Burn bans and closures, attached to the campgrounds they belong to.
    # One query for the map rather than one per pin.
    alerts = store.alerts_by_campground(conn)
    for pin in pins:
        pin["alerts"] = alerts.get((pin["provider"], pin["id"]), [])
    counts: dict[str, int] = {}
    for pin in pins:
        counts[pin["status"]] = counts.get(pin["status"], 0) + 1
    row = conn.execute(
        "SELECT MAX(last_checked) AS t FROM campgrounds"
    ).fetchone()
    all_states = sorted({p["state"] for p in pins if p["state"]})
    cfg = cfg if cfg is not None else config_mod.Config()
    # Counted here rather than in the browser so the number in "N not on the
    # map" comes from the same pass that built the pins, and can't drift from
    # them (§8k: never quietly drop a campground we can't place).
    unlocated = sum(1 for p in pins if not p["located"])
    return {
        "campgrounds": pins,
        "counts": counts,
        "total": len(pins),
        "unlocated": unlocated,
        "map": cfg.map_settings,
        "states": all_states,
        "last_checked": row["t"] if row else None,
        # What the scanner is doing right now, in plain language. Shipped with
        # every payload so a slow moment always has an explanation attached
        # rather than needing a second request to find one.
        "scan": store.get_scan_status(conn).as_dict(),
        "generated": iso(),
    }


#: Fields an opening carries to a client. Deliberately explicit rather than
#: `SELECT *` passed through: this payload is the contract a second front-end
#: would be written against (a native iOS client is on the table — see
#: docs/campsage-ui-notes.md), and a contract made of whatever columns happen
#: to exist is not a contract.
_OPENING_FIELDS = (
    "provider", "campsite_id", "available_date", "nights", "site_name",
    "loop", "campsite_type", "status", "reservation_type",
    "facility_name", "facility_id", "rec_area", "state",
    "latitude", "longitude", "booking_url", "last_seen",
)


def build_openings(
    conn,
    states=None,
    nights=None,
    on_or_after=None,
    length_needed=None,
    access=None,
) -> dict:
    """Openings, each labelled with how it answers the filters — not filtered.

    **Nothing is dropped for failing a filter.** Every opening comes back with
    a verdict attached, and the client dims rather than hides — Scott's rule
    from 2026-07-31, and the same rule as `unknown` and `stale` everywhere
    else. A filter that removes rows here would make the map lie about how
    much exists, and no client could recover what it never received.

    Verdicts, per opening:

    * `length_verdict` — `fits` | `unknown` | `does_not_fit`. `unknown` covers
      both "the number is a form default we don't trust" and "we hold no
      per-site row at all", which is currently every Washington opening.
    * `access_match` — `true` | `false` | `null`, where null means the source
      never stated the access mode. Null is not `false`.

    `coverage` reports, per provider, how many openings we hold site detail
    for. That is what stops "this filter works in Oregon and not Washington"
    from being something a user discovers by noticing odd results.
    """
    rows = store.list_openings(
        conn, states=states, on_or_after=on_or_after, nights=nights)

    verdicts = {}
    if length_needed:
        fits, unknown, no = equipment.filter_openings_by_length(rows, length_needed)
        for bucket, name in ((fits, "fits"), (unknown, "unknown"),
                             (no, "does_not_fit")):
            for opening in bucket:
                verdicts[_row_key(opening)] = name

    out = []
    for row in rows:
        record = {field: row.get(field) for field in _OPENING_FIELDS}
        # Prefer the catalog's name and point over the availability row's,
        # which both real providers leave null. COALESCE in Python so the
        # payload never carries a blank where the catalog knows the answer.
        record["facility_name"] = (
            row.get("facility_name") or row.get("campground_name"))
        record["latitude"] = row.get("latitude") or row.get("campground_latitude")
        record["longitude"] = row.get("longitude") or row.get("campground_longitude")
        record["water_nearby"] = row.get("campground_water")
        record["site"] = {
            "known": row.get("site_joined") is not None,
            "site_type": row.get("site_site_type"),
            "access_class": row.get("site_access_class"),
            "driveway_entry": row.get("site_driveway_entry"),
            "max_vehicle_length": row.get("site_max_vehicle_length"),
            "max_people": row.get("site_max_people"),
            "attributes": row.get("site_attributes"),
        }
        if length_needed:
            record["length_verdict"] = verdicts.get(_row_key(row), "unknown")
        if access:
            stated = row.get("site_access_class")
            record["access_match"] = None if stated is None else stated == access
        out.append(record)

    return {
        "openings": out,
        "total": len(out),
        # Echoed back so a client can show what it asked for, and so a stale
        # or misread query is visible in the response rather than inferred.
        "filters": {
            "states": list(states or []), "nights": nights,
            "on_or_after": on_or_after, "length_needed": length_needed,
            "access": access,
        },
        "coverage": store.opening_site_coverage(conn),
        "scan": store.get_scan_status(conn).as_dict(),
        "generated": iso(),
    }


def _row_key(row) -> tuple:
    return (row.get("provider"), row.get("campsite_id"),
            row.get("available_date"), row.get("nights"))


class Handler(BaseHTTPRequestHandler):
    db_path = None
    cfg = None

    def log_message(self, fmt, *args):        # quieter than the default
        log.debug(fmt, *args)

    def _send(self, code, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here may be cached. We sent no cache headers at all until
        # 2026-07-29, which left Safari to guess — and it held on to an old
        # index.html, so a page with new markup in it came back without the
        # markup and looked like a bug in the feature. Availability is the
        # same argument for the API: a cached answer about what is open is a
        # wrong answer about what is open.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route.startswith("/static/"):
            return self._static(route[len("/static/"):])

        if route.startswith("/api/"):
            conn = db.open_db(self.db_path)
            try:
                return self._api(route, query, conn)
            finally:
                conn.close()

        return self._send(404, b"not found", "text/plain")

    def _api(self, route, query, conn):
        states = query.get("state")
        if route == "/api/state":
            return self._json(build_state(conn, states, self.cfg))
        if route == "/api/openings":
            def one(name, cast=str):
                raw = (query.get(name) or [None])[0]
                if raw in (None, ""):
                    return None
                try:
                    return cast(raw)
                except (TypeError, ValueError):
                    return None
            return self._json(build_openings(
                conn, states=states,
                nights=one("nights", int),
                on_or_after=one("from"),
                length_needed=one("length", int),
                access=one("access"),
            ))
        if route == "/api/search":
            q = (query.get("q") or [""])[0]
            results = store.search_campgrounds(conn, q, states=states)
            return self._json([
                {
                    "provider": c.provider, "id": c.id, "name": c.name,
                    "state": c.state, "status": c.status,
                    "status_reason": c.status_reason, "located": c.has_location,
                }
                for c in results
            ])
        return self._json({"error": "unknown endpoint"}, 404)

    def _static(self, name):
        path = (STATIC_DIR / name).resolve()
        if not path.is_file() or STATIC_DIR.resolve() not in path.parents:
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return self._send(200, path.read_bytes(), ctype)


def serve(db_path=None, host="127.0.0.1", port=8080, cfg=None):
    Handler.db_path = db_path
    # Read once at startup, not per request: the tile URL may carry an API key,
    # and re-reading config on a hot path is a good way to leak file handles.
    Handler.cfg = cfg if cfg is not None else config_mod.load_config()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"CampgroundFinder running at http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
