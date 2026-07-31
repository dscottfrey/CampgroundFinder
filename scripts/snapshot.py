#!/usr/bin/env python3
"""Write the page to one self-contained HTML file that opens with no server.

Why this exists: the sandbox this project is developed in refuses to bind a
listening socket at all — `socket.bind` returns `Operation not permitted` for
every port, including from the `!` prompt. So `manage.py serve` and
`manage.py demo` cannot be run here, and the map was committed without anyone
ever having seen it draw.

This inlines the real assets — `app/static/index.html`, `styles.css`, `app.js`
and the vendored Leaflet — around a frozen `/api/state` payload built by
`app.web.build_state`, so what renders is the actual page against actual data.
`app.js` is embedded byte for byte; a tiny shim answers its one `fetch` from
the frozen payload instead of the network.

What it is NOT: a substitute for running the server. It exercises no routes, no
static-file handling and no live scan, and its data is a snapshot from the
moment it was written. Use it to look at the page; use the real server to test
the server.

Run:  python3 scripts/snapshot.py            # demo data, mock availability
      python3 scripts/snapshot.py --db data/campgrounds.db --config config.yaml
      python3 scripts/snapshot.py -o ~/Desktop/campgroundfinder.html
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, web                                    # noqa: E402
from app.config import load_config, parse_config            # noqa: E402

STATIC = ROOT / "app" / "static"

# Everything index.html pulls in, in the order it pulls it in. Kept as an
# explicit list rather than a regex over the HTML: if someone adds an asset and
# forgets this file, the snapshot should fail loudly on the leftover tag rather
# than silently render a page missing a stylesheet.
CSS = ["vendor/leaflet.css", "styles.css"]
JS = ["vendor/leaflet.js", "app.js"]

# Answers app.js's fetches from frozen payloads. Anything else raises rather
# than quietly resolving, so a snapshot can never appear to have reached a
# network it has no access to.
#
# `/api/openings` is frozen too, and has to be: without it the page falls back
# to "we couldn't load what's open", which is the honest message for a real
# failure and a misleading one for a snapshot that simply forgot the endpoint.
SHIM = """
const __SNAPSHOT__ = %s;
const __OPENINGS__ = %s;
window.fetch = async function (url) {
  const u = String(url);
  if (u.indexOf("/api/openings") !== -1) {
    return { ok: true, statusText: "OK", json: async () => __OPENINGS__ };
  }
  if (u.indexOf("/api/state") !== -1) {
    return { ok: true, statusText: "OK", json: async () => __SNAPSHOT__ };
  }
  throw new Error("this is a snapshot — there is no server behind it: " + u);
};
"""

BANNER = """
<div style="background:#4b3a12;color:#f5e6c8;padding:.5rem .9rem;font:600 13px/1.4
 system-ui,sans-serif;text-align:center">
  Snapshot — frozen data, no server. Written %s.
</div>
"""


def demo_state():
    """The same mock catalog + availability that `manage.py demo` serves."""
    from app import catalog
    from app.notifier import Notifier
    from app.scanner import scan_once

    tmp = Path(tempfile.mkdtemp(prefix="cgf-snapshot-")) / "demo.db"
    conn = db.open_db(str(tmp))
    catalog.seed_catalog(conn)
    config = parse_config({
        "default_window_days": 5,
        "sources": [
            {"label": "Mock OR", "provider": "Mock", "state": "OR"},
            {"label": "Mock WA", "provider": "Mock", "state": "WA"},
        ],
    })
    catalog.refresh_catalog(conn, config.sources)
    report = scan_once(conn, config, notifier=Notifier([]), window_days=5)
    print(f"demo data ready: {report.summary()}")
    try:
        return web.build_state(conn, cfg=parse_config({})), web.build_openings(conn)
    finally:
        conn.close()


def live_state(db_path, config_path):
    conn = db.open_db(db_path)
    try:
        return (web.build_state(conn, cfg=load_config(config_path)),
                web.build_openings(conn))
    finally:
        conn.close()


def build_html(state: dict, openings: dict) -> str:
    html = (STATIC / "index.html").read_text()

    for name in CSS:
        tag = f'<link rel="stylesheet" href="/static/{name}">'
        if tag not in html:
            raise SystemExit(f"index.html no longer links {name} the way "
                             f"snapshot.py expects — update CSS in this script")
        css = (STATIC / name).read_text()
        html = html.replace(tag, f"<style>\n{css}\n</style>")

    payload = json.dumps(state, default=str)
    openings_payload = json.dumps(openings, default=str)
    for name in JS:
        tag = f'<script src="/static/{name}"></script>'
        if tag not in html:
            raise SystemExit(f"index.html no longer loads {name} the way "
                             f"snapshot.py expects — update JS in this script")
        body = (STATIC / name).read_text()
        # The shim has to be in place before app.js runs, and app.js is the
        # last script, so it rides in just ahead of it.
        if name == "app.js":
            body = (SHIM % (payload, openings_payload)) + "\n" + body
        html = html.replace(tag, f"<script>\n{body}\n</script>")

    if "/static/" in html:
        raise SystemExit("something in index.html still points at /static/ — "
                         "the snapshot would load it from disk and fail")

    return html.replace("<body>", "<body>" + (BANNER % state.get("generated", "?")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="a real database; omit for the offline demo")
    ap.add_argument("--config", help="config file, for the basemap settings")
    ap.add_argument("-o", "--out", help="where to write the HTML")
    ap.add_argument("--open", action="store_true",
                    help="open it in the default browser when it's written")
    args = ap.parse_args()

    state, openings = (
        live_state(args.db, args.config) if args.db else demo_state())

    out = Path(args.out) if args.out else (
        Path(os.environ.get("TMPDIR", "/tmp")) / "campgroundfinder-snapshot.html"
    )
    html = build_html(state, openings)
    out.write_text(html)

    counts = ", ".join(f"{n} {k}" for k, n in sorted(state["counts"].items()))
    print(f"{out}  ({len(html) / 1024:.0f} KB)")
    print(f"  {state['total']} campgrounds — {counts}")
    print(f"  {state['unlocated']} with no location, drawn nowhere but listed")

    if args.open:
        import subprocess
        subprocess.run(["open", str(out)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
