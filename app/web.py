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

from . import db, store
from .util import iso

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_state(conn, states=None) -> dict:
    """Everything the page needs, in one payload."""
    pins = store.map_view(conn, states=states)
    counts: dict[str, int] = {}
    for pin in pins:
        counts[pin["status"]] = counts.get(pin["status"], 0) + 1
    row = conn.execute(
        "SELECT MAX(last_checked) AS t FROM campgrounds"
    ).fetchone()
    all_states = sorted({p["state"] for p in pins if p["state"]})
    return {
        "campgrounds": pins,
        "counts": counts,
        "total": len(pins),
        "states": all_states,
        "last_checked": row["t"] if row else None,
        # What the scanner is doing right now, in plain language. Shipped with
        # every payload so a slow moment always has an explanation attached
        # rather than needing a second request to find one.
        "scan": store.get_scan_status(conn).as_dict(),
        "generated": iso(),
    }


class Handler(BaseHTTPRequestHandler):
    db_path = None

    def log_message(self, fmt, *args):        # quieter than the default
        log.debug(fmt, *args)

    def _send(self, code, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
            return self._json(build_state(conn, states))
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


def serve(db_path=None, host="127.0.0.1", port=8080):
    Handler.db_path = db_path
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"CampgroundFinder running at http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
