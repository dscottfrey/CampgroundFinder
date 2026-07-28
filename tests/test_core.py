"""Core offline tests — stdlib only, no network, no external deps.

Covers build-plan steps 1–3:
  step 1  mock scan → store → watch match
  step 2  camply adapter normalization (against a replayed fake camply module)
  step 3  catalog seed / live diff / never-shrink, plus the Reehers acceptance test

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import catalog, db, store  # noqa: E402
from app.config import Source, parse_config  # noqa: E402
from app.notifier import Notifier, format_alert  # noqa: E402
from app.providers import build_provider  # noqa: E402
from app.providers.base import (  # noqa: E402
    STATUS_AVAILABLE,
    STATUS_FULL,
    STATUS_STALE,
    STATUS_UNKNOWN,
    Campground,
    Campsite,
    SearchRequest,
)
from app.providers.camply_provider import CamplyProvider  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.scanner import run_watches, scan_once  # noqa: E402
from app.util import haversine_miles  # noqa: E402

START = date(2026, 8, 3)          # a Monday
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def make_db():
    return db.open_db(":memory:")


class DBTestCase(unittest.TestCase):
    """Opens an in-memory DB per test and closes it on teardown."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)


def a_site(**kw) -> Campsite:
    base = dict(
        provider="Mock",
        campsite_id="mock-site-a",
        available_date=START,
        nights=1,
        site_name="A01",
        facility_id="mock-cg-1",
        facility_name="Mock Riverside Camp",
        state="OR",
        latitude=45.30,
        longitude=-121.90,
        booking_url="https://example.invalid/book/mock-site-a",
    )
    base.update(kw)
    return Campsite(**base)


# ---------------------------------------------------------------- step 1 ----

class TestMockProviderSearch(unittest.TestCase):
    def test_search_returns_deterministic_sites(self):
        provider = MockProvider()
        req = SearchRequest(provider="Mock", start_date=START,
                            end_date=START + timedelta(days=2), nights=1)
        first = provider.search(req)
        second = provider.search(req)
        self.assertTrue(first)
        self.assertEqual([s.key for s in first], [s.key for s in second])

    def test_campground_and_campsite_filters_apply(self):
        provider = MockProvider()
        req = SearchRequest(provider="Mock", start_date=START, end_date=START,
                            nights=1, campground_ids=["mock-cg-1"])
        sites = provider.search(req)
        self.assertTrue(sites)
        self.assertTrue(all(s.facility_id == "mock-cg-1" for s in sites))

    def test_weekends_only_yields_friday_saturday_nights(self):
        provider = MockProvider()
        req = SearchRequest(provider="Mock", start_date=START,
                            end_date=START + timedelta(days=13), nights=1,
                            weekends_only=True)
        sites = provider.search(req)
        self.assertTrue(sites)
        self.assertTrue(all(s.available_date.weekday() in (4, 5) for s in sites))

    def test_campsite_key_is_stable_and_distinct(self):
        one = a_site()
        same = a_site()
        other = a_site(available_date=START + timedelta(days=1))
        self.assertEqual(one.key, same.key)
        self.assertNotEqual(one.key, other.key)


class TestStoreAvailability(DBTestCase):
    def test_upsert_returns_only_newly_appeared(self):
        first = store.upsert_availability(self.conn, [a_site()], now=NOW)
        self.assertEqual(len(first), 1)
        # Same key again: refreshed, not new — this is what stops re-alerting.
        again = store.upsert_availability(self.conn, [a_site()], now=NOW + timedelta(minutes=30))
        self.assertEqual(again, [])
        count = self.conn.execute("SELECT COUNT(*) n FROM availability").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_round_trip_preserves_fields(self):
        store.upsert_availability(
            self.conn,
            [a_site(loop="River Loop", campsite_type="TENT ONLY",
                    attributes={"max_vehicle_length": 30},
                    extra={"permitted_equipment": ["Tent"]})],
            now=NOW,
        )
        got = store.list_availability(self.conn)[0]
        self.assertEqual(got.loop, "River Loop")
        self.assertEqual(got.campsite_type, "TENT ONLY")
        self.assertEqual(got.attributes["max_vehicle_length"], 30)
        self.assertEqual(got.extra["permitted_equipment"], ["Tent"])
        self.assertEqual(got.available_date, START)

    def test_prune_removes_only_stale_rows(self):
        store.upsert_availability(self.conn, [a_site()], now=NOW - timedelta(hours=2))
        store.upsert_availability(self.conn, [a_site(campsite_id="fresh")], now=NOW)
        pruned = store.prune_availability(self.conn, older_than=NOW)
        self.assertEqual(pruned, 1)
        remaining = store.list_availability(self.conn)
        self.assertEqual([s.campsite_id for s in remaining], ["fresh"])


class TestWatchMatching(DBTestCase):
    def _watch(self, **kw):
        base = dict(name="w", provider="Mock", campground_ids=["mock-cg-1"],
                    start_date=START, end_date=START + timedelta(days=7), nights=1)
        base.update(kw)
        watch = store.Watch(**base)
        store.add_watch(self.conn, watch, now=NOW)
        return watch

    def test_matches_within_scope(self):
        watch = self._watch()
        matches = store.watch_matches(watch, [a_site()])
        self.assertEqual(len(matches), 1)

    def test_rejects_other_campground(self):
        watch = self._watch()
        matches = store.watch_matches(watch, [a_site(facility_id="mock-cg-2")])
        self.assertEqual(matches, [])

    def test_rejects_outside_date_window(self):
        watch = self._watch()
        late = a_site(available_date=START + timedelta(days=30))
        self.assertEqual(store.watch_matches(watch, [late]), [])

    def test_run_spanning_past_end_date_is_rejected(self):
        watch = self._watch(end_date=START + timedelta(days=1))
        spanning = a_site(nights=5)
        self.assertEqual(store.watch_matches(watch, [spanning]), [])

    def test_weekends_only_watch(self):
        watch = self._watch(weekends_only=True)
        monday = a_site(available_date=START)                       # Monday
        friday = a_site(available_date=START + timedelta(days=4))   # Friday
        self.assertEqual(store.watch_matches(watch, [monday]), [])
        self.assertEqual(len(store.watch_matches(watch, [friday])), 1)

    def test_distance_filter_uses_home_base(self):
        watch = self._watch(campground_ids=[], filters={"max_miles": 60})
        near = a_site(latitude=45.50, longitude=-122.60)
        far = a_site(campsite_id="far", latitude=42.00, longitude=-120.00)
        home = (45.52, -122.68)
        matched = store.watch_matches(watch, [near, far], home_base=home)
        self.assertEqual([s.campsite_id for s in matched], ["mock-site-a"])

    def test_unlocated_site_is_skipped_by_distance_watch_but_not_others(self):
        unlocated = a_site(campsite_id="nowhere", latitude=None, longitude=None)
        home = (45.52, -122.68)
        distance_watch = self._watch(campground_ids=[], filters={"max_miles": 60})
        self.assertEqual(store.watch_matches(distance_watch, [unlocated], home_base=home), [])
        plain_watch = self._watch(campground_ids=[])
        self.assertEqual(len(store.watch_matches(plain_watch, [unlocated], home_base=home)), 1)

    def test_campsite_type_filter(self):
        watch = self._watch(filters={"campsite_type_any": ["TENT ONLY"]})
        tent = a_site(campsite_type="TENT ONLY")
        rv = a_site(campsite_id="rv", campsite_type="STANDARD ELECTRIC")
        matched = store.watch_matches(watch, [tent, rv])
        self.assertEqual([s.campsite_id for s in matched], ["mock-site-a"])


class TestNotificationDedupe(DBTestCase):
    def setUp(self):
        super().setUp()
        self.watch = store.Watch(name="w", provider="Mock")
        store.add_watch(self.conn, self.watch, now=NOW)

    def test_cooldown_suppresses_then_expires(self):
        site = a_site()
        self.assertFalse(store.already_notified(self.conn, self.watch.id, site.key, now=NOW))
        store.record_notification(self.conn, self.watch.id, site.key, now=NOW)
        self.assertTrue(
            store.already_notified(self.conn, self.watch.id, site.key, 8, now=NOW + timedelta(hours=1))
        )
        self.assertFalse(
            store.already_notified(self.conn, self.watch.id, site.key, 8, now=NOW + timedelta(hours=9))
        )

    def test_pending_filters_already_sent(self):
        sites = [a_site(), a_site(campsite_id="b")]
        store.record_notification(self.conn, self.watch.id, sites[0].key, now=NOW)
        pending = store.pending_notifications(self.conn, self.watch, sites, now=NOW)
        self.assertEqual([s.campsite_id for s in pending], ["b"])


class TestEndToEndMockScan(DBTestCase):
    """The step-1 acceptance path: mock scan → store → watch match → alert."""

    def setUp(self):
        super().setUp()
        self.config = parse_config({
            "home_base": {"latitude": 45.52, "longitude": -122.68},
            "default_window_days": 5,
            "notify": {"default_targets": ["mock://target"]},
            "sources": [{"label": "Mock OR", "provider": "Mock", "state": "OR"}],
        })
        catalog.seed_catalog(self.conn, seed=MockProvider().list_campgrounds(), now=NOW)
        store.add_watch(
            self.conn,
            store.Watch(name="mock watch", provider="Mock",
                        campground_ids=["mock-cg-1"], nights=1),
            now=NOW,
        )

    def test_scan_stores_matches_and_alerts_once(self):
        notifier = Notifier(["mock://target"])
        report = scan_once(self.conn, self.config, notifier=notifier,
                           start=START, window_days=3, now=NOW)
        self.assertGreater(report.found, 0)
        self.assertGreater(report.newly_available, 0)
        self.assertGreater(report.alerts_sent, 0)
        self.assertEqual(report.provider_errors, {})

        # Second identical cycle: nothing newly available, so no repeat alerts.
        before = len(notifier.sent)
        second = scan_once(self.conn, self.config, notifier=notifier,
                           start=START, window_days=3, now=NOW + timedelta(minutes=30))
        self.assertEqual(second.newly_available, 0)
        self.assertEqual(second.alerts_sent, 0)
        self.assertEqual(len(notifier.sent), before)

    def test_many_openings_collapse_into_one_notification(self):
        """A cycle with many matches sends one digest, not one buzz per site."""
        notifier = Notifier(["mock://target"])
        report = scan_once(self.conn, self.config, notifier=notifier,
                           start=START, window_days=3, now=NOW)
        # The watch matches several site-nights across the window...
        self.assertGreater(report.found, 3)
        # ...but that is a single dispatched message.
        self.assertEqual(report.alerts_sent, 1)
        self.assertEqual(len(notifier.sent), 1)
        body = notifier.sent[0][1]
        self.assertIn("new", body)
        # Every booking link still travels in the digest.
        self.assertGreaterEqual(body.count("https://example.invalid/book/"), 4)

    def test_single_opening_sends_a_plain_alert(self):
        notifier = Notifier(["mock://target"])
        store.add_watch(
            self.conn,
            store.Watch(name="one site", provider="Mock",
                        campsite_ids=["mock-site-c"], nights=1),
            now=NOW,
        )
        scan_once(self.conn, self.config, notifier=notifier,
                  start=START, window_days=0, now=NOW)
        singles = [m for _t, m in notifier.sent if "new\n" not in m]
        self.assertTrue(singles)
        self.assertIn("site C12", singles[0])

    def test_scan_stamps_catalog_status(self):
        scan_once(self.conn, self.config, notifier=Notifier([]),
                  start=START, window_days=3, now=NOW)
        cg1 = store.get_campground(self.conn, "Mock", "mock-cg-1")
        self.assertEqual(cg1.status, STATUS_AVAILABLE)

    def test_provider_failure_degrades_to_stale_not_empty(self):
        class Boom(MockProvider):
            def search(self, req):
                raise RuntimeError("upstream 503")

        report = scan_once(self.conn, self.config, notifier=Notifier([]),
                           start=START, window_days=3, now=NOW,
                           provider_factory=lambda spec, state=None, **kw: Boom(state=state))
        self.assertIn("Mock OR", report.provider_errors)
        # The map must still have every pin, marked stale — never emptied.
        pins = store.map_view(self.conn)
        self.assertEqual(len(pins), 3)
        self.assertTrue(all(p["status"] == STATUS_STALE for p in pins))


# ---------------------------------------------------------------- step 2 ----

class FakeEquipment:
    def __init__(self, name):
        self.equipment_name = name


class FakeLocation:
    latitude = 45.3311
    longitude = -121.7113


class FakeAvailableCampsite:
    """Mirrors camply's AvailableCampsite fields (verified in samples/camply-main)."""

    campsite_id = 12345
    booking_date = datetime(2026, 8, 3, 0, 0)
    booking_end_date = datetime(2026, 8, 5, 0, 0)
    booking_nights = 2
    campsite_site_name = "B012"
    campsite_loop_name = "LOOP B"
    campsite_type = "STANDARD NONELECTRIC"
    campsite_occupancy = (1, 6)
    campsite_use_type = "Overnight"
    availability_status = "Available"
    recreation_area = "Mount Hood National Forest"
    recreation_area_id = 1106
    facility_name = "Trillium Lake"
    facility_id = 232876
    booking_url = "https://www.recreation.gov/camping/campsites/12345"
    location = FakeLocation()
    permitted_equipment = [FakeEquipment("Tent"), FakeEquipment("RV")]
    campsite_attributes = []


class FakeSearchClass:
    """Records constructor kwargs so the adapter's call shape is asserted."""

    last_kwargs: dict = {}
    provider_class = None

    def __init__(self, **kwargs):
        FakeSearchClass.last_kwargs = kwargs

    def get_matching_campsites(self, **kwargs):
        FakeSearchClass.last_call = kwargs
        return [FakeAvailableCampsite()]


class InstallFakeCamply:
    """Context manager injecting a stand-in `camply` package into sys.modules.

    camply can't be installed in this environment, so the adapter is verified
    against a replay of the real container shape (§13 fixture guidance) rather
    than left untested.
    """

    def __enter__(self):
        self._saved = {k: v for k, v in sys.modules.items() if k.startswith("camply")}
        camply = types.ModuleType("camply")
        search = types.ModuleType("camply.search")
        containers = types.ModuleType("camply.containers")

        class SearchWindow:
            def __init__(self, start_date, end_date):
                self.start_date, self.end_date = start_date, end_date

        search.CAMPSITE_SEARCH_PROVIDER = {"RecreationDotGov": FakeSearchClass}
        containers.SearchWindow = SearchWindow
        containers.AvailableCampsite = FakeAvailableCampsite
        camply.search, camply.containers = search, containers
        sys.modules.update({
            "camply": camply,
            "camply.search": search,
            "camply.containers": containers,
        })
        return self

    def __exit__(self, *exc):
        for key in [k for k in sys.modules if k.startswith("camply")]:
            del sys.modules[key]
        sys.modules.update(self._saved)
        return False


class TestCamplyAdapter(unittest.TestCase):
    def test_provider_name_normalizes_both_spellings(self):
        # camply's registry is keyed by provider name; the plan's §6/§12 say
        # search-class name. Accept either, normalize to the verified form.
        self.assertEqual(CamplyProvider("RecreationDotGov").name, "RecreationDotGov")
        self.assertEqual(CamplyProvider("SearchRecreationDotGov").name, "RecreationDotGov")

    def test_search_normalizes_available_campsite(self):
        with InstallFakeCamply():
            provider = CamplyProvider("RecreationDotGov", state="OR")
            sites = provider.search(SearchRequest(
                provider="RecreationDotGov", start_date=START,
                end_date=START + timedelta(days=7), nights=2,
                rec_area_ids=["1106"],
            ))
        self.assertEqual(len(sites), 1)
        site = sites[0]
        self.assertEqual(site.provider, "RecreationDotGov")
        self.assertEqual(site.campsite_id, "12345")
        self.assertEqual(site.available_date, date(2026, 8, 3))  # datetime → date
        self.assertEqual(site.nights, 2)
        self.assertEqual(site.facility_id, "232876")
        self.assertEqual(site.rec_area_id, "1106")
        self.assertEqual(site.state, "OR")   # stamped from config, not the payload
        self.assertAlmostEqual(site.latitude, 45.3311)
        self.assertEqual(site.extra["permitted_equipment"], ["Tent", "RV"])
        self.assertTrue(site.key.startswith("RecreationDotGov|12345|2026-08-03|2"))

    def test_search_passes_verified_kwargs(self):
        with InstallFakeCamply():
            CamplyProvider("RecreationDotGov").search(SearchRequest(
                provider="RecreationDotGov", start_date=START,
                end_date=START + timedelta(days=7), nights=2,
                rec_area_ids=["1106"], weekends_only=True,
            ))
        kwargs = FakeSearchClass.last_kwargs
        self.assertEqual(kwargs["recreation_area"], ["1106"])
        self.assertEqual(kwargs["nights"], 2)
        self.assertTrue(kwargs["weekends_only"])
        self.assertFalse(kwargs["offline_search"])
        self.assertIsNone(kwargs["campgrounds"])
        self.assertFalse(FakeSearchClass.last_call["continuous"])
        self.assertEqual(FakeSearchClass.last_call["notification_provider"], "silent")

    def test_unknown_provider_raises_with_available_list(self):
        with InstallFakeCamply():
            with self.assertRaises(KeyError) as ctx:
                CamplyProvider("NotAProvider").search(SearchRequest(
                    provider="NotAProvider", start_date=START, end_date=START))
        self.assertIn("RecreationDotGov", str(ctx.exception))

    def test_missing_camply_raises_actionable_error(self):
        from app.providers.camply_provider import CamplyNotInstalled
        saved = {k: v for k, v in sys.modules.items() if k.startswith("camply")}
        for key in list(saved):
            del sys.modules[key]
        sys.modules["camply"] = None       # forces ImportError on import
        try:
            with self.assertRaises(CamplyNotInstalled):
                CamplyProvider("RecreationDotGov").search(
                    SearchRequest(provider="RecreationDotGov", start_date=START, end_date=START))
        finally:
            del sys.modules["camply"]
            sys.modules.update(saved)

    def test_registry_builds_camply_provider(self):
        provider = build_provider("RecreationDotGov", state="OR")
        self.assertIsInstance(provider, CamplyProvider)
        self.assertEqual(provider.state, "OR")

    def test_registry_rejects_unimplemented_provider(self):
        with self.assertRaises(NotImplementedError):
            build_provider("PerfectMind:SanJuanCoWA", state="WA")


# ---------------------------------------------------------------- step 3 ----

class TestCatalogSeed(DBTestCase):
    def setUp(self):
        super().setUp()

    def test_seed_file_loads(self):
        seeded = catalog.load_seed(Path(__file__).resolve().parent.parent
                                   / "data" / "seed" / "pnw_campgrounds.json")
        self.assertTrue(seeded)
        self.assertTrue(any(c.name.startswith("Reehers") for c in seeded))

    def test_seed_is_idempotent(self):
        seed = [Campground(provider="Mock", id="x", name="X Camp", state="OR")]
        added, _ = catalog.seed_catalog(self.conn, seed=seed, now=NOW)
        self.assertEqual(added, 1)
        added2, updated2 = catalog.seed_catalog(self.conn, seed=seed, now=NOW)
        self.assertEqual((added2, updated2), (0, 1))

    def test_write_seed_round_trips(self):
        seed = [Campground(provider="Mock", id="x", name="X Camp", state="OR",
                           latitude=45.0, longitude=-122.0)]
        with tempfile.TemporaryDirectory() as tmp:
            path = catalog.write_seed(seed, Path(tmp) / "seed.json")
            reloaded = catalog.load_seed(path)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].name, "X Camp")
        self.assertEqual(reloaded[0].latitude, 45.0)


class TestCatalogRefreshNeverShrinks(DBTestCase):
    """§8k's central guarantee, tested directly."""

    def setUp(self):
        super().setUp()
        # MockProvider has 3 campgrounds, 2 of them in OR — an OR-scoped source
        # must enumerate exactly those two.
        self.sources = [Source(label="Mock OR", provider="Mock", state="OR")]

    def test_live_enumeration_adds_new_campgrounds(self):
        report = catalog.refresh_catalog(self.conn, self.sources)
        self.assertEqual(report.added, 2)
        self.assertEqual(len(store.list_campgrounds(self.conn)), 2)

    def test_enumeration_is_state_scoped(self):
        catalog.refresh_catalog(
            self.conn, [Source(label="Mock WA", provider="Mock", state="WA")]
        )
        ids = {c.id for c in store.list_campgrounds(self.conn)}
        self.assertEqual(ids, {"mock-cg-2"})

    def test_campground_dropped_by_live_query_is_kept_and_flagged(self):
        catalog.refresh_catalog(self.conn, self.sources)

        class ForgetfulMock(MockProvider):
            """Simulates the Reehers bug: a real park a live query omits."""
            def list_campgrounds(self, state=None, rec_area_ids=None):
                return [c for c in super().list_campgrounds(state, rec_area_ids)
                        if c.id != "mock-cg-1"]

        report = catalog.refresh_catalog(
            self.conn, self.sources,
            provider_factory=lambda spec, state=None, **kw: ForgetfulMock(state=state),
        )
        self.assertEqual([c.id for c in report.missing_from_live], ["mock-cg-1"])
        # Kept, not deleted.
        survivor = store.get_campground(self.conn, "Mock", "mock-cg-1")
        self.assertIsNotNone(survivor)
        self.assertIn(survivor.status, (STATUS_STALE, STATUS_UNKNOWN))
        self.assertIn("completeness floor", survivor.status_reason)
        self.assertEqual(len(store.list_campgrounds(self.conn)), 2)

    def test_enumeration_error_does_not_shrink_catalog(self):
        catalog.refresh_catalog(self.conn, self.sources)

        def boom(spec, state=None, **kw):
            raise RuntimeError("provider down")

        report = catalog.refresh_catalog(self.conn, self.sources, provider_factory=boom)
        self.assertIn("Mock OR", report.provider_errors)
        self.assertFalse(report.ok)
        # A source outage must not flag every pin as missing, and must not delete.
        self.assertEqual(report.missing_from_live, [])
        self.assertEqual(len(store.list_campgrounds(self.conn)), 2)


class TestReehersAcceptance(DBTestCase):
    """Step-3 acceptance test (§8k): findable by search AND on the map when full."""

    def setUp(self):
        super().setUp()
        seed_path = (Path(__file__).resolve().parent.parent
                     / "data" / "seed" / "pnw_campgrounds.json")
        catalog.seed_catalog(self.conn, path=seed_path, now=NOW)

    def test_reehers_is_findable_by_search(self):
        results = store.search_campgrounds(self.conn, "Reehers")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "412704")

    def test_reehers_is_on_the_map_even_when_full(self):
        store.set_campground_status(self.conn, "ReserveAmerica", "412704",
                                    STATUS_FULL, "no open sites", now=NOW)
        pins = store.map_view(self.conn)
        reehers = [p for p in pins if p["id"] == "412704"]
        self.assertEqual(len(reehers), 1)
        self.assertEqual(reehers[0]["status"], STATUS_FULL)
        self.assertEqual(reehers[0]["open_sites"], 0)

    def test_reehers_without_coordinates_is_still_listed(self):
        pin = [p for p in store.map_view(self.conn) if p["id"] == "412704"][0]
        # No authoritative lat/lon was available for the seed — the pin must
        # still exist, flagged unlocated rather than dropped (§13).
        self.assertFalse(pin["located"])
        self.assertIsNone(pin["latitude"])

    def test_search_is_state_scoped(self):
        self.assertEqual(len(store.search_campgrounds(self.conn, "Reehers", states=["OR"])), 1)
        self.assertEqual(len(store.search_campgrounds(self.conn, "Reehers", states=["WA"])), 0)


class TestConfigWatchSeeding(DBTestCase):
    """§8c: config `watches:` entries are seeds — insert-if-absent, by name."""

    ENTRIES = [
        {
            "name": "Labor Day dream spot",
            "mode": "targeted",
            "provider": "RecreationDotGov",
            "campground_ids": ["232876"],
            "start_date": "2026-09-04",
            "end_date": "2026-09-07",
            "nights": 2,
            "notify_targets": ["tgram://token/chat"],
        },
        {
            "name": "Any good OR/WA weekend within 120mi",
            "mode": "autonomous",
            "states": ["OR", "WA"],
            "weekends_only": True,
            "nights": 2,
            "filters": {"max_miles": 120, "campsite_type_any": ["TENT ONLY"]},
        },
    ]

    def test_seeds_watches_from_config(self):
        added = store.seed_watches(self.conn, self.ENTRIES, now=NOW)
        self.assertEqual(added, 2)
        watches = {w.name: w for w in store.list_watches(self.conn)}
        targeted = watches["Labor Day dream spot"]
        self.assertEqual(targeted.campground_ids, ["232876"])
        self.assertEqual(targeted.start_date, date(2026, 9, 4))
        self.assertEqual(targeted.nights, 2)

    def test_top_level_states_fold_into_filters(self):
        store.seed_watches(self.conn, self.ENTRIES, now=NOW)
        autonomous = [w for w in store.list_watches(self.conn)
                      if w.mode == "autonomous"][0]
        self.assertEqual(autonomous.filters["states"], ["OR", "WA"])
        self.assertEqual(autonomous.filters["max_miles"], 120)
        self.assertTrue(autonomous.weekends_only)

    def test_seeding_is_idempotent_and_does_not_revert_edits(self):
        store.seed_watches(self.conn, self.ENTRIES, now=NOW)
        store.set_watch_active(self.conn, 1, False)
        added = store.seed_watches(self.conn, self.ENTRIES, now=NOW)
        self.assertEqual(added, 0)
        self.assertEqual(len(store.list_watches(self.conn, active_only=False)), 2)
        # A watch paused in the app stays paused; config does not resurrect it.
        paused = [w for w in store.list_watches(self.conn, active_only=False)
                  if w.id == 1][0]
        self.assertFalse(paused.active)

    def test_seeded_watch_is_matched_by_the_scanner(self):
        config = parse_config({
            "default_window_days": 3,
            "notify": {"default_targets": ["mock://t"]},
            "sources": [{"label": "Mock OR", "provider": "Mock", "state": "OR"}],
            "watches": [{"name": "from config", "provider": "Mock",
                         "campground_ids": ["mock-cg-1"], "nights": 1}],
        })
        notifier = Notifier(["mock://t"])
        report = scan_once(self.conn, config, notifier=notifier,
                           start=START, window_days=2, now=NOW)
        self.assertEqual(len(store.list_watches(self.conn)), 1)
        self.assertGreater(report.alerts_sent, 0)


class TestConfig(unittest.TestCase):
    def test_parses_sources_and_defaults(self):
        cfg = parse_config({
            "home_base": {"latitude": 45.52, "longitude": -122.68},
            "sources": [
                {"label": "Mt Hood", "provider": "RecreationDotGov",
                 "state": "OR", "rec_area_ids": [1106]},
            ],
        })
        self.assertEqual(cfg.default_states, ["OR", "WA"])
        self.assertEqual(cfg.home_point, (45.52, -122.68))
        self.assertEqual(cfg.sources[0].rec_area_ids, ["1106"])   # coerced to str

    def test_json_config_loads_without_pyyaml(self):
        from app.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "default_states": ["WA"],
                "sources": [{"label": "m", "provider": "Mock", "state": "WA"}],
            }))
            cfg = load_config(path)
        self.assertEqual(cfg.default_states, ["WA"])
        self.assertEqual(len(cfg.sources), 1)

    def test_missing_config_is_not_fatal(self):
        from app.config import load_config
        cfg = load_config(Path("/nonexistent/config.yaml"))
        self.assertEqual(cfg.sources, [])
        self.assertEqual(cfg.default_states, ["OR", "WA"])


class TestNotifierFormatting(unittest.TestCase):
    def test_alert_includes_booking_link(self):
        msg = format_alert(a_site())
        self.assertIn("Mock Riverside Camp", msg)
        self.assertIn("https://example.invalid/book/mock-site-a", msg)

    def test_first_come_alert_has_no_dead_link(self):
        msg = format_alert(a_site(reservation_type="first_come", booking_url=None))
        self.assertIn("first-come", msg)
        self.assertNotIn("http", msg)

    def test_batching_sends_single_digest(self):
        notifier = Notifier(["mock://t"])
        sent = notifier.send_sites([a_site(), a_site(campsite_id="b")],
                                   batch=True, title="digest")
        self.assertEqual(sent, 1)
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("2 new", notifier.sent[0][1])

    def test_no_targets_is_reported_not_raised(self):
        notifier = Notifier([])
        self.assertFalse(notifier.send("hello"))


class TestUtil(unittest.TestCase):
    def test_haversine_known_distance(self):
        # Portland OR → Seattle WA is ~145 miles.
        miles = haversine_miles(45.5152, -122.6784, 47.6062, -122.3321)
        self.assertAlmostEqual(miles, 145, delta=5)

    def test_haversine_returns_none_when_unlocated(self):
        self.assertIsNone(haversine_miles(45.0, -122.0, None, None))


if __name__ == "__main__":
    unittest.main()
