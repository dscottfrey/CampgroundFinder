#!/usr/bin/env python3
"""CLI entry point: catalog-refresh, scan-once, search, map, list-providers."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import catalog, coordinates, db, inventory, store  # noqa: E402
from app.config import load_config  # noqa: E402
from app.notifier import Notifier  # noqa: E402
from app.providers import known_providers  # noqa: E402
from app.scanner import scan_once  # noqa: E402


def cmd_backfill_coordinates(args) -> int:
    """Locate campgrounds whose own provider doesn't publish coordinates.

    A maintenance job, not part of a scan: coordinates change approximately
    never. Safe to re-run — it only ever fills in blanks (§8k).
    """
    conn = db.open_db(args.db)
    if args.provider != "GoingToCamp:BC":
        print(f"no coordinate source configured for {args.provider}; "
              f"only GoingToCamp:BC is implemented (see docs/bc-coordinates.md)")
        return 2

    # The bcparks.ca URLs live in the GoingToCamp directory, so ask it once.
    from app.providers import build_provider
    provider = build_provider(args.provider)
    payload = provider._fetch("/api/resourceLocation", {})
    websites = {}
    for entry in payload:
        location_id = entry.get("resourceLocationId")
        if location_id is None:
            continue
        for localized in entry.get("localizedValues") or []:
            if localized.get("website"):
                websites[str(location_id)] = localized["website"]
                break

    report = coordinates.backfill_bc(conn, args.provider, websites=websites)
    print(report.summary())
    for name in report.unmatched:
        print(f"  still unlocated: {name}")
    print("\ncoordinate provenance across the whole catalog:")
    for source, count in sorted(store.coordinate_provenance(conn).items()):
        print(f"  {count:>4}  {source}")
    if args.write_seed:
        path = catalog.write_seed(store.list_campgrounds(conn))
        print(f"\nwrote {path}")
    return 0


def cmd_backfill_site_inventory(args) -> int:
    """Count each campground's bookable vs not-bookable sites — once.

    Site inventory changes on the order of years, so this is measured once and
    committed to the seed, never re-queried on a schedule. Re-running only
    fills blanks.
    """
    conn = db.open_db(args.db)
    report = inventory.backfill_site_inventory(
        conn, provider=args.provider, states=args.state, limit=args.limit)
    print(report.summary())
    if report.no_inventory:
        print(f"\n{len(report.no_inventory)} facilities have no site list in the "
              f"provider's data — left unknown, not recorded as zero:")
        for name in report.no_inventory[:10]:
            print(f"  {name}")
        if len(report.no_inventory) > 10:
            print(f"  ... and {len(report.no_inventory) - 10} more")
    mixed = [c for c in store.list_campgrounds(conn, provider=args.provider)
             if c.first_come_sites]
    if mixed:
        print(f"\n{len(mixed)} campgrounds have sites that aren't bookable online:")
        for c in sorted(mixed, key=lambda c: -(c.sites_not_bookable or 0))[:10]:
            print(f"  {c.sites_not_bookable:>4} of {c.sites_total:>4}  {c.name}")
    if args.write_seed:
        path = catalog.write_seed(store.list_campgrounds(conn))
        print(f"\nwrote {path}")
    return 0


def cmd_list_providers(args) -> int:
    for name in known_providers():
        print(name)
    print("\nNot yet implemented: PerfectMind:* (§7), ReserveAmerica:* (§4d)")
    return 0


def cmd_catalog_refresh(args) -> int:
    config = load_config(args.config)
    conn = db.open_db(args.db)
    added, updated = catalog.seed_catalog(conn, path=args.seed)
    print(f"seed: added={added} updated={updated}")

    if not config.sources:
        print("no sources configured — seed-only catalog "
              "(copy config.example.yaml to config.yaml to enable live refresh)")
        return 0

    report = catalog.refresh_catalog(conn, config.sources)
    print(f"live: {report.summary()}")
    for label, err in report.provider_errors.items():
        print(f"  ! {label}: {err}")
    for cg in report.missing_from_live:
        print(f"  ~ kept (absent from live enumeration): {cg.provider}|{cg.id} {cg.name}")

    if args.write_seed:
        cgs = store.list_campgrounds(conn)
        path = catalog.write_seed(cgs, args.seed)
        print(f"wrote seed: {path} ({len(cgs)} campgrounds)")
    return 0 if report.ok else 1


def cmd_scan_once(args) -> int:
    config = load_config(args.config)
    conn = db.open_db(args.db)
    notifier = Notifier(config.default_notify_targets)
    report = scan_once(conn, config, notifier=notifier, nights=args.nights,
                       window_days=args.window_days)
    print(f"scan: {report.summary()}")
    for label, err in report.provider_errors.items():
        print(f"  ! {label}: {err}")
    return 0 if not report.provider_errors else 1


def cmd_search(args) -> int:
    conn = db.open_db(args.db)
    results = store.search_campgrounds(conn, args.query, states=args.state)
    if not results:
        print(f"no catalogued campground matches {args.query!r}")
        return 1
    for cg in results:
        loc = (f"{cg.latitude:.4f},{cg.longitude:.4f}"
               if cg.has_location else "location unknown")
        print(f"{cg.provider}|{cg.id}  {cg.name}  [{cg.state}]  {cg.status}  ({loc})")
    return 0


def cmd_serve(args) -> int:
    from app.web import serve

    db.open_db(args.db).close()          # make sure the schema exists first
    serve(db_path=args.db, host=args.host, port=args.port)
    return 0


def cmd_demo(args) -> int:
    """Populate a database with the offline mock data and serve it."""
    from app.config import parse_config
    from app.providers.mock import MockProvider
    from app.notifier import Notifier
    from app import catalog
    from app.scanner import scan_once

    conn = db.open_db(args.db)
    catalog.seed_catalog(conn, path=args.seed)
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
    conn.close()
    serve_args = argparse.Namespace(db=args.db, host=args.host, port=args.port)
    return cmd_serve(serve_args)


def cmd_add_watch(args) -> int:
    from datetime import date as _date

    conn = db.open_db(args.db)
    watch = store.Watch(
        name=args.name,
        provider=args.provider,
        mode=args.mode,
        campground_ids=args.campground or [],
        campsite_ids=args.campsite or [],
        rec_area_ids=args.rec_area or [],
        start_date=_date.fromisoformat(args.start) if args.start else None,
        end_date=_date.fromisoformat(args.end) if args.end else None,
        nights=args.nights,
        weekends_only=args.weekends,
        filters={"max_miles": args.max_miles} if args.max_miles else {},
        notify_targets=args.notify or [],
    )
    watch_id = store.add_watch(conn, watch)
    print(f"added watch {watch_id}: {watch.name} ({watch.mode})")
    return 0


def cmd_list_watches(args) -> int:
    conn = db.open_db(args.db)
    watches = store.list_watches(conn, active_only=not args.all)
    if not watches:
        print("no watches")
        return 0
    for w in watches:
        scope = w.campground_ids or w.campsite_ids or w.rec_area_ids or "any"
        print(f"[{w.id}] {w.name}  mode={w.mode}  provider={w.provider}  "
              f"scope={scope}  active={w.active}")
    return 0


def cmd_map(args) -> int:
    conn = db.open_db(args.db)
    print(json.dumps(store.map_view(conn, states=args.state), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="manage.py")
    parser.add_argument("--db", default=None, help="sqlite path (default data/campgroundfinder.db)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-providers")
    p.set_defaults(func=cmd_list_providers)

    p = sub.add_parser("catalog-refresh")
    p.add_argument("--seed", default=None)
    p.add_argument("--write-seed", action="store_true",
                   help="serialize the resulting catalog back to the seed file")
    p.set_defaults(func=cmd_catalog_refresh)

    p = sub.add_parser("backfill-coordinates",
                       help="fill in coordinates a provider doesn't publish")
    p.add_argument("--provider", default="GoingToCamp:BC")
    p.add_argument("--write-seed", action="store_true")
    p.set_defaults(func=cmd_backfill_coordinates)

    p = sub.add_parser("backfill-site-inventory",
                       help="count bookable vs not-bookable sites, once")
    p.add_argument("--provider", default="RecreationDotGov")
    p.add_argument("--state", action="append")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--write-seed", action="store_true")
    p.set_defaults(func=cmd_backfill_site_inventory)

    p = sub.add_parser("scan-once")
    p.add_argument("--nights", type=int, default=1)
    p.add_argument("--window-days", type=int, default=None)
    p.set_defaults(func=cmd_scan_once)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--state", action="append")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("serve", help="run the web page")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("demo", help="load offline sample data, then serve it")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--seed", default=None)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("add-watch")
    p.add_argument("name")
    p.add_argument("--provider")
    p.add_argument("--mode", choices=["targeted", "autonomous"], default="targeted")
    p.add_argument("--campground", action="append")
    p.add_argument("--campsite", action="append")
    p.add_argument("--rec-area", action="append")
    p.add_argument("--start", help="YYYY-MM-DD")
    p.add_argument("--end", help="YYYY-MM-DD")
    p.add_argument("--nights", type=int, default=1)
    p.add_argument("--weekends", action="store_true")
    p.add_argument("--max-miles", type=float)
    p.add_argument("--notify", action="append", help="Apprise URL (repeatable)")
    p.set_defaults(func=cmd_add_watch)

    p = sub.add_parser("list-watches")
    p.add_argument("--all", action="store_true", help="include paused watches")
    p.set_defaults(func=cmd_list_watches)

    p = sub.add_parser("map")
    p.add_argument("--state", action="append")
    p.set_defaults(func=cmd_map)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
