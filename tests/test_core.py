"""Core offline tests — stdlib only, no network, no external deps.

Covers build-plan steps 1–3:
  step 1  mock scan → store → watch match
  step 2  camply adapter normalization (against a replayed fake camply module)
  step 3  catalog seed / live diff / never-shrink, plus the Reehers acceptance test

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import pathlib
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
        # The map must still have every pin — never emptied.
        pins = store.map_view(self.conn)
        self.assertEqual(len(pins), 3)
        # The Oregon campgrounds this source covers go stale...
        by_id = {p["id"]: p for p in pins}
        self.assertEqual(by_id["mock-cg-1"]["status"], STATUS_STALE)
        self.assertEqual(by_id["mock-cg-3"]["status"], STATUS_STALE)
        # ...but a source declared `state: OR` has nothing to say about a
        # Washington campground, so it is left alone rather than tarred with
        # the same failure.
        self.assertNotEqual(by_id["mock-cg-2"]["status"], STATUS_STALE)


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
        # Keyed "ReserveAmerica:OR" — the name the provider actually emits.
        # It was "ReserveAmerica" here while the seed agreed with it, and the
        # two of them agreed on a value nothing else in the app used.
        store.set_campground_status(self.conn, "ReserveAmerica:OR", "412704",
                                    STATUS_FULL, "no open sites", now=NOW)
        pins = store.map_view(self.conn)
        reehers = [p for p in pins if p["id"] == "412704"]
        self.assertEqual(len(reehers), 1)
        self.assertEqual(reehers[0]["status"], STATUS_FULL)
        self.assertEqual(reehers[0]["open_sites"], 0)

    def test_reehers_now_has_real_coordinates(self):
        # It had none while the seed entry was hand-written. The live directory
        # carries them, and a guessed coordinate would still be unacceptable.
        pin = [p for p in store.map_view(self.conn) if p["id"] == "412704"][0]
        self.assertTrue(pin["located"])
        self.assertAlmostEqual(pin["latitude"], 45.7066667, places=4)
        self.assertAlmostEqual(pin["longitude"], -123.3380556, places=4)

    def test_an_unlocated_campground_is_still_listed(self):
        # The §13 invariant Reehers used to demonstrate: no coordinates is a
        # legitimate state that shows as "location unknown", never a deletion.
        store.upsert_campgrounds(self.conn, [Campground(
            provider="ReserveAmerica:OR", id="no-coords",
            name="Somewhere Unmapped", state="OR")], now=NOW)
        pin = [p for p in store.map_view(self.conn) if p["id"] == "no-coords"][0]
        self.assertFalse(pin["located"])
        self.assertIsNone(pin["latitude"])

    def test_a_state_park_pin_admits_it_locates_the_park(self):
        # Seen on the map 2026-07-29: Cape Lookout's pin sits about a kilometre
        # south of its campground, and Cape Disappointment's in the middle of
        # the park. Both coordinates are the provider's own — CampSage draws the
        # identical spot — so the pin stays and the claim gets qualified.
        pin = [p for p in store.map_view(self.conn) if p["id"] == "412704"][0]
        self.assertEqual(pin["coord_precision"], "park")

    def test_a_federal_pin_is_the_campground_itself(self):
        # RIDB has one facility record per campground, so there is no park-level
        # slop to warn about — and warning anyway would train people to ignore it.
        store.upsert_campgrounds(self.conn, [Campground(
            provider="RecreationDotGov", id="232043", name="Some Forest CG",
            state="OR", latitude=45.0, longitude=-122.0)], now=NOW)
        pin = [p for p in store.map_view(self.conn) if p["id"] == "232043"][0]
        self.assertEqual(pin["coord_precision"], "campground")

    def test_an_unlocated_campground_claims_no_precision_at_all(self):
        store.upsert_campgrounds(self.conn, [Campground(
            provider="GoingToCamp:WA", id="nowhere", name="Unplaced Park",
            state="WA")], now=NOW)
        pin = [p for p in store.map_view(self.conn) if p["id"] == "nowhere"][0]
        self.assertIsNone(pin["coord_precision"])

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



# --------------------------------------------- ReserveAmerica (step 8) ----

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def ra_provider(**kw):
    """Provider wired to replay saved pages — never touches the network."""
    from app.providers.reserveamerica import ReserveAmericaProvider

    def fetcher(path, params):
        if path == "campgroundDirectoryList.do":
            # Page 2+ repeats page 1, which is what the live site does once you
            # run past the end. The parser must stop on "no new parks".
            return (FIXTURES / "ra_directory_or.html").read_text()
        if path == "campgroundDetails.do":
            return (FIXTURES / "ra_park_412704.html").read_text()
        raise AssertionError(f"unexpected path {path}")

    kw.setdefault("fetcher", fetcher)
    kw.setdefault("delay", 0)
    return ReserveAmericaProvider("OR", "oregonstateparks.reserveamerica.com", **kw)


class TestReserveAmericaDirectory(unittest.TestCase):
    """Parses real pages captured from the live Oregon portal on 2026-07-27."""

    def test_directory_yields_parks_with_coordinates(self):
        parks = ra_provider(state="OR").list_campgrounds()
        self.assertTrue(parks)
        for p in parks:
            self.assertEqual(p.provider, "ReserveAmerica:OR")
            self.assertEqual(p.state, "OR")
            self.assertTrue(p.has_location)
            # Oregon: latitude 42-46 N, longitude -117 to -125.
            self.assertTrue(41 < p.latitude < 47, p.latitude)
            self.assertTrue(-125 < p.longitude < -116, p.longitude)

    def test_paging_stops_when_directory_wraps(self):
        # Every page returns the same rows; without a "no new parks" check
        # this would loop to the 1000 bound.
        parks = ra_provider().list_campgrounds()
        ids = [p.id for p in parks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_known_park_is_present_with_verified_id(self):
        parks = {p.id: p for p in ra_provider().list_campgrounds()}
        self.assertIn("409402", parks)                     # Ainsworth State Park
        self.assertIn("Ainsworth", parks["409402"].name)


class TestReserveAmericaSiteList(unittest.TestCase):
    """Reehers: the park RA's own search will not return under any site type."""

    def setUp(self):
        self.sites = ra_provider().list_sites("412704")

    def test_park_page_lists_sites(self):
        self.assertTrue(self.sites)
        self.assertTrue(all(s["site_id"] for s in self.sites))

    def test_reehers_has_tent_sites_not_only_horse_sites(self):
        # `site_type` is now the table's own "Site type" column, not the icon.
        types = {s["site_type"] for s in self.sites}
        self.assertIn("HORSE SITE", types)
        # The whole point: searching "tent" at Reehers returns nothing on the
        # live site, yet tent sites plainly exist on the park's own page.
        self.assertIn("TENT SITE", types)

    def test_each_site_appears_exactly_once(self):
        # The page repeats every row; the parser used to return each twice.
        ids = [s["site_id"] for s in self.sites]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_whole_park_is_present_seventeen_of_seventeen(self):
        """Reehers has 17 sites, not the 34 the build plan claimed.

        Verified against the live page by Scott 2026-07-28: A-01..A-09 plus a
        Host site (10 HORSE SITE) and B-01..B-07 (7 TENT SITE). The earlier
        "20 horse sites and 14 tent sites" was never true, and the previous
        fixture was a partial capture of 10 rows.
        """
        self.assertEqual(len(self.sites), 17)
        loops = {s["loop"] for s in self.sites}
        self.assertEqual(loops, {"A", "B"})

    def test_the_b_loop_is_tent_sites(self):
        # The entire reason this provider exists: RA's tent search returns
        # nothing for Reehers, yet loop B is seven tent sites.
        b = [s for s in self.sites if s["loop"] == "B"]
        self.assertEqual(len(b), 7)
        self.assertTrue(all(s["site_type"] == "TENT SITE" for s in b))

    def test_the_host_site_is_visible_and_identifiable(self):
        # A camp-host pitch, the RA equivalent of RIDB's MANAGEMENT type.
        hosts = [s for s in self.sites if (s["name"] or "").lower() == "host"]
        self.assertEqual(len(hosts), 1)

    def test_columns_are_read_positionally_not_guessed(self):
        site = self.sites[0]
        self.assertEqual(site["loop"], "A")               # Loop column
        self.assertEqual(site["site_type"], "HORSE SITE")  # Site type column
        # A drive-in site states its driveway; a walk-to site leaves it empty.
        self.assertEqual(site["equipment_length"], "Back-In")


class TestWalkToSitesAreIdentifiable(unittest.TestCase):
    """The access-mode signal, ground-truthed against the live page.

    Scott checked L.L. Stub Stewart's Brooke Creek Hike-In Camp: the "Site
    type" column reads WALK TO, the "Equip length/Driveway" column is empty,
    and the sites cannot take an RV — despite carrying an `rv` type icon.
    """

    def setUp(self):
        from app.providers.reserveamerica import ReserveAmericaProvider
        self.sites = ReserveAmericaProvider.parse_sites(
            (FIXTURES / "ra_stub_stewart_sites.html").read_text())

    def test_walk_to_is_read_from_the_site_type_column(self):
        walk = [s for s in self.sites if s["site_type"] == "WALK TO"]
        self.assertEqual(len(walk), 21)
        self.assertTrue(all(s["name"].startswith("HIKE") for s in walk))

    def test_the_loop_name_is_not_mistaken_for_the_site_type(self):
        # The old parser returned "BROOKE CREEK HIKE-IN CAMP" as the type.
        walk = [s for s in self.sites if s["site_type"] == "WALK TO"][0]
        self.assertEqual(walk["loop"], "BROOKE CREEK HIKE-IN CAMP")
        self.assertNotEqual(walk["site_type"], walk["loop"])

    def test_an_empty_driveway_corroborates_walk_to(self):
        # Two independent signals for "no vehicle reaches this site".
        walk = [s for s in self.sites if s["site_type"] == "WALK TO"]
        self.assertTrue(all(not s["equipment_length"] for s in walk))
        drive = [s for s in self.sites if s["site_type"] != "WALK TO"]
        self.assertTrue(any(s["equipment_length"] for s in drive))

    def test_tent_site_is_not_treated_as_an_equipment_restriction(self):
        """Beverly Beach C27 is a TENT SITE that Scott has camped in a van.

        Nothing may turn the site type into a vehicle prohibition. Only an
        explicit "RV prohibited" in the site description does that, and we do
        not read descriptions — so "does my van fit?" is unknown, and §8g says
        unknown is shown, never filtered away.
        """
        import inspect
        import app.providers.reserveamerica as ra
        source = inspect.getsource(ra)
        # No code may branch on the tent type to exclude anything.
        self.assertNotIn('site_type"] == "TENT', source)
        self.assertNotIn("site_type'] == 'TENT", source)

    def test_the_driveway_number_is_never_used_as_a_bound(self):
        # C27/C28 read "20 Back-In" and are really 40+ feet — source data
        # error. Presence is usable; the value is not.
        import inspect
        import app.providers.reserveamerica as ra
        source = inspect.getsource(ra)
        for bad in ("int(cells[4]", "float(cells[4]", "equipment_length >",
                    "equipment_length <"):
            self.assertNotIn(bad, source)

    def test_the_type_icon_is_wrong_and_must_not_be_trusted(self):
        # Every one of these says `rv`. They cannot take an RV.
        walk = [s for s in self.sites if s["site_type"] == "WALK TO"]
        self.assertTrue(all(s["type_icon"] == "rv" for s in walk))


class TestReserveAmericaHonesty(unittest.TestCase):
    def test_unscoped_search_refuses_rather_than_returning_empty(self):
        """Refusing beats an empty list, which would read as 'nothing available'."""
        from app.providers.base import SearchRequest as SR
        with self.assertRaises(ValueError):
            ra_provider().search(SR(provider="ReserveAmerica:OR",
                                    start_date=START, end_date=START))

    def test_registry_builds_oregon_from_contract_code(self):
        from app.providers import build_provider as bp
        from app.providers.reserveamerica import ReserveAmericaProvider
        p = bp("ReserveAmerica:OR")
        self.assertIsInstance(p, ReserveAmericaProvider)
        self.assertEqual(p.host, "oregonstateparks.reserveamerica.com")
        self.assertEqual(p.state, "OR")

    def test_unknown_contract_code_refuses_rather_than_guessing_a_host(self):
        from app.providers import build_provider as bp
        with self.assertRaises(ValueError) as ctx:
            bp("ReserveAmerica:GA")
        self.assertIn("no known host", str(ctx.exception))

    def test_explicit_host_is_accepted_for_other_agencies(self):
        from app.providers import build_provider as bp
        p = bp("ReserveAmerica:GA", state="GA", host="a1.reserveamerica.com")
        self.assertEqual(p.host, "a1.reserveamerica.com")
        self.assertEqual(p.contract_code, "GA")

    def test_a_missing_http_client_refuses_instead_of_fetching_badly(self):
        """There is no stdlib fallback, on purpose (2026-07-31).

        `urllib` truncates this host's chunked responses and carries no
        session cookie, so falling back to it turns a clear ImportError into
        short pages that look real. The backfill run under the system
        `python3` did exactly that and fetched half-parks. Running is not
        better than not running when the output is wrong.
        """
        import builtins
        from app.providers import reserveamerica as ra

        real_import = builtins.__import__

        def no_requests(name, *args, **kwargs):
            if name == "requests":
                raise ImportError("no requests here")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = no_requests
        try:
            with self.assertRaises(ra.MissingHttpClient) as ctx:
                ra._fetch_url("https://example.invalid/x")
        finally:
            builtins.__import__ = real_import
        # The message has to name the fix, not just the problem.
        self.assertIn(".venv/bin/python", str(ctx.exception))


class TestReserveAmericaAvailability(unittest.TestCase):
    """The two-week per-site grid — the call that finally works."""

    CAL = FIXTURES / "ra_site_45859_calendar.html"

    def test_parses_real_grid(self):
        from app.providers.reserveamerica import ReserveAmericaProvider as RA
        days = RA.parse_calendar(self.CAL.read_text())
        self.assertEqual(len(days), 14)
        self.assertEqual(days[0][0], date(2026, 8, 10))
        self.assertEqual(days[-1][0], date(2026, 8, 23))
        self.assertTrue(all(s in ("a", "x", "r") for _d, s in days))

    def test_keeps_reserved_and_unavailable_distinct(self):
        from app.providers.reserveamerica import ReserveAmericaProvider as RA
        html_doc = self.CAL.read_text().replace(
            "class='td status a' title='Available' data-auto-id='mday20260811'",
            "class='td status r' title='Reserved' data-auto-id='mday20260811'", 1)
        grid = dict(RA.parse_calendar(html_doc))
        self.assertEqual(grid[date(2026, 8, 11)], "r")
        self.assertEqual(grid[date(2026, 8, 10)], "a")

    def _provider(self):
        from app.providers.reserveamerica import ReserveAmericaProvider as RA
        def fetcher(path, params):
            # search() reads the park matrix; the per-site page is only used by
            # site_availability(), which parse_calendar tests cover directly.
            if path == "campgroundDetails.do" and params.get("arvdate"):
                return (FIXTURES / "ra_park_412704_matrix.html").read_text()
            if path == "campgroundDetails.do":
                return (FIXTURES / "ra_park_412704.html").read_text()
            if path == "campsiteDetails.do":
                return self.CAL.read_text()
            raise AssertionError(path)
        return RA("OR", "oregonstateparks.reserveamerica.com",
                  state="OR", delay=0, fetcher=fetcher)

    def test_search_yields_runs_with_booking_links(self):
        from app.providers.base import SearchRequest as SR
        sites = self._provider().search(SR(
            provider="ReserveAmerica:OR", start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 23), nights=2, campground_ids=["412704"]))
        self.assertTrue(sites)
        s = sites[0]
        self.assertEqual(s.available_date, date(2026, 8, 10))
        self.assertEqual(s.nights, 2)
        self.assertEqual(s.facility_id, "412704")
        self.assertEqual(s.state, "OR")
        self.assertIn("arvdate=08/10/2026", s.booking_url)

    def test_run_must_be_fully_available(self):
        from app.providers.base import SearchRequest as SR
        # A 20-night run cannot fit inside a 14-day grid.
        sites = self._provider().search(SR(
            provider="ReserveAmerica:OR", start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 23), nights=20, campground_ids=["412704"]))
        self.assertEqual(sites, [])

    def test_unscoped_search_refuses_rather_than_crawling_everything(self):
        from app.providers.base import SearchRequest as SR
        with self.assertRaises(ValueError) as ctx:
            self._provider().search(SR(provider="ReserveAmerica:OR",
                                       start_date=START, end_date=START))
        self.assertIn("campground_ids", str(ctx.exception))



class TestReserveAmericaParkMatrix(unittest.TestCase):
    """One request returns every site x 14 days — the 34x saving."""

    MATRIX = FIXTURES / "ra_park_412704_matrix.html"

    def _provider(self):
        from app.providers.reserveamerica import ReserveAmericaProvider as RA
        self.calls = []
        def fetcher(path, params):
            self.calls.append((path, params.get("arvdate")))
            return self.MATRIX.read_text()
        return RA("OR", "oregonstateparks.reserveamerica.com",
                  state="OR", delay=0, fetcher=fetcher)

    def test_matrix_parses_every_site(self):
        from app.providers.reserveamerica import ReserveAmericaProvider as RA
        matrix = RA.parse_park_matrix(self.MATRIX.read_text())
        self.assertEqual(len(matrix), 16)
        total = sum(len(v) for v in matrix.values())
        self.assertEqual(total, 150)

    def test_year_comes_from_the_link_not_the_label(self):
        from app.providers.reserveamerica import ReserveAmericaProvider as RA
        matrix = RA.parse_park_matrix(self.MATRIX.read_text())
        every = {d for days in matrix.values() for d in days}
        self.assertTrue(all(d.year == 2026 for d in every))
        self.assertEqual(min(every), date(2026, 8, 10))

    def test_one_request_per_park_per_fortnight(self):
        from app.providers.base import SearchRequest as SR
        p = self._provider()
        p.search(SR(provider="ReserveAmerica:OR", start_date=date(2026, 8, 10),
                    end_date=date(2026, 8, 21), nights=1, campground_ids=["412704"]))
        # 12-day window fits in one fortnight -> exactly one fetch.
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "campgroundDetails.do")

    def test_longer_window_pages_by_fortnight(self):
        from app.providers.base import SearchRequest as SR
        p = self._provider()
        p.search(SR(provider="ReserveAmerica:OR", start_date=date(2026, 8, 10),
                    end_date=date(2026, 9, 20), nights=1, campground_ids=["412704"]))
        self.assertEqual(len(self.calls), 3)

    def test_overlapping_windows_do_not_duplicate_site_nights(self):
        from app.providers.base import SearchRequest as SR
        p = self._provider()
        sites = p.search(SR(provider="ReserveAmerica:OR",
                            start_date=date(2026, 8, 10), end_date=date(2026, 9, 20),
                            nights=1, campground_ids=["412704"]))
        keys = [s.key for s in sites]
        self.assertEqual(len(keys), len(set(keys)))

    def test_two_night_run_requires_both_nights_open(self):
        from app.providers.base import SearchRequest as SR
        p = self._provider()
        one = p.search(SR(provider="ReserveAmerica:OR", start_date=date(2026, 8, 10),
                          end_date=date(2026, 8, 21), nights=1, campground_ids=["412704"]))
        two = p.search(SR(provider="ReserveAmerica:OR", start_date=date(2026, 8, 10),
                          end_date=date(2026, 8, 21), nights=2, campground_ids=["412704"]))
        self.assertLess(len(two), len(one))
        self.assertTrue(all(s.nights == 2 for s in two))



class TestScopeLimits(unittest.TestCase):
    """Scott's scope decisions, 2026-07-27: 2-4 night trips, western states."""

    def test_defaults_are_two_to_four_nights(self):
        cfg = parse_config({})
        self.assertEqual(cfg.nights_options, [2, 3, 4])

    def test_scan_regions_default_to_declared_states_not_everywhere(self):
        # An unset scan scope must never silently mean "the whole country".
        cfg = parse_config({"default_states": ["OR", "WA", "CA"]})
        self.assertEqual(cfg.scan_regions, ["OR", "WA", "CA"])
        self.assertEqual(parse_config({}).scan_regions, ["OR", "WA"])

    def test_scan_regions_can_be_narrower_than_displayed_states(self):
        # The catalog may span more regions than we actively scan (§8k).
        cfg = parse_config({"default_states": ["OR", "WA", "BC"],
                            "scan_regions": ["OR"]})
        self.assertEqual(cfg.scan_regions, ["OR"])
        self.assertIn("BC", cfg.default_states)


# ------------------------------------------------- pacing (scanning-design) ----

from app import pacing, scanner  # noqa: E402
from app.pacing import Blocked, RateLimiter  # noqa: E402
from app.providers.base import Provider  # noqa: E402


def setUpModule():
    """No test may ever actually wait.

    Providers now go through the process-wide limiter, so without this the
    suite would sit out a real 2- or 6-second gap between fixture replays.
    Tests that care about pacing build their own limiter.
    """
    pacing.set_shared_limiter(RateLimiter(delays={}, default_delay=0, min_gap=0))


def tearDownModule():
    pacing.set_shared_limiter(None)


class FakeClock:
    """A clock that only moves when a sleep asks it to."""

    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


def fake_limiter(**kw):
    clock = FakeClock()
    limiter = RateLimiter(sleep=clock.sleep, clock=clock, **kw)
    return limiter, clock


class TestRateLimiterSpacing(unittest.TestCase):
    """The numbers in docs/scanning-design.md, enforced rather than documented."""

    def test_reserveamerica_gets_six_seconds_and_ridb_two(self):
        limiter, _ = fake_limiter()
        self.assertEqual(limiter.delay_for("oregonstateparks.reserveamerica.com"), 6.0)
        self.assertEqual(limiter.delay_for("ridb.recreation.gov"), 2.0)

    def test_unknown_host_gets_the_slow_default_not_the_fast_one(self):
        # Being wrong politely costs minutes; being wrong the other way costs
        # the household's IP address.
        limiter, _ = fake_limiter()
        self.assertEqual(limiter.delay_for("some-new-portal.example.gov"), 6.0)
        self.assertGreaterEqual(limiter.delay_for("unknown"), limiter.delay_for("ridb.recreation.gov"))

    def test_gap_is_measured_between_consecutive_hits_on_one_host(self):
        limiter, clock = fake_limiter()
        host = "oregonstateparks.reserveamerica.com"
        with limiter.slot(host):
            pass
        self.assertEqual(clock.slept, [])          # first request never waits
        with limiter.slot(host):
            pass
        self.assertEqual(clock.slept, [6.0])

    def test_a_slow_response_lengthens_the_gap_it_never_shortens_it(self):
        # Timestamps are taken when the response lands, so a 5s request is
        # followed by a full 6s of quiet, not 1s.
        limiter, clock = fake_limiter()
        host = "oregonstateparks.reserveamerica.com"
        with limiter.slot(host):
            clock.advance(5.0)
        with limiter.slot(host):
            pass
        self.assertEqual(clock.slept, [6.0])

    def test_interleaving_hosts_costs_only_the_global_floor(self):
        # This is why round-robin is worth doing: the second host's request
        # fills the first host's gap instead of queueing behind it.
        limiter, clock = fake_limiter()
        with limiter.slot("oregonstateparks.reserveamerica.com"):
            pass
        with limiter.slot("ridb.recreation.gov"):
            pass
        self.assertEqual(clock.slept, [pacing.GLOBAL_MIN_GAP])

    def test_a_burst_across_hosts_still_hits_the_floor(self):
        limiter, clock = fake_limiter(min_gap=1.0)
        for host in ("a.example.com", "b.example.com", "c.example.com"):
            with limiter.slot(host):
                pass
        self.assertEqual(clock.slept, [1.0, 1.0])

    def test_wait_time_is_reportable_before_the_request_is_made(self):
        # The progress widget needs a real number, not a spinner.
        limiter, _ = fake_limiter()
        host = "oregonstateparks.reserveamerica.com"
        self.assertEqual(limiter.wait_time(host), 0.0)
        with limiter.slot(host):
            pass
        self.assertEqual(limiter.wait_time(host), 6.0)

    def test_only_one_request_can_hold_the_slot_at_a_time(self):
        import threading

        limiter, _ = fake_limiter(min_gap=0)
        inside = threading.Event()
        release = threading.Event()
        overlapped = []

        def hold():
            with limiter.slot("a.example.com"):
                inside.set()
                release.wait(2)

        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(inside.wait(2))
        second = threading.Thread(
            target=lambda: overlapped.append("in") if limiter._turn.acquire(blocking=False)
            else overlapped.append("blocked")
        )
        second.start()
        second.join(2)
        release.set()
        t.join(2)
        self.assertEqual(overlapped, ["blocked"])


class TestRateLimiterBlocks(unittest.TestCase):
    """403/429 stops us dead. We never retry into a block (§13)."""

    def test_blocked_host_raises_instead_of_being_requested(self):
        limiter, _ = fake_limiter()
        limiter.block("oregonstateparks.reserveamerica.com", "429 — backing off")
        with self.assertRaises(Blocked):
            with limiter.slot("oregonstateparks.reserveamerica.com"):
                self.fail("a blocked host must never be requested")

    def test_a_block_is_shared_so_on_demand_cannot_walk_into_it(self):
        # One limiter for the whole process is the point: the sweep discovering
        # a block must stop the map's on-demand refresh too.
        limiter, _ = fake_limiter()
        limiter.block("camping.example.com", "403")
        self.assertTrue(limiter.is_blocked("camping.example.com"))
        self.assertIn("403", limiter.blocked_reason("camping.example.com"))

    def test_a_block_does_not_leak_to_other_hosts(self):
        limiter, _ = fake_limiter()
        limiter.block("oregonstateparks.reserveamerica.com", "429")
        self.assertFalse(limiter.is_blocked("ridb.recreation.gov"))

    def test_block_expires_after_the_cooldown(self):
        limiter, clock = fake_limiter(cooldown=60.0)
        limiter.block("a.example.com", "429")
        clock.advance(59)
        self.assertTrue(limiter.is_blocked("a.example.com"))
        clock.advance(2)
        self.assertFalse(limiter.is_blocked("a.example.com"))

    def test_reserveamerica_block_is_the_shared_block_type(self):
        from app.providers.reserveamerica import BlockedByProvider

        self.assertTrue(issubclass(BlockedByProvider, Blocked))


class TestProvidersShareOneLimiter(unittest.TestCase):
    def test_reserveamerica_and_camply_use_the_same_instance(self):
        from app.providers.reserveamerica import ReserveAmericaProvider

        shared = pacing.shared_limiter()
        ra = ReserveAmericaProvider("OR", "oregonstateparks.reserveamerica.com")
        camply = CamplyProvider("RecreationDotGov", state="OR")
        self.assertIs(ra.limiter, shared)
        self.assertIs(camply.limiter, shared)

    def test_an_unmapped_camply_provider_is_paced_conservatively(self):
        # We don't know GoingToCamp's host from this class, so it must not
        # borrow RIDB's fast budget.
        camply = CamplyProvider("GoingToCamp", state="WA")
        self.assertIsNone(camply.host)
        limiter, _ = fake_limiter()
        self.assertEqual(limiter.delay_for(camply._pacing_key), pacing.DEFAULT_DELAY)


# --------------------------------------------- round-robin + progress ----

class RecordingProvider(Provider):
    """Records the order units are asked for, and can refuse on cue."""

    def __init__(self, name, order, state=None, block_on=None, fail_on=None):
        self.name = name
        self.order = order
        self.state = state
        self.block_on = set(block_on or [])
        self.fail_on = set(fail_on or [])

    def search(self, req):
        target = req.campground_ids[0] if req.campground_ids else "*"
        self.order.append(f"{self.name}:{target}")
        if target in self.block_on:
            raise Blocked(f"{self.name} returned 429 — backing off")
        if target in self.fail_on:
            raise RuntimeError("upstream 503")
        return []


class RoundRobinTestCase(DBTestCase):
    def setUp(self):
        super().setUp()
        self.order: list[str] = []
        self.config = parse_config({
            "round_pause_seconds": 5,
            "sources": [
                {"label": "A", "provider": "Mock", "state": "OR",
                 "campground_ids": ["a1", "a2", "a3"]},
                {"label": "B", "provider": "Mock", "state": "WA",
                 "campground_ids": ["b1", "b2"]},
            ],
        })
        store.upsert_campgrounds(self.conn, [
            Campground(provider="src-OR", id=i, name=f"Park {i}", state="OR")
            for i in ("a1", "a2", "a3")
        ] + [
            Campground(provider="src-WA", id=i, name=f"Park {i}", state="WA")
            for i in ("b1", "b2")
        ], now=NOW)

    def factory(self, **kw):
        def build(spec, state=None, **_):
            return RecordingProvider(f"src-{state}", self.order, state=state, **kw)
        return build


class TestRoundRobin(RoundRobinTestCase):
    """Interleave sources; never drain one host before starting the next."""

    def test_units_alternate_between_sources(self):
        limiter, _ = fake_limiter(min_gap=0)
        scanner.scan_once(self.conn, self.config, notifier=Notifier([]),
                          start=START, window_days=3, now=NOW,
                          provider_factory=self.factory(), limiter=limiter)
        self.assertEqual(
            self.order,
            ["src-OR:a1", "src-WA:b1", "src-OR:a2", "src-WA:b2", "src-OR:a3"],
        )

    def test_a_named_campground_becomes_its_own_unit(self):
        # Small units are what make interleaving possible at all.
        provider = RecordingProvider("src-OR", self.order, state="OR")
        units = scanner.plan_source(
            self.conn, self.config.sources[0], provider, START, START + timedelta(days=3)
        )
        self.assertEqual([u.scope for u in units], [["a1"], ["a2"], ["a3"]])
        # And each is labelled from the catalog, so progress reads as a place.
        self.assertEqual([u.label for u in units], ["Park a1", "Park a2", "Park a3"])

    def test_a_source_naming_no_campgrounds_is_one_unit(self):
        # Nothing to split on — asking the provider for less is not an option.
        config = parse_config({"sources": [
            {"label": "Mock OR", "provider": "Mock", "state": "OR"}]})
        provider = RecordingProvider("src-OR", self.order, state="OR")
        units = scanner.plan_source(
            self.conn, config.sources[0], provider, START, START + timedelta(days=3)
        )
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].scope, [])

    def test_pause_between_rounds_but_not_after_the_last(self):
        limiter, clock = fake_limiter(min_gap=0)
        scanner.scan_once(self.conn, self.config, notifier=Notifier([]),
                          start=START, window_days=3, now=NOW,
                          provider_factory=self.factory(), limiter=limiter)
        # 3 rounds of work → 2 pauses, and no trailing pause on an empty queue.
        self.assertEqual(clock.slept, [5.0, 5.0])


class TestBlockedProviderIsSkipped(RoundRobinTestCase):
    """Stop dead on 403/429 — and say so, rather than showing empty parks."""

    def run_scan(self):
        limiter, _ = fake_limiter(min_gap=0)
        return scanner.scan_once(
            self.conn, self.config, notifier=Notifier([]),
            start=START, window_days=3, now=NOW,
            provider_factory=self.factory(block_on=["a2"]), limiter=limiter,
        )

    def test_the_blocked_source_is_abandoned_for_the_cycle(self):
        self.run_scan()
        # a3 is never requested — we do not retry into a block.
        self.assertNotIn("src-OR:a3", self.order)
        # ...and the other source keeps going. One host's refusal is not global.
        self.assertIn("src-WA:b2", self.order)

    def test_unchecked_parks_go_stale_not_full(self):
        # "We didn't look" must never render as "there's nothing there".
        self.run_scan()
        for cg_id in ("a2", "a3"):
            cg = store.get_campground(self.conn, "src-OR", cg_id)
            self.assertEqual(cg.status, STATUS_STALE)

    def test_the_report_counts_what_was_skipped(self):
        report = self.run_scan()
        self.assertEqual(report.skipped_units, 2)          # a2 and a3
        self.assertIn("A", report.blocked)
        self.assertIn("429", report.blocked["A"])

    def test_one_park_failing_does_not_stale_the_whole_state(self):
        # An ordinary error is not a block: the cycle carries on down the queue.
        limiter, _ = fake_limiter(min_gap=0)
        scanner.scan_once(self.conn, self.config, notifier=Notifier([]),
                          start=START, window_days=3, now=NOW,
                          provider_factory=self.factory(fail_on=["a1"]),
                          limiter=limiter)
        self.assertEqual(
            store.get_campground(self.conn, "src-OR", "a1").status, STATUS_STALE
        )
        self.assertIn("src-OR:a3", self.order)
        self.assertNotEqual(
            store.get_campground(self.conn, "src-OR", "a3").status, STATUS_STALE
        )


class TestScanStatusIsRecorded(DBTestCase):
    """Slowness is fine; unexplained slowness is not."""

    def setUp(self):
        super().setUp()
        self.config = parse_config({
            "round_pause_seconds": 0,
            "sources": [{"label": "Mock OR", "provider": "Mock", "state": "OR"}],
        })
        catalog.seed_catalog(self.conn, seed=MockProvider().list_campgrounds(), now=NOW)

    def test_status_starts_idle_and_is_never_missing(self):
        status = store.get_scan_status(self.conn)
        self.assertEqual(status.state, store.SCAN_IDLE)
        self.assertFalse(status.busy)

    def test_the_scanner_says_what_it_is_checking_while_it_checks(self):
        seen = []
        conn = self.conn

        class Peeking(MockProvider):
            def search(self, req):
                seen.append(store.get_scan_status(conn))
                return super().search(req)

        limiter, _ = fake_limiter(min_gap=0)
        scanner.scan_once(self.conn, self.config, notifier=Notifier([]),
                          start=START, window_days=3, now=NOW,
                          provider_factory=lambda spec, state=None, **kw: Peeking(state=state),
                          limiter=limiter)
        self.assertEqual(seen[0].state, store.SCAN_SCANNING)
        self.assertEqual(seen[0].target, "Mock OR")
        self.assertEqual(seen[0].total, 1)

    def test_it_returns_to_idle_with_an_honest_count(self):
        limiter, _ = fake_limiter(min_gap=0)
        scanner.scan_once(self.conn, self.config, notifier=Notifier([]),
                          start=START, window_days=3, now=NOW, limiter=limiter)
        status = store.get_scan_status(self.conn)
        self.assertEqual(status.state, store.SCAN_IDLE)
        self.assertEqual(status.message, "Checked 1 campground")

    def test_a_wait_is_explained_in_plain_language(self):
        # "Waiting 6s before the next request to …" beats a bare spinner.
        conn = self.conn
        limiter, _ = fake_limiter()
        seen = []

        class Paced(MockProvider):
            def search(self, req):
                with limiter.slot("oregonstateparks.reserveamerica.com"):
                    seen.append(store.get_scan_status(conn))
                return []

        config = parse_config({
            "round_pause_seconds": 0,
            "sources": [{"label": "Park one", "provider": "Mock", "state": "OR",
                         "campground_ids": ["p1", "p2"]}],
        })
        scanner.scan_once(conn, config, notifier=Notifier([]),
                          start=START, window_days=3, now=NOW,
                          provider_factory=lambda spec, state=None, **kw: Paced(state=state),
                          limiter=limiter)
        waiting = [s for s in seen if s.state == store.SCAN_WAITING]
        self.assertTrue(waiting)
        self.assertIn("reserveamerica.com", waiting[0].detail)
        self.assertIn("6s", waiting[0].detail)

    def test_the_reason_for_the_pace_travels_with_the_status(self):
        # Constant copy, and it must never be hidden — the honesty rule.
        status = store.get_scan_status(self.conn)
        self.assertIn("block us", status.note)
        self.assertIn("note", status.as_dict())

    def test_a_block_is_reported_to_the_user_not_swallowed(self):
        class Refusing(MockProvider):
            def search(self, req):
                raise Blocked("Mock returned 429 — backing off")

        limiter, _ = fake_limiter(min_gap=0)
        scanner.scan_once(self.conn, self.config, notifier=Notifier([]),
                          start=START, window_days=3, now=NOW,
                          provider_factory=lambda spec, state=None, **kw: Refusing(state=state),
                          limiter=limiter)
        status = store.get_scan_status(self.conn)
        self.assertEqual(status.state, store.SCAN_IDLE)
        self.assertIn("skipped", status.detail)


# ------------------------------- truncated responses (found live 2026-07-28) ----

class TestReserveAmericaTruncatedPages(unittest.TestCase):
    """A short page must never be read as a short directory.

    Found by running the real enumeration: this host ends roughly half its
    chunked responses without the terminating chunk. A page cut off mid-`<head>`
    is HTTP 200, looks like HTML, and parses to zero park rows — identical to
    the end of the directory. Enumeration stopped at 25 of 65 Oregon parks,
    alphabetically A through C, which drops Reehers and everything after it.
    """

    def setUp(self):
        from app.providers.reserveamerica import ReserveAmericaProvider
        self.cls = ReserveAmericaProvider
        self.full = (FIXTURES / "ra_directory_or.html").read_text()
        # Cut where the live failures cut: before the listing ever renders.
        cut = self.full.find("<table")
        self.truncated = self.full[:cut] if cut > 0 else self.full[:200]

    def provider(self, pages):
        """`pages` are page bodies served in order; then the full page repeats."""
        served = list(pages)
        calls = []

        def fetcher(path, params):
            calls.append((path, params.get("startIdx")))
            if path != "campgroundDirectoryList.do":
                raise AssertionError(f"unexpected path {path}")
            return served.pop(0) if served else self.full

        p = self.cls("OR", "oregonstateparks.reserveamerica.com",
                     delay=0, fetcher=fetcher)
        return p, calls

    def test_a_truncated_page_is_not_mistaken_for_a_complete_one(self):
        from app.providers.reserveamerica import page_is_complete
        self.assertTrue(page_is_complete(self.full))
        self.assertFalse(page_is_complete(self.truncated))

    def test_a_truncated_page_is_retried_once_before_giving_up(self):
        # Not a block signal — nobody asked us to stop — so one paced retry.
        p, calls = self.provider([self.truncated, self.full])
        parks = p.list_campgrounds()
        self.assertGreater(len(parks), 0)
        self.assertEqual(calls[0], calls[1])          # the same page, asked twice

    def test_two_truncated_pages_raise_rather_than_return_a_short_list(self):
        from app.providers.reserveamerica import IncompleteDirectory
        p, _ = self.provider([self.truncated, self.truncated])
        with self.assertRaises(IncompleteDirectory):
            p.list_campgrounds()

    def test_the_exact_live_failure_no_longer_silently_truncates(self):
        # Page 1 fine, page 2 truncated both times. The old code returned page
        # 1's parks and called that the whole directory; now it refuses.
        from app.providers.reserveamerica import IncompleteDirectory
        p, _ = self.provider([self.full, self.truncated, self.truncated])
        with self.assertRaises(IncompleteDirectory):
            p.list_campgrounds()

    def test_a_refused_directory_leaves_the_existing_catalog_alone(self):
        # §8k: refusing an update is right, because a partial directory looks
        # like an answer. What we already knew must survive it.
        from app.providers.reserveamerica import IncompleteDirectory
        conn = make_db()
        self.addCleanup(conn.close)
        catalog.seed_catalog(conn, seed=[
            Campground(provider="ReserveAmerica:OR", id="412704",
                       name="Reehers Camp Horse Camp", state="OR")], now=NOW)
        p, _ = self.provider([self.truncated, self.truncated])
        with self.assertRaises(IncompleteDirectory):
            p.list_campgrounds()
        self.assertIsNotNone(
            store.get_campground(conn, "ReserveAmerica:OR", "412704"))


class TestReserveAmericaTransport(unittest.TestCase):
    """httpx must not come back as the preferred client.

    This host ends chunked bodies without a terminator. urllib3 tolerates
    that; strict parsers (h11, http.client) do not — the stdlib truncated the
    same page to 74 KB where requests got 176 KB, every time. Preferring an
    unproven strict client over a proven tolerant one is risk with no upside,
    so httpx is gone from both the code and requirements.txt.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def test_the_fetcher_does_not_reach_for_httpx(self):
        import inspect
        import app.providers.reserveamerica as ra
        source = inspect.getsource(ra._fetch_url)
        # Strip the docstring, which legitimately explains why httpx is absent.
        code = source.split('"""')[2] if source.count('"""') >= 2 else source
        self.assertNotIn("httpx", code)
        self.assertIn("requests", code)

    def test_httpx_is_not_a_declared_dependency(self):
        # Comments may mention it; a requirement line may not.
        lines = [
            line.split("#")[0].strip()
            for line in (self.ROOT / "requirements.txt").read_text().splitlines()
        ]
        declared = [line for line in lines if line]
        self.assertTrue(any(d.startswith("requests") for d in declared), declared)
        self.assertFalse([d for d in declared if "httpx" in d], declared)


# ------------------------------- seed / provider key agreement (2026-07-28) ----

class TestSeedKeysMatchProviders(unittest.TestCase):
    """The seed's provider keys must be the ones providers actually emit.

    The seed carried `provider: "ReserveAmerica"` while the provider names
    itself `ReserveAmerica:OR`. Both the availability join and the §8k
    missing-from-live check are keyed on that string, so Reehers sat in the
    catalog permanently unknown, unlocated, and unable to receive the
    availability found for the very same park.
    """

    def setUp(self):
        self.seed = catalog.load_seed()

    def test_every_seed_provider_is_one_a_provider_would_emit(self):
        # Derived from the registry, not a hand-kept list — a new provider
        # whose seed key disagrees with its name must fail here on day one.
        from app.providers import known_providers
        emitted = set()
        for spec in known_providers():
            try:
                emitted.add(build_provider(spec, state="OR").name)
            except Exception:      # camply providers need camply installed
                emitted.add(catalog.build_provider_name(spec))
        used = {c.provider for c in self.seed}
        self.assertTrue(used, "seed is empty")
        self.assertEqual(used - emitted, set(),
                         f"seed uses provider keys nothing emits: {used - emitted}")

    def test_build_provider_name_keeps_the_instance_suffix(self):
        # Dropping it disabled the never-shrink check for ReserveAmerica.
        self.assertEqual(catalog.build_provider_name("ReserveAmerica:OR"),
                         "ReserveAmerica:OR")
        self.assertEqual(catalog.build_provider_name("ReserveAmerica:OR"),
                         build_provider("ReserveAmerica:OR").name)
        self.assertEqual(catalog.build_provider_name("RecreationDotGov"),
                         "RecreationDotGov")
        self.assertEqual(catalog.build_provider_name("Mock"), "Mock")

    def test_reehers_is_seeded_under_the_live_key_with_coordinates(self):
        reehers = [c for c in self.seed if "Reehers" in c.name]
        self.assertEqual(len(reehers), 1, "Reehers must appear exactly once")
        cg = reehers[0]
        self.assertEqual(cg.provider, "ReserveAmerica:OR")
        self.assertEqual(cg.id, "412704")
        self.assertTrue(cg.has_location, "coordinates were enumerated live")

    def test_availability_reaches_the_seeded_pin(self):
        # The join that was silently broken: same park, two different keys.
        conn = make_db()
        self.addCleanup(conn.close)
        catalog.seed_catalog(conn)
        store.upsert_availability(conn, [a_site(
            provider="ReserveAmerica:OR", campsite_id="s1",
            facility_id="412704", state="OR")], now=NOW)
        pin = [p for p in store.map_view(conn) if p["id"] == "412704"][0]
        self.assertEqual(pin["open_sites"], 1)
        self.assertTrue(pin["located"])

    def test_the_whole_oregon_directory_is_seeded_not_a_shortlist(self):
        # 65 parks enumerated live 2026-07-28; a shortlist is the Reehers bug.
        ra = [c for c in self.seed if c.provider == "ReserveAmerica:OR"]
        self.assertGreaterEqual(len(ra), 65)
        self.assertTrue(all(c.has_location for c in ra),
                        "the directory carries coordinates for every park")


class TestScopeComesFromTheCatalog(DBTestCase):
    """A provider that won't crawl blind gets its scope from the catalog."""

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="ReserveAmerica:OR", id=str(i),
                       name=f"Park {i}", state="OR")
            for i in range(1, 4)
        ], now=NOW)
        self.source = parse_config({"sources": [
            {"label": "Oregon State Parks", "provider": "ReserveAmerica:OR",
             "state": "OR", "campground_ids": []}]}).sources[0]

    def test_empty_config_scope_expands_to_every_catalogued_park(self):
        provider = build_provider("ReserveAmerica:OR", state="OR")
        units = scanner.plan_source(
            self.conn, self.source, provider, START, START + timedelta(days=3))
        self.assertEqual([u.scope for u in units], [["1"], ["2"], ["3"]])
        self.assertEqual([u.label for u in units], ["Park 1", "Park 2", "Park 3"])

    def test_an_empty_catalog_yields_no_units_rather_than_a_blind_crawl(self):
        conn = make_db()
        self.addCleanup(conn.close)
        provider = build_provider("ReserveAmerica:OR", state="OR")
        units = scanner.plan_source(
            conn, self.source, provider, START, START + timedelta(days=3))
        self.assertEqual(units, [])

    def test_providers_that_can_enumerate_are_left_alone(self):
        provider = MockProvider(state="OR")
        self.assertFalse(getattr(provider, "requires_scope", False))
        source = parse_config({"sources": [
            {"label": "Mock OR", "provider": "Mock", "state": "OR"}]}).sources[0]
        units = scanner.plan_source(
            self.conn, source, provider, START, START + timedelta(days=3))
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].scope, [])


# ------------------------------------------- GoingToCamp (WA + BC parks) ----

class GTCTestCase(unittest.TestCase):
    """Replays payloads captured live from Washington on 2026-07-28."""

    def setUp(self):
        from app.providers.goingtocamp import GoingToCampProvider
        self.cls = GoingToCampProvider
        self.locations = json.loads(
            (FIXTURES / "gtc_wa_resourcelocation.json").read_text())
        self.availability = json.loads(
            (FIXTURES / "gtc_wa_availability.json").read_text())

    def provider(self, availability=None, calls=None):
        avail = self.availability if availability is None else availability
        calls = calls if calls is not None else []

        def fetcher(path, params):
            calls.append((path, params))
            if path == "/api/resourceLocation":
                return self.locations
            if path == "/api/availability/map":
                got = avail(params) if callable(avail) else avail
                return got
            raise AssertionError(f"unexpected path {path}")

        return self.cls("WA", "washington.goingtocamp.com", 3,
                        state="WA", fetcher=fetcher), calls


class TestGoingToCampCatalog(GTCTestCase):
    def test_only_campable_locations_are_catalogued(self):
        # The rec area lists day-use spots too; they are not campgrounds.
        p, _ = self.provider()
        names = {c.name for c in p.list_campgrounds()}
        self.assertIn("Alta Lake State Park", names)
        self.assertNotIn("Anderson Lake", names)      # no campable category
        self.assertNotIn("Big Eddy", names)

    def test_coordinates_are_parsed_from_the_gps_string(self):
        p, _ = self.provider()
        alta = [c for c in p.list_campgrounds() if c.name == "Alta Lake State Park"][0]
        self.assertAlmostEqual(alta.latitude, 48.03218, places=5)
        self.assertAlmostEqual(alta.longitude, -119.9347, places=4)

    def test_a_park_without_coordinates_is_kept_not_dropped(self):
        # Sun Lakes has no gpsCoordinates live. §13: show it as unlocated,
        # never drop it and never invent a position. BC Parks is almost
        # entirely in this state, so it is not an edge case.
        p, _ = self.provider()
        parks = p.list_campgrounds()
        sun = [c for c in parks if c.name == "Sun Lakes State Park"]
        self.assertEqual(len(sun), 1)
        self.assertIsNone(sun[0].latitude)
        self.assertFalse(sun[0].has_location)

    def test_the_whole_catalog_costs_one_request(self):
        p, calls = self.provider()
        p.list_campgrounds()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "/api/resourceLocation")

    def test_registry_builds_both_portals(self):
        wa = build_provider("GoingToCamp:WA")
        bc = build_provider("GoingToCamp:BC")
        self.assertEqual(wa.name, "GoingToCamp:WA")
        self.assertEqual(wa.host, "washington.goingtocamp.com")
        self.assertEqual(bc.state, "BC")
        self.assertEqual(bc.host, "camping.bcparks.ca")

    def test_unknown_portal_refuses_rather_than_guessing_a_host(self):
        with self.assertRaises(ValueError):
            build_provider("GoingToCamp:Nowhere")


class TestGoingToCampAvailabilityEncoding(GTCTestCase):
    """The daily codes, derived by cross-tabulation — documented nowhere.

    Verified live 2026-07-28 on one Alta Lake loop: every site bookable for a
    2-night stay read (0,0,0) or (0,0,1), and no unbookable site had its first
    two entries both 0. So 0 is open, the entry after the last night is
    checkout day, and anything that is not 0 is not open.
    """

    def runs(self, codes, nights=2, start=None):
        p, _ = self.provider()
        req = SearchRequest(provider=p.name, start_date=start or START,
                            end_date=(start or START) + timedelta(days=30),
                            nights=nights, campground_ids=["-1"])
        return list(p._runs(req, "-1", "site-1", codes))

    def test_a_two_night_run_needs_two_open_nights(self):
        self.assertEqual(len(self.runs([0, 0, 1])), 1)      # bookable
        self.assertEqual(len(self.runs([1, 0, 0])), 1)      # bookable from night 2
        self.assertEqual(len(self.runs([1, 1, 0])), 0)      # only one open night
        self.assertEqual(len(self.runs([1, 0, 1])), 0)

    def test_checkout_day_is_not_required_to_be_open(self):
        # (0,0,1) was observed on 7 sites that were genuinely bookable.
        run = self.runs([0, 0, 1])[0]
        self.assertEqual(run.available_date, START)
        self.assertEqual(run.nights, 2)

    def test_unknown_codes_are_never_treated_as_available(self):
        # Codes 4 and 5 appear on a couple of sites and mean something we have
        # not identified. Unknown is not a green light (§13).
        self.assertEqual(len(self.runs([4, 4, 4])), 0)
        self.assertEqual(len(self.runs([5, 4, 5])), 0)
        self.assertEqual(len(self.runs([0, 4])), 0)

    def test_offsets_map_to_real_dates(self):
        runs = self.runs([1, 1, 0, 0, 0], nights=2)
        self.assertEqual([r.available_date for r in runs],
                         [START + timedelta(days=2), START + timedelta(days=3)])

    def test_the_captured_loop_reproduces_the_live_counts(self):
        """Cross-checked against the platform's own stay-level answer.

        The live run asked the API "is this site bookable for a 2-night stay
        starting on day one" and got 25 of 46. Our run detection finds runs
        starting on ANY night, so it legitimately finds more — the difference
        must be exactly the sites whose codes are (1, 0, 0), bookable from
        night two. That is the check worth making: not that the numbers match,
        but that every extra one is explained.
        """
        p, _ = self.provider()
        ra = self.availability["resourceAvailabilities"]
        self.assertEqual(len(ra), 46)
        req = SearchRequest(provider=p.name, start_date=START,
                            end_date=START + timedelta(days=3), nights=2,
                            campground_ids=["-1"])
        codes = {rid: [d["availability"] for d in days] for rid, days in ra.items()}
        runs = {rid: list(p._runs(req, "-1", rid, c)) for rid, c in codes.items()}

        first_night = {rid for rid, rs in runs.items()
                       if any(r.available_date == START for r in rs)}
        self.assertEqual(len(first_night), 25, "matches the platform's own answer")

        any_night = {rid for rid, rs in runs.items() if rs}
        extra = any_night - first_night
        self.assertEqual({tuple(codes[rid]) for rid in extra}, {(1, 0, 0)})
        self.assertEqual(len(extra), 2)


class TestGoingToCampSearch(GTCTestCase):
    def test_unscoped_search_refuses_rather_than_crawling_everything(self):
        p, _ = self.provider()
        with self.assertRaises(ValueError):
            p.search(SearchRequest(provider=p.name, start_date=START,
                                   end_date=START + timedelta(days=3), nights=2))

    def test_sub_maps_are_followed_or_the_park_reads_as_empty(self):
        # A park's root map usually holds no sites — only links to its loops.
        root = {"resourceAvailabilities": {},
                "mapLinkAvailabilities": {"-500": {}, "-501": {}}}
        loop = {"resourceAvailabilities": {"s1": [{"availability": 0},
                                                  {"availability": 0}]},
                "mapLinkAvailabilities": {}}
        seen_maps = []

        def avail(params):
            seen_maps.append(str(params["mapId"]))
            return root if str(params["mapId"]).startswith("-2147") else loop

        p, calls = self.provider(availability=avail)
        req = SearchRequest(provider=p.name, start_date=START,
                            end_date=START + timedelta(days=2), nights=2,
                            campground_ids=["-2147483647"])
        sites = p.search(req)
        self.assertIn("-500", seen_maps)
        self.assertIn("-501", seen_maps)
        self.assertTrue(sites, "sites live on the sub-maps, not the root")

    def test_booking_url_deep_links_into_their_own_flow(self):
        # §8j-B: we hand off, we never book.
        p, _ = self.provider()
        url = p.booking_url("-2147483647", START)
        self.assertIn("washington.goingtocamp.com/create-booking/results", url)
        self.assertIn("resourceLocationId=-2147483647", url)
        self.assertIn(f"startDate={START.isoformat()}", url)

    def test_scope_is_required_so_the_scanner_fills_it_from_the_catalog(self):
        self.assertTrue(build_provider("GoingToCamp:WA").requires_scope)


class TestGoingToCampSeeded(unittest.TestCase):
    def setUp(self):
        self.seed = catalog.load_seed()

    def test_washington_state_parks_are_in_the_catalog(self):
        wa = [c for c in self.seed if c.provider == "GoingToCamp:WA"]
        self.assertGreaterEqual(len(wa), 79)
        self.assertTrue(any("Alta Lake" in c.name for c in wa))
        self.assertTrue(all(c.state == "WA" for c in wa))

    def test_bc_parks_are_catalogued_even_though_they_lack_coordinates(self):
        # The platform does not publish coordinates for BC. They are still the
        # known universe (§8k) — searchable, listed, honestly unmappable.
        bc = [c for c in self.seed if c.provider == "GoingToCamp:BC"]
        self.assertGreaterEqual(len(bc), 100)
        self.assertTrue(all(c.state == "BC" for c in bc))
        self.assertTrue(any(not c.has_location for c in bc))


# --------------------------------- coordinate backfill (BC Parks, 2026-07-28) ----

from app import coordinates  # noqa: E402


def bc_area(id_, name, slug, lat=50.0, lon=-120.0):
    return {"id": id_, "protectedAreaName": name,
            "url": f"https://bcparks.ca/{slug}/", "latitude": lat, "longitude": lon}


class TestCoordinateProvenance(DBTestCase):
    """A coordinate must say where it came from, and never be invented."""

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="GoingToCamp:BC", id="-1", name="Ruckle Provincial Park",
                       state="BC"),
            Campground(provider="GoingToCamp:BC", id="-2", name="Already Located",
                       state="BC", latitude=49.0, longitude=-123.0),
        ], now=NOW)

    def test_a_coordinate_records_its_source(self):
        ok = store.set_campground_coordinates(
            self.conn, "GoingToCamp:BC", "-1", 48.7, -123.4, source="a real source")
        self.assertTrue(ok)
        cg = store.get_campground(self.conn, "GoingToCamp:BC", "-1")
        self.assertEqual(cg.coord_source, "a real source")
        self.assertTrue(cg.has_location)

    def test_a_missing_coordinate_never_overwrites_a_present_one(self):
        store.set_campground_coordinates(
            self.conn, "GoingToCamp:BC", "-2", None, None, source="x")
        cg = store.get_campground(self.conn, "GoingToCamp:BC", "-2")
        self.assertEqual(cg.latitude, 49.0)

    def test_provenance_is_not_optional(self):
        with self.assertRaises(ValueError):
            store.set_campground_coordinates(
                self.conn, "GoingToCamp:BC", "-1", 48.7, -123.4, source="")

    def test_provenance_report_counts_unlocated_honestly(self):
        counts = store.coordinate_provenance(self.conn)
        self.assertEqual(counts.get("unlocated"), 1)

    def test_a_routine_enumeration_does_not_erase_a_recorded_source(self):
        # Re-running catalog-refresh must not silently downgrade the claim.
        store.set_campground_coordinates(
            self.conn, "GoingToCamp:BC", "-1", 48.7, -123.4, source="BC Parks API")
        store.upsert_campgrounds(self.conn, [
            Campground(provider="GoingToCamp:BC", id="-1", name="Ruckle Provincial Park",
                       state="BC", latitude=48.7, longitude=-123.4)], now=NOW)
        cg = store.get_campground(self.conn, "GoingToCamp:BC", "-1")
        self.assertEqual(cg.coord_source, "BC Parks API")


class TestBCBackfill(DBTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="GoingToCamp:BC", id="-1", name="Ruckle Provincial Park",
                       state="BC"),
            Campground(provider="GoingToCamp:BC", id="-2", name="Nowhere In The API",
                       state="BC"),
            Campground(provider="GoingToCamp:BC", id="-3", name="Has Coords Already",
                       state="BC", latitude=49.0, longitude=-123.0),
        ], now=NOW)
        self.websites = {
            "-1": "https://bcparks.ca/ruckle-park/",
            "-2": "https://bcparks.ca/nowhere-park/",
            "-3": "https://bcparks.ca/already-park/",
        }
        self.areas = [bc_area(1, "Ruckle Park", "ruckle-park", 48.77, -123.38),
                      bc_area(2, "Already Park", "already-park", 1.0, 2.0)]

    def fetcher(self, areas=None, total=None):
        areas = self.areas if areas is None else areas

        def fetch(path, params):
            page = int(params["pagination[page]"])
            size = int(params["pagination[pageSize]"])
            chunk = areas[(page - 1) * size: page * size]
            count = len(areas) if total is None else total
            pages = max(1, (len(areas) + size - 1) // size)
            return {"data": chunk,
                    "meta": {"pagination": {"page": page, "pageSize": size,
                                            "pageCount": pages, "total": count}}}
        return fetch

    def test_it_locates_by_url_slug_and_records_the_source(self):
        report = coordinates.backfill_bc(
            self.conn, websites=self.websites, fetcher=self.fetcher(), now=NOW)
        self.assertEqual(report.located, 1)
        cg = store.get_campground(self.conn, "GoingToCamp:BC", "-1")
        self.assertAlmostEqual(cg.latitude, 48.77)
        self.assertIn("BC Parks API", cg.coord_source)

    def test_an_unmatched_park_stays_unlocated_rather_than_guessed(self):
        # No name fallback: the two systems name parks differently
        # ("Ruckle Provincial Park" vs "Ruckle Park"), and a near-miss would
        # put a pin on the wrong park.
        report = coordinates.backfill_bc(
            self.conn, websites=self.websites, fetcher=self.fetcher(), now=NOW)
        self.assertIn("Nowhere In The API", report.unmatched)
        cg = store.get_campground(self.conn, "GoingToCamp:BC", "-2")
        self.assertFalse(cg.has_location)
        self.assertIsNone(cg.coord_source)

    def test_a_park_that_already_has_coordinates_is_left_alone(self):
        coordinates.backfill_bc(
            self.conn, websites=self.websites, fetcher=self.fetcher(), now=NOW)
        cg = store.get_campground(self.conn, "GoingToCamp:BC", "-3")
        self.assertEqual(cg.latitude, 49.0)     # not the API's 1.0
        self.assertIsNone(cg.coord_source)

    def test_rerunning_is_idempotent(self):
        first = coordinates.backfill_bc(
            self.conn, websites=self.websites, fetcher=self.fetcher(), now=NOW)
        second = coordinates.backfill_bc(
            self.conn, websites=self.websites, fetcher=self.fetcher(), now=NOW)
        self.assertEqual(first.located, 1)
        self.assertEqual(second.located, 0)     # nothing left to fill in

    def test_a_short_source_list_is_refused_not_used(self):
        """The pagination trap, caught live.

        The API silently ignores `offset`/`_start`, and its default ordering
        is not stable across pages — paging without an explicit sort returned
        1052 rows holding only 736 distinct parks. A third of the province went
        missing while duplicates filled the gap, which would have read as
        "the API doesn't have your park" rather than as an error.
        """
        short = self.fetcher(total=999)          # API claims more than it sends
        with self.assertRaises(coordinates.IncompleteSource):
            coordinates.backfill_bc(
                self.conn, websites=self.websites, fetcher=short, now=NOW)

    def test_paging_always_asks_for_a_stable_sort(self):
        asked = []

        def fetch(path, params):
            asked.append(params)
            return {"data": self.areas,
                    "meta": {"pagination": {"page": 1, "pageSize": 100,
                                            "pageCount": 1, "total": len(self.areas)}}}
        coordinates.fetch_bc_protected_areas(fetcher=fetch)
        self.assertTrue(asked)
        self.assertIn("sort[0]", asked[0])

    def test_slug_parsing(self):
        self.assertEqual(coordinates.park_slug("https://bcparks.ca/ruckle-park/"),
                         "ruckle-park")
        self.assertEqual(coordinates.park_slug("https://bcparks.ca/Ruckle-Park"),
                         "ruckle-park")
        self.assertIsNone(coordinates.park_slug(None))
        self.assertIsNone(coordinates.park_slug(""))


class TestSchemaMigration(unittest.TestCase):
    def test_columns_are_added_to_an_existing_database(self):
        # A database created before provenance existed must gain the columns,
        # because CREATE TABLE IF NOT EXISTS will not add them.
        conn = db.connect(":memory:")
        conn.executescript("""
            CREATE TABLE campgrounds (
              provider TEXT, id TEXT, name TEXT, rec_area TEXT, state TEXT,
              latitude REAL, longitude REAL, reservation_type TEXT, status TEXT,
              status_reason TEXT, closed_until TEXT, first_cataloged TEXT,
              last_checked TEXT, seeded INTEGER DEFAULT 0,
              PRIMARY KEY (provider, id));""")
        self.addCleanup(conn.close)
        applied = db.migrate(conn)
        self.assertIn("campgrounds.coord_source", applied)
        self.assertEqual(db.migrate(conn), [])          # idempotent
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(campgrounds)")}
        self.assertIn("coord_source", cols)
        self.assertIn("coord_updated", cols)


class TestSeedCarriesProvenance(unittest.TestCase):
    def test_bc_parks_are_located_in_the_committed_seed(self):
        seed = catalog.load_seed()
        bc = [c for c in seed if c.provider == "GoingToCamp:BC"]
        located = [c for c in bc if c.has_location]
        self.assertGreaterEqual(len(located), 100,
                                "the BC backfill result must be committed")
        self.assertTrue(all("BC Parks API" in (c.coord_source or "")
                            for c in located if c.coord_source))

    def test_provenance_survives_a_seed_round_trip(self):
        # Otherwise the claim is only as durable as someone's local .db file.
        conn = make_db()
        self.addCleanup(conn.close)
        catalog.seed_catalog(conn)
        bc = [c for c in store.list_campgrounds(conn, provider="GoingToCamp:BC")
              if c.coord_source]
        self.assertTrue(bc)
        self.assertIn("BC Parks API", bc[0].coord_source)


# ------------------------- first-come campgrounds are never called "full" ----

class TestFirstComeIsNeverCalledFull(DBTestCase):
    """206 of the 803 catalogued campgrounds are first-come, from RIDB's own
    Reservable flag. They have no reservation feed, so a scan finding nothing
    says *nothing* about whether sites are free — it is exactly as
    uninformative as not looking.

    Marking them "full" would send someone driving past a campground with
    space: the Reehers failure inverted, with the map asserting what it does
    not know.
    """

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="res", name="Reservable Camp",
                       state="OR", reservation_type="reservable"),
            Campground(provider="Mock", id="fcfs", name="First-Come Camp",
                       state="OR", reservation_type="first_come"),
        ], now=NOW)

    def stamp(self, cg_id):
        return store.stamp_status_from_availability(
            self.conn, "Mock", cg_id, checked_ok=True, now=NOW)

    def test_an_empty_scan_leaves_a_first_come_site_unknown_not_full(self):
        self.assertEqual(self.stamp("fcfs"), STATUS_UNKNOWN)
        cg = store.get_campground(self.conn, "Mock", "fcfs")
        self.assertIn("first-come", cg.status_reason)

    def test_a_reservable_site_with_nothing_open_is_still_full(self):
        # The honest statement here: we checked a real feed and it was empty.
        self.assertEqual(self.stamp("res"), STATUS_FULL)

    def test_a_first_come_site_reporting_availability_is_shown_as_available(self):
        # Some providers do publish first-come status; believe it when they do.
        store.upsert_availability(self.conn, [a_site(
            provider="Mock", campsite_id="f1", facility_id="fcfs",
            reservation_type="first_come")], now=NOW)
        self.assertEqual(self.stamp("fcfs"), STATUS_AVAILABLE)

    def test_the_end_to_end_scan_does_not_call_them_full(self):
        class Empty(MockProvider):
            def search(self, req):
                return []

        config = parse_config({"round_pause_seconds": 0, "sources": [
            {"label": "S", "provider": "Mock", "state": "OR"}]})
        scan_once(self.conn, config, notifier=Notifier([]), start=START,
                  window_days=3, now=NOW,
                  provider_factory=lambda spec, state=None, **kw: Empty(state=state))
        self.assertEqual(
            store.get_campground(self.conn, "Mock", "fcfs").status, STATUS_UNKNOWN)
        self.assertEqual(
            store.get_campground(self.conn, "Mock", "res").status, STATUS_FULL)

    def test_the_real_catalog_has_first_come_entries_to_protect(self):
        seed = catalog.load_seed()
        fcfs = [c for c in seed if c.reservation_type == "first_come"]
        self.assertGreater(len(fcfs), 100,
                           "RIDB flags a large minority as first-come")


# ------------------- a scan only speaks for what it actually queried ----

class TestScanDoesNotOverreach(DBTestCase):
    """A rec-area-scoped source must not stamp the whole state.

    Scanning Mt Hood used to mark every Oregon recreation.gov campground
    `full`, including coastal ones hundreds of miles away that were never
    queried — the map asserting knowledge it did not have.
    """

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="in-scope", name="Mt Hood site", state="OR"),
            Campground(provider="Mock", id="far-away", name="Coast site", state="OR"),
        ], now=NOW)

    def scan(self, provider_cls, rec_areas=None):
        source = {"label": "Mt Hood NF", "provider": "Mock", "state": "OR"}
        if rec_areas:
            source["rec_area_ids"] = rec_areas
        config = parse_config({"round_pause_seconds": 0, "sources": [source]})
        return scan_once(
            self.conn, config, notifier=Notifier([]), start=START, window_days=3,
            now=NOW, provider_factory=lambda spec, state=None, **kw: provider_cls(state=state))

    def test_a_rec_area_scan_leaves_uncovered_campgrounds_alone(self):
        class OnlyInScope(MockProvider):
            def search(self, req):
                return [a_site(provider="Mock", campsite_id="s1",
                               facility_id="in-scope", state="OR")]

        self.scan(OnlyInScope, rec_areas=["1106"])
        self.assertEqual(
            store.get_campground(self.conn, "Mock", "in-scope").status,
            STATUS_AVAILABLE)
        # Never queried, so it keeps whatever it had — it is NOT called full.
        far = store.get_campground(self.conn, "Mock", "far-away")
        self.assertEqual(far.status, STATUS_UNKNOWN)
        self.assertNotEqual(far.status, STATUS_FULL)

    def test_an_empty_rec_area_scan_stamps_nothing_at_all(self):
        class Empty(MockProvider):
            def search(self, req):
                return []

        self.scan(Empty, rec_areas=["1106"])
        for cg_id in ("in-scope", "far-away"):
            self.assertEqual(
                store.get_campground(self.conn, "Mock", cg_id).status,
                STATUS_UNKNOWN, f"{cg_id} must not be called full")

    def test_an_unscoped_source_still_stamps_the_whole_provider(self):
        # A source that genuinely covers everything keeps the old behaviour —
        # this is about not claiming more than the scope supports.
        class Empty(MockProvider):
            def search(self, req):
                return []

        self.scan(Empty)                      # no rec_area_ids
        self.assertEqual(
            store.get_campground(self.conn, "Mock", "far-away").status, STATUS_FULL)

    def test_a_per_campground_unit_stamps_only_its_own_park(self):
        class Empty(MockProvider):
            def search(self, req):
                return []

        config = parse_config({"round_pause_seconds": 0, "sources": [
            {"label": "S", "provider": "Mock", "state": "OR",
             "campground_ids": ["in-scope"]}]})
        scan_once(self.conn, config, notifier=Notifier([]), start=START,
                  window_days=3, now=NOW,
                  provider_factory=lambda spec, state=None, **kw: Empty(state=state))
        self.assertEqual(
            store.get_campground(self.conn, "Mock", "in-scope").status, STATUS_FULL)
        self.assertEqual(
            store.get_campground(self.conn, "Mock", "far-away").status, STATUS_UNKNOWN)


# ------------- campground-level vs site-level first-come are different ----

class TestFirstComeIsTwoSeparateClaims(DBTestCase):
    """"This campground takes no reservations" and "this bookable campground
    also has first-come sites" are different facts, and we usually know only the
    first. Three-state per §8g: yes / no / **unknown**, never inferred.
    """

    def test_the_two_fields_are_independent(self):
        cg = Campground(provider="p", id="1", name="n",
                        reservation_type="reservable", first_come_sites=True)
        self.assertEqual(cg.reservation_type, "reservable")
        self.assertIs(cg.first_come_sites, True)

    def test_unknown_is_the_default_and_says_nothing(self):
        cg = Campground(provider="p", id="1", name="n")
        self.assertIsNone(cg.first_come_sites)
        # Crucially it does NOT claim every site is bookable.
        self.assertEqual(cg.booking_label, "Reservable")
        self.assertNotIn("every site", cg.booking_label)

    def test_labels_read_honestly_in_each_state(self):
        def label(rt, fc):
            return Campground(provider="p", id="1", name="n",
                              reservation_type=rt, first_come_sites=fc).booking_label
        self.assertIn("no reservations", label("first_come", None))
        self.assertIn("some sites are first-come", label("reservable", True))
        self.assertIn("every site is bookable", label("reservable", False))
        self.assertEqual(label("reservable", None), "Reservable")

    def test_it_round_trips_through_the_database(self):
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="mixed", name="Mixed", state="OR",
                       reservation_type="reservable", first_come_sites=True),
            Campground(provider="Mock", id="pure", name="Pure", state="OR",
                       reservation_type="reservable", first_come_sites=False),
            Campground(provider="Mock", id="dunno", name="Dunno", state="OR",
                       reservation_type="reservable"),
        ], now=NOW)
        got = {c.id: c.first_come_sites
               for c in store.list_campgrounds(self.conn, provider="Mock")}
        self.assertIs(got["mixed"], True)
        self.assertIs(got["pure"], False)
        self.assertIsNone(got["dunno"])       # unknown survives as unknown

    def test_an_enumeration_that_cannot_tell_does_not_erase_a_known_answer(self):
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="mixed", name="Mixed", state="OR",
                       first_come_sites=True)], now=NOW)
        # A later refresh from a source with no site-level data.
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="mixed", name="Mixed", state="OR")], now=NOW)
        cg = store.get_campground(self.conn, "Mock", "mixed")
        self.assertIs(cg.first_come_sites, True)

    def test_the_map_payload_carries_both_and_a_ready_sentence(self):
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="fcfs", name="Walk-up only", state="OR",
                       reservation_type="first_come")], now=NOW)
        pin = store.map_view(self.conn)[0]
        self.assertEqual(pin["reservation_type"], "first_come")
        self.assertIsNone(pin["first_come_sites"])
        self.assertIn("no reservations", pin["booking_label"])

    def test_it_survives_the_seed_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.json"
            catalog.write_seed([
                Campground(provider="Mock", id="mixed", name="Mixed",
                           first_come_sites=True),
                Campground(provider="Mock", id="dunno", name="Dunno"),
            ], path=path)
            back = {c.id: c.first_come_sites for c in catalog.load_seed(path)}
        self.assertIs(back["mixed"], True)
        self.assertIsNone(back["dunno"])

    def test_a_walk_up_site_inside_a_bookable_campground_is_coherent(self):
        """The case the two fields exist for.

        A reservable campground that also has first-come sites: the campground
        keeps its booking route, while an individual first-come site in it
        still gets no booking link (§4). Neither fact overrides the other.
        """
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="mixed", name="Mixed Camp", state="OR",
                       reservation_type="reservable", first_come_sites=True)], now=NOW)
        cg = store.get_campground(self.conn, "Mock", "mixed")
        self.assertIn("some sites are first-come", cg.booking_label)

        walk_up = a_site(facility_id="mixed", reservation_type="first_come",
                         booking_url=None)
        bookable = a_site(campsite_id="b1", facility_id="mixed")
        self.assertNotIn("http", format_alert(walk_up))
        self.assertIn("http", format_alert(bookable))


# ------------------------------- site inventory backfill (2026-07-28) ----

from app import inventory  # noqa: E402


def ridb_site(name, reservable=True, site_type="STANDARD NONELECTRIC"):
    return {"CampsiteName": name, "CampsiteReservable": reservable,
            "CampsiteType": site_type, "TypeOfUse": "Overnight"}


class TestSiteClassification(unittest.TestCase):
    def test_management_sites_are_excluded_from_both_sides(self):
        """Camp-host pitches are nobody's to book.

        At Trillium Lake 11 non-reservable sites are MANAGEMENT. Counting them
        would offer a camper sites that are somebody else's job.
        """
        counts = inventory.classify_sites([
            ridb_site("1"), ridb_site("2"),
            ridb_site("3", reservable=False),
            ridb_site("host", reservable=False, site_type="MANAGEMENT"),
            ridb_site("host2", reservable=False, site_type="MANAGEMENT"),
        ])
        self.assertEqual(counts.total, 3)            # management not counted
        self.assertEqual(counts.bookable, 2)
        self.assertEqual(counts.not_bookable, 1)
        self.assertEqual(counts.management, 2)
        self.assertTrue(counts.has_unbookable_sites)

    def test_a_fully_bookable_campground_reports_none_unbookable(self):
        counts = inventory.classify_sites([ridb_site("1"), ridb_site("2")])
        self.assertEqual(counts.not_bookable, 0)
        self.assertFalse(counts.has_unbookable_sites)


class TestSiteInventoryBackfill(DBTestCase):
    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="RecreationDotGov", id="mixed", name="Mixed",
                       state="OR"),
            Campground(provider="RecreationDotGov", id="pure", name="Pure",
                       state="OR"),
            Campground(provider="RecreationDotGov", id="empty", name="No Inventory",
                       state="OR", reservation_type="first_come"),
        ], now=NOW)
        self.data = {
            "mixed": [ridb_site("a"), ridb_site("b", reservable=False),
                      ridb_site("h", reservable=False, site_type="MANAGEMENT")],
            "pure": [ridb_site("a"), ridb_site("b")],
            "empty": [],
        }

    def fetcher(self, overrides=None):
        seen = []

        def fetch(path, params):
            seen.append(path)
            fid = path.split("/")[1]
            recs = (overrides or self.data).get(fid, [])
            offset = params.get("offset", 0)
            page = recs[offset:offset + params["limit"]]
            return {"RECDATA": page,
                    "METADATA": {"RESULTS": {"TOTAL_COUNT": len(recs)}}}
        fetch.seen = seen
        return fetch

    def test_counts_are_recorded_and_the_flag_is_derived_from_them(self):
        f = self.fetcher()
        report = inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=f, now=NOW)
        self.assertEqual(report.recorded, 2)
        mixed = store.get_campground(self.conn, "RecreationDotGov", "mixed")
        self.assertEqual(mixed.sites_total, 2)          # management excluded
        self.assertEqual(mixed.sites_not_bookable, 1)
        self.assertIs(mixed.first_come_sites, True)
        pure = store.get_campground(self.conn, "RecreationDotGov", "pure")
        self.assertEqual(pure.sites_not_bookable, 0)
        self.assertIs(pure.first_come_sites, False)     # a real answer, not unknown

    def test_a_facility_with_no_site_list_stays_unknown_not_zero(self):
        """"RIDB doesn't know" and "there are no sites" are different claims.

        Every first-come facility returns zero campsite records, because RIDB's
        inventory comes from the reservation system.
        """
        report = inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=self.fetcher(), now=NOW)
        self.assertIn("No Inventory", report.no_inventory)
        cg = store.get_campground(self.conn, "RecreationDotGov", "empty")
        self.assertIsNone(cg.first_come_sites)
        self.assertIsNone(cg.sites_total)

    def test_it_uses_the_nested_path_not_the_ignored_query_parameter(self):
        # campsites?facilityID=X returns all 137,117 campsites in RIDB.
        f = self.fetcher()
        inventory.backfill_site_inventory(self.conn, states=["OR"], fetcher=f, now=NOW)
        self.assertTrue(all(p.startswith("facilities/") and p.endswith("/campsites")
                            for p in f.seen), f.seen)

    def test_a_short_page_raises_rather_than_undercounting(self):
        def liar(path, params):
            return {"RECDATA": [ridb_site("only-one")],
                    "METADATA": {"RESULTS": {"TOTAL_COUNT": 99}}}
        with self.assertRaises(inventory.IncompleteInventory):
            inventory.fetch_facility_campsites("mixed", fetcher=liar)

    def test_a_facility_error_is_recorded_and_does_not_stop_the_run(self):
        def flaky(path, params):
            if "pure" in path:
                raise RuntimeError("upstream 500")
            return self.fetcher()(path, params)
        report = inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=flaky, now=NOW)
        self.assertIn("Pure", report.errors)
        self.assertEqual(
            store.get_campground(self.conn, "RecreationDotGov", "mixed").sites_total, 2)

    def test_rerunning_only_fills_blanks(self):
        # Measured once and kept: inventory changes on the order of years.
        f = self.fetcher()
        inventory.backfill_site_inventory(self.conn, states=["OR"], fetcher=f, now=NOW)
        before = len(f.seen)
        second = inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=f, now=NOW)
        self.assertEqual(second.recorded, 0)
        # Only the still-unknown facility is re-queried.
        self.assertLess(len(f.seen) - before, before)

    def test_the_label_states_what_was_measured_and_nothing_more(self):
        cg = Campground(provider="p", id="1", name="n", first_come_sites=True,
                        sites_total=279, sites_not_bookable=131)
        label = cg.booking_label
        self.assertIn("131 of 279", label)
        self.assertIn("aren't bookable online", label)
        # Must NOT claim they are first-come or available.
        self.assertNotIn("first-come sites", label)
        self.assertNotIn("available", label)

    def test_counts_survive_the_seed_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.json"
            catalog.write_seed([Campground(
                provider="RecreationDotGov", id="mixed", name="Mixed",
                first_come_sites=True, sites_total=279,
                sites_not_bookable=131)], path=path)
            back = catalog.load_seed(path)[0]
        self.assertEqual(back.sites_total, 279)
        self.assertEqual(back.sites_not_bookable, 131)
        self.assertIs(back.first_come_sites, True)

# ------------- RA changed its markup and every park read "full" (2026-07-28) ----

class TestMatrixMarkupChange(unittest.TestCase):
    """ReserveAmerica appended `&lengthOfStay=1` to every matrix link.

    The old pattern required the arrival date to be followed immediately by the
    closing quote, so it matched 150 cells in the 2026-07-27 capture and ZERO
    live — while the page still advertised 138 available cells. Silently, every
    Oregon park read as full.
    """

    def setUp(self):
        from app.providers.reserveamerica import ReserveAmericaProvider
        self.parse = ReserveAmericaProvider.parse_park_matrix

    def test_both_link_shapes_parse(self):
        old = (FIXTURES / "ra_park_412704_matrix.html").read_text()
        new = (FIXTURES / "ra_park_412704_matrix_v2.html").read_text()
        self.assertGreater(len(self.parse(old)), 0, "the original capture")
        self.assertGreater(len(self.parse(new)), 0, "after &lengthOfStay=1 appeared")

    def test_extra_query_parameters_do_not_break_it(self):
        old = (FIXTURES / "ra_park_412704_matrix.html").read_text()
        mutated = old.replace("arvdate=8/10/2026",
                              "arvdate=8/10/2026&amp;lengthOfStay=1&amp;more=x")
        self.assertEqual(len(self.parse(mutated)), len(self.parse(old)))

    def test_an_unreadable_grid_raises_instead_of_reporting_full(self):
        """The guard this class of bug earned.

        An empty result means "this park is full" to every caller downstream,
        which is the answer that quietly sends someone somewhere else. If the
        page shows available cells and none parse, that is a parser failure and
        it must say so.
        """
        from app.providers.reserveamerica import MatrixParseError
        old = (FIXTURES / "ra_park_412704_matrix.html").read_text()
        unreadable = old.replace("aria-label='A for", "aria-label='Available for")
        self.assertIn("td status a", unreadable)
        with self.assertRaises(MatrixParseError):
            self.parse(unreadable)

    def test_a_genuinely_full_park_still_reports_empty(self):
        # No available cells at all is a real answer, not an error.
        old = (FIXTURES / "ra_park_412704_matrix.html").read_text()
        full = old.replace("class='td status a'", "class='td status r'")
        self.assertEqual(self.parse(full), {})

# --------------- the park page shows 25 of 279 sites (found 2026-07-28) ----

class TestSiteListPaging(unittest.TestCase):
    """Beverly Beach looked like a 27-site park. It has 279.

    `campgroundDetails.do` shows only the first 25 rows and silently ignores
    `startIdx`. The real control is a separate endpoint, taken from the page's
    own Next link:

        executePaging("/campsitePaging.do?...&startIdx=25")

    Until this was found, the entire C loop was invisible — which is why site
    C29 could not be looked up at all.
    """

    def setUp(self):
        from app.providers.reserveamerica import ReserveAmericaProvider
        self.cls = ReserveAmericaProvider

    def rows(self, names, total, base=0):
        # Ids are unique per position across the whole park, as a real park's
        # are. Colliding ids would dedupe pages together and make the walk look
        # short — which is exactly what the guard is there to catch.
        def site_id(offset):
            return 1000 + base + offset

        cells = "".join(
            f"<div class='br'><div class='td'>Map {n}</div>"
            f"<div class='td'>C</div><div class='td'>TENT SITE</div>"
            f"<div class='td maxPeopleCell'>8</div><div class='td'>20 Back-In</div>"
            f"<div class='td amenitiesicons'></div>"
            f"<div class='td sitescompareselectorbtn{site_id(k)}'>Enter Date</div></div>"
            for k, n in enumerate(names))
        return (f"<div class='matchSummary mb5'>{total} site(s) found</div>"
                + cells + "<div class='tfoot'></div>")

    def provider(self, pages, total):
        calls = []

        def fetch(path, params):
            calls.append((path, params.get("startIdx")))
            idx = params.get("startIdx", 0) or 0
            chunk = pages[idx // 25] if idx // 25 < len(pages) else []
            return self.rows(chunk, total, base=idx)
        p = self.cls("OR", "oregonstateparks.reserveamerica.com",
                     delay=0, fetcher=fetch)
        return p, calls

    def test_it_walks_every_page(self):
        pages = [[f"C{i:02d}" for i in range(1, 26)],
                 [f"D{i:02d}" for i in range(1, 26)],
                 [f"E{i:02d}" for i in range(1, 4)]]
        p, calls = self.provider(pages, total=53)
        sites = p.list_sites("402126")
        self.assertEqual(len(sites), 53)
        self.assertIn("campsitePaging.do", [c[0] for c in calls])
        # First request is the park page; the rest go to the paging endpoint.
        self.assertEqual(calls[0][0], "campgroundDetails.do")
        self.assertTrue(all(c[0] == "campsitePaging.do" for c in calls[1:]))

    def test_a_short_walk_raises_rather_than_reporting_a_partial_park(self):
        from app.providers.reserveamerica import IncompleteSiteList
        # The page claims 279 but only one page of rows is ever served.
        p, _ = self.provider([[f"C{i:02d}" for i in range(1, 26)]], total=279)
        with self.assertRaises(IncompleteSiteList):
            p.list_sites("402126")

    def test_a_single_page_park_makes_no_paging_request(self):
        p, calls = self.provider([[f"A{i:02d}" for i in range(1, 6)]], total=5)
        self.assertEqual(len(p.list_sites("412704")), 5)
        self.assertEqual([c[0] for c in calls], ["campgroundDetails.do"])

# ------------- the driveway length is a floor, not a measurement (2026-07-28) ----

class TestDrivewayIsAMinimum(unittest.TestCase):
    """A Beverly Beach manager told Scott the sites were never measured.

    When the park went onto ReserveAmerica they had no staffing to measure, so
    most sites were entered at a default. Ground truth: A01 is listed
    `20 Back-In` and is really 53 feet; A15, genuinely small, was entered
    accurately at 15. On that page 21 of 24 sites read exactly "20 Back-In".

    So the number is a FLOOR. A rig longer than the listing may still fit, and
    saying "no" would hide a site that works.
    """

    def setUp(self):
        from app.providers.reserveamerica import fits_equipment, parse_driveway
        self.fits, self.parse = fits_equipment, parse_driveway

    def test_the_cell_splits_into_feet_and_manoeuvre(self):
        self.assertEqual(self.parse("20 Back-In"), (20, "Back-In"))
        self.assertEqual(self.parse("Pull-Through"), (None, "Pull-Through"))
        self.assertEqual(self.parse(""), (None, None))

    def test_a_listing_at_or_above_the_need_fits(self):
        self.assertIs(self.fits("30 Back-In", 25), True)
        self.assertIs(self.fits("15 Back-In", 15), True)

    def test_a_listing_below_the_need_is_unknown_not_no(self):
        # A01 lists 20 and is really 53. Answering "no" would hide it.
        self.assertIsNone(self.fits("20 Back-In", 25))
        self.assertIsNone(self.fits("20 Back-In", 50))

    def test_a_driveway_with_no_number_is_unknown(self):
        self.assertIsNone(self.fits("Back-In", 25))

    def test_an_empty_driveway_is_the_one_confident_no(self):
        # WALK TO sites. No driveway at all means no vehicle reaches it.
        self.assertIs(self.fits("", 25), False)
        self.assertIs(self.fits("", 1), False)

    def test_the_real_beverly_beach_page_behaves_as_described(self):
        from app.providers.reserveamerica import ReserveAmericaProvider
        sites = {s["name"]: s for s in ReserveAmericaProvider.parse_sites(
            (FIXTURES / "ra_beverly_loop_a.html").read_text())}
        self.assertEqual(sites["A01"]["equipment_length"], "20 Back-In")
        self.assertEqual(sites["A15"]["equipment_length"], "15 Back-In")
        # A01 is really 53ft, so a 40ft rig must not be told "no".
        self.assertIsNone(self.fits(sites["A01"]["equipment_length"], 40))

# ---------------- trusting a length by how often it repeats (2026-07-28) ----

class TestDefaultLengthDetection(unittest.TestCase):
    """Scott's tell: no real campground has most sites the exact same length.

    A forested loop bends around trees; identical figures across a park are a
    form default, not a measurement. So a repeated value is untrustworthy and a
    rare one was entered deliberately and should be believed.
    """

    def setUp(self):
        from app import equipment
        from app.providers.reserveamerica import ReserveAmericaProvider
        self.eq = equipment
        self.sites = ReserveAmericaProvider.parse_sites(
            (FIXTURES / "ra_beverly_loop_a.html").read_text())

    def test_the_repeated_value_is_flagged_as_a_default(self):
        from app.providers.reserveamerica import parse_driveway
        stated = [parse_driveway(s["equipment_length"])[0] for s in self.sites]
        self.assertEqual(self.eq.default_lengths(stated), {20})

    def test_a_small_sample_yields_no_verdict(self):
        # Three sites reading 20 is not evidence of anything.
        self.assertEqual(self.eq.default_lengths([20, 20, 20]), set())

    def test_a_varied_park_has_no_default(self):
        self.assertEqual(
            self.eq.default_lengths([15, 20, 22, 28, 31, 35, 40, 44]), set())

    def test_a_default_below_the_need_is_unknown_never_no(self):
        # A01 lists 20 and is really 53ft.
        r = self.eq.read_length(20, True, {20})
        self.assertIsNone(self.eq.fits(r, 40))

    def test_a_specific_value_below_the_need_is_believed(self):
        # A15 lists 15 and really is 15ft.
        r = self.eq.read_length(15, True, {20})
        self.assertIs(self.eq.fits(r, 40), False)

    def test_a_forty_foot_rig_gets_no_false_candidates(self):
        """The point Scott made: never show "minimum 20" to a 40ft RV.

        Merging unknowns into the results would hand a 40-footer 21 sites
        listed at a default 20 as though they were matches.
        """
        fit, unknown, no = self.eq.filter_by_length(self.sites, 40)
        self.assertEqual(len(fit), 0, "nothing here is confirmed to fit 40ft")
        self.assertEqual(len(unknown), 21)
        self.assertIn("A15", [s["name"] for s in no])

    def test_unknowns_are_kept_separate_not_discarded(self):
        # §8g: unknown is shown, never hidden — but never as a match either.
        fit, unknown, no = self.eq.filter_by_length(self.sites, 40)
        self.assertEqual(len(fit) + len(unknown) + len(no), len(self.sites))
        self.assertIn("A01", [s["name"] for s in unknown])

    def test_a_confident_fit_is_still_reported(self):
        fit, _unknown, _no = self.eq.filter_by_length(self.sites, 25)
        self.assertEqual([s["name"] for s in fit], ["A19"])   # listed 30ft

    def test_the_copy_does_not_encourage_a_forty_foot_rig(self):
        r = self.eq.read_length(20, True, {20})
        text = self.eq.describe(r, 40)
        self.assertIn("not reliably recorded", text)
        self.assertNotIn("minimum", text.lower())
        self.assertNotIn("may be longer", text.lower())

# --------------------- a site that differs from its own loop (2026-07-28) ----

class TestLoopOutliers(unittest.TestCase):
    """Beverly Beach A19, read off the page by Scott.

    It is the only STANDARD site in a loop of tent sites, the only one with
    power and water, and the longest driveway — and the map shows it by the
    hiker/biker camps. His read: probably the camp host pitch.

    It also happened to be the SINGLE confirmed fit for a 25 ft rig in that
    loop. A lone confident answer that turns out to be a host pitch is worse
    than no answer, so it has to carry its caveat.
    """

    def setUp(self):
        from app import equipment
        from app.providers.reserveamerica import ReserveAmericaProvider
        self.eq = equipment
        self.sites = ReserveAmericaProvider.parse_sites(
            (FIXTURES / "ra_beverly_loop_a.html").read_text())
        self.by_name = {s["name"]: s for s in self.sites}

    def test_amenities_are_captured_at_all(self):
        # They were being parsed and thrown away.
        self.assertIn("Electric Hookup available: 30 amp",
                      self.by_name["A19"]["amenities"])
        self.assertIn("Electric Hookup - no", self.by_name["A01"]["amenities"])

    def test_the_map_link_is_not_mistaken_for_an_amenity(self):
        for site in self.sites:
            for amenity in site["amenities"]:
                self.assertNotIn("on map", amenity.lower())

    def test_a_negated_amenity_does_not_count_as_a_service(self):
        # "Electric Hookup - no" must not read as "has electric".
        self.assertFalse(self.eq._has_service(self.by_name["A01"]["amenities"]))
        self.assertTrue(self.eq._has_service(self.by_name["A19"]["amenities"]))

    def test_the_lone_serviced_site_is_flagged(self):
        notes = self.eq.loop_outliers(self.sites)
        a19 = self.by_name["A19"]["site_id"]
        self.assertIn(a19, notes)
        self.assertTrue(any("hookups" in n for n in notes[a19]))
        self.assertTrue(any("STANDARD" in n for n in notes[a19]))

    def test_ordinary_sites_are_not_flagged(self):
        notes = self.eq.loop_outliers(self.sites)
        self.assertNotIn(self.by_name["A01"]["site_id"], notes)

    def test_the_caveat_hedges_rather_than_asserting_camp_host(self):
        """We observe the anomaly; we do not claim the cause.

        "Camp host" needs a map and local knowledge. Asserting it from this
        data would be the confident guess the rest of the codebase refuses.
        """
        notes = self.eq.loop_outliers(self.sites)
        text = self.eq.outlier_caveat(notes[self.by_name["A19"]["site_id"]])
        self.assertIn("sometimes", text)
        self.assertIn("worth checking", text)
        self.assertNotIn("is the camp host", text)

    def test_a_small_loop_makes_no_claim(self):
        # Two sites differing from each other proves nothing.
        tiny = [{"site_id": "1", "loop": "Z", "site_type": "TENT SITE", "amenities": []},
                {"site_id": "2", "loop": "Z", "site_type": "STANDARD",
                 "amenities": ["Electric Hookup available: 30 amp"]}]
        self.assertEqual(self.eq.loop_outliers(tiny), {})

# ------------- per-site inventory, and trusting lengths per campground ----

def ridb_full_site(site_id, name, length=None, access=None, reservable=True,
                   site_type="STANDARD NONELECTRIC", loop="A", lat=45.0, lon=-122.0):
    attrs = [{"AttributeName": "Max Num of People", "AttributeValue": "6"}]
    if length is not None:
        attrs.append({"AttributeName": "Max Vehicle Length",
                      "AttributeValue": str(length)})
    if access:
        attrs.append({"AttributeName": "Site Access", "AttributeValue": access})
    return {
        "CampsiteID": site_id, "CampsiteName": name, "Loop": loop,
        "CampsiteType": site_type, "TypeOfUse": "Overnight",
        "CampsiteReservable": reservable, "CampsiteAccessible": False,
        "CampsiteLatitude": lat, "CampsiteLongitude": lon,
        "PERMITTEDEQUIPMENT": [{"EquipmentName": "Tent", "MaxLength": 0}],
        "ATTRIBUTES": attrs,
    }


class TestPerSiteInventory(DBTestCase):
    """The backfill keeps per-site records, not just a count.

    RIDB states Max Vehicle Length, Site Access (Drive-In / Hike-In), Driveway
    Entry, Loop and per-site coordinates for every site. Spending 545
    facilities' worth of requests and storing a single number would throw all
    of that away — the same mistake caught earlier with CampsiteType.
    """

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="RecreationDotGov", id="varied", name="Varied",
                       state="OR"),
            Campground(provider="RecreationDotGov", id="lazy", name="Lazy", state="OR"),
        ], now=NOW)
        self.data = {
            # Genuinely varied — somebody measured these.
            "varied": [ridb_full_site(i, f"S{i}", length=L)
                       for i, L in enumerate([18, 26, 32, 33, 36, 38, 39, 40], 1)],
            # One figure repeated — a form default.
            "lazy": [ridb_full_site(i, f"L{i}", length=20) for i in range(1, 9)],
        }

    def fetcher(self):
        def fetch(path, params):
            fid = path.split("/")[1]
            recs = self.data.get(fid, [])
            off = params.get("offset", 0)
            return {"RECDATA": recs[off:off + params["limit"]],
                    "METADATA": {"RESULTS": {"TOTAL_COUNT": len(recs)}}}
        return fetch

    def test_site_rows_are_written(self):
        inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=self.fetcher(), now=NOW)
        rows = store.list_campsites(self.conn, "RecreationDotGov", "varied")
        self.assertEqual(len(rows), 8)
        self.assertEqual(sorted(r["max_vehicle_length"] for r in rows),
                         [18, 26, 32, 33, 36, 38, 39, 40])

    def test_per_site_coordinates_are_kept(self):
        inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=self.fetcher(), now=NOW)
        rows = store.list_campsites(self.conn, "RecreationDotGov", "varied")
        self.assertTrue(all(r["latitude"] is not None for r in rows))

    def test_the_access_axis_is_captured_when_stated(self):
        self.data["varied"][0] = ridb_full_site(1, "S1", length=18, access="Hike-In")
        inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=self.fetcher(), now=NOW)
        rows = {r["name"]: r for r in
                store.list_campsites(self.conn, "RecreationDotGov", "varied")}
        self.assertEqual(rows["S1"]["site_access"], "Hike-In")
        # Unstated stays unstated — never assumed to be Drive-In.
        self.assertIsNone(rows["S2"]["site_access"])

    def test_a_zero_length_upstream_means_not_stated(self):
        self.data["varied"] = [ridb_full_site(1, "Z", length=0)]
        inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=self.fetcher(), now=NOW)
        rows = store.list_campsites(self.conn, "RecreationDotGov", "varied")
        self.assertIsNone(rows[0]["max_vehicle_length"])

    def test_length_quality_is_graded_per_campground_not_per_provider(self):
        """Both are RecreationDotGov. One measured its sites; one did not."""
        inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=self.fetcher(), now=NOW)
        varied = store.get_campground(self.conn, "RecreationDotGov", "varied")
        lazy = store.get_campground(self.conn, "RecreationDotGov", "lazy")
        self.assertEqual(varied.provider, lazy.provider)
        self.assertEqual(varied.length_data_quality, "measured")
        self.assertEqual(lazy.length_data_quality, "default")

    def test_a_campground_with_site_rows_is_not_refetched(self):
        f = self.fetcher()
        calls = []

        def counting(path, params):
            calls.append(path)
            return f(path, params)
        inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=counting, now=NOW)
        before = len(calls)
        second = inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=counting, now=NOW)
        self.assertEqual(second.visited, 0)
        self.assertEqual(len(calls), before)

    def test_resume_is_keyed_on_site_rows_not_the_old_count_flag(self):
        # Campgrounds backfilled by the earlier counts-only pass were committed
        # to the seed. Testing the old flag would skip exactly the ones that
        # still need the richer pass.
        store.set_site_inventory(self.conn, "RecreationDotGov", "varied",
                                 sites_total=8, sites_not_bookable=0,
                                 source="old counts-only pass", now=NOW)
        self.assertIsNotNone(
            store.get_campground(self.conn, "RecreationDotGov", "varied").first_come_sites)
        report = inventory.backfill_site_inventory(
            self.conn, states=["OR"], fetcher=self.fetcher(), now=NOW)
        self.assertEqual(report.recorded, 2, "the counts-only campground is redone")

# ------------- ReserveAmerica per-site inventory (2026-07-31) -------------------

class TestReserveAmericaInventory(DBTestCase):
    """Oregon's state parks get the same per-site treatment as the federal ones.

    Everything in docs/terminology.md — the driveway floor, `WALK TO` as
    access mode, the lying icon, host pitches named rather than typed — was
    learned from these parks, and until this backfill ran it all applied to
    zero stored rows.
    """

    PROVIDER = "ReserveAmerica:OR"

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider=self.PROVIDER, id="412704", name="Reehers",
                       state="OR"),
            Campground(provider=self.PROVIDER, id="stub", name="Stub Stewart",
                       state="OR"),
        ], now=NOW)
        self.pages = {
            "412704": (FIXTURES / "ra_park_412704.html").read_text(),
            "stub": (FIXTURES / "ra_stub_stewart_sites.html").read_text(),
        }

    def fetcher(self):
        from app.providers.reserveamerica import ReserveAmericaProvider

        return lambda cg: ReserveAmericaProvider.parse_sites(self.pages[cg.id])

    def run_backfill(self):
        return inventory.backfill_site_inventory(
            self.conn, provider=self.PROVIDER, fetcher=self.fetcher(), now=NOW)

    def test_site_rows_are_written_for_an_ra_park(self):
        report = self.run_backfill()
        self.assertEqual(report.recorded, 2)
        rows = store.list_campsites(self.conn, self.PROVIDER, "412704")
        self.assertTrue(rows, "ReserveAmerica parks must get per-site rows")

    def test_the_host_pitch_is_excluded_even_though_it_is_typed_horse_site(self):
        """Reehers names its host site `Host` and types it `HORSE SITE`.

        A type-only exclusion — the shape that works for RIDB's MANAGEMENT —
        walks straight past it and offers a camper somebody's job.
        """
        self.run_backfill()
        rows = store.list_campsites(self.conn, self.PROVIDER, "412704")
        self.assertEqual(len(rows), 16, "17 sites minus the host pitch")
        self.assertNotIn("Host", [r["name"] for r in rows])

    def test_walk_to_becomes_hike_in_and_carries_no_driveway(self):
        self.run_backfill()
        rows = store.list_campsites(self.conn, self.PROVIDER, "stub")
        walk = [r for r in rows if r["site_type"] == "WALK TO"]
        self.assertEqual(len(walk), 21)
        self.assertTrue(all(r["access_class"] == "hike_in" for r in walk))
        # The second, independent signal: no driveway at all.
        self.assertTrue(all(r["driveway_entry"] is None for r in walk))

    def test_the_type_icon_is_stored_but_never_as_access(self):
        """Those WALK TO sites carry an `rv` icon. Believing it would be wrong."""
        self.run_backfill()
        rows = store.list_campsites(self.conn, self.PROVIDER, "stub")
        walk = next(r for r in rows if r["site_type"] == "WALK TO")
        attrs = json.loads(walk["attributes"])
        self.assertEqual(attrs["untrusted_type_icon"], "rv")
        self.assertEqual(walk["access_class"], "hike_in")

    def test_a_bare_manoeuvre_means_driveway_yes_length_unknown(self):
        """Reehers reads `Back-In` with no number — that is not zero feet."""
        self.run_backfill()
        rows = store.list_campsites(self.conn, self.PROVIDER, "412704")
        horse = next(r for r in rows if r["name"] == "A-01")
        self.assertEqual(horse["driveway_entry"], "Back-In")
        self.assertIsNone(horse["max_vehicle_length"])

    def test_absent_hookups_are_not_read_as_present(self):
        """`Electric Hookup - no` beside `... available: 30 amp`.

        A contains-check for "hookup" marks every unserviced site as serviced.
        """
        self.run_backfill()
        rows = store.list_campsites(self.conn, self.PROVIDER, "412704")
        attrs = json.loads(rows[0]["attributes"])
        self.assertEqual(attrs["Electric Hookup"], "no")

    def test_the_available_suffix_does_not_split_a_feature_in_two(self):
        """`Full Hookup available` and `Full Hookup - no` are one feature.

        Found by surveying all 64 Oregon parks (2026-07-31): the same hookup
        answered `no` under "Full Hookup" and `yes` under "Full Hookup
        available", so a filter reading either key got half the truth.
        """
        parsed = inventory.parse_ra_amenities([
            "Full Hookup available",
            "Electric Hookup available: 30 amp",
            "Near Water - no",
            "Water Hookup",
        ])
        self.assertEqual(parsed["Full Hookup"], "yes")
        self.assertNotIn("Full Hookup available", parsed)
        self.assertEqual(parsed["Electric Hookup"], "30 amp")
        self.assertEqual(parsed["Near Water"], "no")
        self.assertEqual(parsed["Water Hookup"], "yes")

    def test_bookability_stays_unknown_rather_than_zero(self):
        """The park page has no such column — its last cell reads "Enter Date".

        Recording 0 would assert every site is bookable online, which nobody
        measured. `first_come_sites` must stay NULL, not become False.
        """
        self.run_backfill()
        cg = store.get_campground(self.conn, self.PROVIDER, "412704")
        self.assertEqual(cg.sites_total, 16)
        self.assertIsNone(cg.sites_not_bookable)
        self.assertIsNone(cg.first_come_sites)

    def test_rerunning_skips_parks_already_held(self):
        """65 parks at 6s a request — a stopped run must resume, not restart."""
        self.run_backfill()
        second = self.run_backfill()
        self.assertEqual(second.visited, 0)

    def test_an_unimplemented_provider_refuses_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            inventory.backfill_site_inventory(self.conn, provider="GoingToCamp:WA")


# ------------- burn bans and closures (2026-07-31) ------------------------------

class TestParkAlerts(DBTestCase):
    """parks.wa.gov publishes every park's burn ban on one server-rendered page."""

    def fixture(self):
        return (FIXTURES / "wa_park_alerts.html").read_text()

    def test_alerts_are_parsed_with_their_park_level_and_date(self):
        from app import alerts
        got = alerts.parse_alerts(self.fixture())
        alta = next(a for a in got if a.park_name.startswith("Alta Lake"))
        self.assertEqual(alta.alert_type, "Burn Ban")
        self.assertEqual(alta.level, "3")
        self.assertTrue(alta.posted.startswith("2026-07-07"))
        self.assertIn("No charcoal or wood fires", alta.text)

    def test_a_standing_no_fires_rule_is_not_a_level(self):
        """Many marine parks carry this year-round, dated 2024 — not an emergency."""
        from app import alerts
        got = alerts.parse_alerts(self.fixture())
        anderson = next(a for a in got
                        if a.park_name.startswith("Anderson") and a.is_burn_ban)
        self.assertEqual(anderson.level, "no fires at any time")

    def test_every_alert_belongs_to_its_own_park(self):
        """Parsed by walking each park's block, not by hunting headings.

        A regex that found alerts first would attribute one to whichever park
        name it happened to see last — wrong in a way that looks fine until
        somebody drives somewhere.
        """
        from app import alerts
        by_park = {}
        for a in alerts.parse_alerts(self.fixture()):
            by_park.setdefault(a.park_name, []).append(a.alert_type)
        self.assertIn("Water Closure", by_park["Anderson Lake State Park"])
        self.assertNotIn("Water Closure", by_park["Alta Lake State Park"])

    def test_one_park_alert_reaches_all_that_parks_campgrounds(self):
        """Riverside posts one ban; we hold two of its campgrounds.

        Picking one would leave the other silently unwarned.
        """
        from app import alerts
        store.upsert_campgrounds(self.conn, [
            Campground(provider="GoingToCamp:WA", id="1",
                       name="Riverside State Park - Bowl and Pitcher", state="WA"),
            Campground(provider="GoingToCamp:WA", id="2",
                       name="Riverside State Park - Lake Spokane", state="WA"),
        ], now=NOW)
        one = alerts.Alert(park_slug="riverside", park_name="Riverside State Park",
                           alert_type="Burn Ban", level="3", posted=None, text="x")
        matched, unmatched = alerts.match_to_catalog(self.conn, [one])
        self.assertEqual(sorted(matched), ["1", "2"])
        self.assertEqual(unmatched, [])

    def test_two_similar_parks_are_not_merged(self):
        """Lewis and Clark, and Lewis and Clark Trail, are different parks."""
        from app import alerts
        store.upsert_campgrounds(self.conn, [
            Campground(provider="GoingToCamp:WA", id="a",
                       name="Lewis and Clark State Park (SW Washington)", state="WA"),
            Campground(provider="GoingToCamp:WA", id="b",
                       name="Lewis and Clark Trail State Park (SE Washington)",
                       state="WA"),
        ], now=NOW)
        trail = alerts.Alert(park_slug="t", park_name="Lewis and Clark Trail",
                             alert_type="Burn Ban", level="4", posted=None, text="x")
        matched, _ = alerts.match_to_catalog(self.conn, [trail])
        self.assertEqual(list(matched), ["b"], "the ban must not land on both")

    def test_unmatched_park_names_are_returned_not_swallowed(self):
        """An unmatched park is a park whose burn ban we are not showing."""
        from app import alerts
        stray = alerts.Alert(park_slug="x", park_name="Nowhere State Park",
                             alert_type="Burn Ban", level="2", posted=None, text="x")
        matched, unmatched = alerts.match_to_catalog(self.conn, [stray])
        self.assertEqual(matched, {})
        self.assertEqual(unmatched, ["Nowhere State Park"])

    def test_a_lifted_ban_disappears_on_refresh(self):
        """Alerts are the one thing here allowed to be deleted.

        A ban that has been lifted is taken off the operator's page; merging
        would leave it on our map telling somebody they can't light a fire
        that is now legal.
        """
        store.replace_park_alerts(self.conn, "GoingToCamp:WA",
                                  [("1", "Burn Ban", "4", None, "old", "2026-07-31")])
        self.assertEqual(len(store.alerts_for(self.conn, "GoingToCamp:WA", "1")), 1)
        store.replace_park_alerts(self.conn, "GoingToCamp:WA", [])
        self.assertEqual(store.alerts_for(self.conn, "GoingToCamp:WA", "1"), [])


# ------------- openings joined to what the site actually is (2026-07-31) --------

class TestOpeningsJoin(DBTestCase):
    """`availability` says when; `campsites` says what. Nothing joined them.

    Until this, 5,413 Oregon sites' worth of driveway lengths and `WALK TO`
    flags applied to nothing at all.
    """

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="ReserveAmerica:OR", id="412704",
                       name="Reehers", state="OR"),
            Campground(provider="GoingToCamp:WA", id="-99", name="Alta Lake",
                       state="WA"),
        ], now=NOW)
        store.upsert_campsites(self.conn, "ReserveAmerica:OR", "412704", [
            {"site_id": "45859", "name": "A-01", "site_type": "HORSE SITE",
             "driveway_entry": "Back-In", "max_vehicle_length": 40,
             "access_class": None, "max_people": 8},
            {"site_id": "17108", "name": "HIKE 01", "site_type": "WALK TO",
             "driveway_entry": None, "max_vehicle_length": None,
             "access_class": "hike_in", "max_people": 6},
        ], source="test", now=NOW)
        # Openings: two ReserveAmerica (joinable) and one Washington (not).
        store.upsert_availability(self.conn, [
            Campsite(provider="ReserveAmerica:OR", campsite_id="45859",
                     available_date=START, nights=2, site_name="A-01",
                     facility_id="412704", state="OR"),
            Campsite(provider="ReserveAmerica:OR", campsite_id="17108",
                     available_date=START, nights=2, site_name="HIKE 01",
                     facility_id="412704", state="OR"),
            Campsite(provider="GoingToCamp:WA", campsite_id="55555",
                     available_date=START, nights=2, site_name="55555",
                     facility_id="-99", state="WA"),
        ], now=NOW)

    def test_site_detail_rides_along_with_the_opening(self):
        rows = store.list_openings(self.conn, provider="ReserveAmerica:OR")
        by_id = {r["campsite_id"]: r for r in rows}
        self.assertEqual(by_id["45859"]["site_max_vehicle_length"], 40)
        self.assertEqual(by_id["17108"]["site_access_class"], "hike_in")

    def test_an_opening_with_no_site_row_survives_the_join(self):
        """LEFT, never INNER — this is the whole design.

        GoingToCamp holds no per-site rows, so an inner join would silently
        hide every Washington opening. A shorter answer that looks complete is
        this project's recurring bug (§8k).
        """
        rows = store.list_openings(self.conn)
        wa = [r for r in rows if r["provider"] == "GoingToCamp:WA"]
        self.assertEqual(len(wa), 1)
        self.assertIsNone(wa[0]["site_joined"])
        self.assertIsNone(wa[0]["site_max_vehicle_length"])

    def test_an_unjoined_opening_is_unknown_not_does_not_fit(self):
        """Getting this backwards hides a whole state from every RV owner."""
        from app import equipment
        rows = store.list_openings(self.conn)
        fits, unknown, no = equipment.filter_openings_by_length(rows, 30)
        wa = [r for r in unknown if r["provider"] == "GoingToCamp:WA"]
        self.assertEqual(len(wa), 1, "unmatched must be unknown")
        self.assertFalse([r for r in no if r["provider"] == "GoingToCamp:WA"])

    def test_a_site_with_no_driveway_is_the_one_honest_exclusion(self):
        from app import equipment
        rows = store.list_openings(self.conn, provider="ReserveAmerica:OR")
        _fits, _unknown, no = equipment.filter_openings_by_length(rows, 30)
        self.assertEqual([r["campsite_id"] for r in no], ["17108"])

    def test_access_filter_separates_matching_from_unclassified(self):
        from app import equipment
        rows = store.list_openings(self.conn)
        matching, unknown = equipment.filter_openings_by_access(rows, "hike_in")
        self.assertEqual([r["campsite_id"] for r in matching], ["17108"])
        self.assertIn("55555", [r["campsite_id"] for r in unknown])

    def test_coverage_is_reported_rather_than_assumed(self):
        """A filter that works on one provider and not another must be visible."""
        coverage = store.opening_site_coverage(self.conn)
        self.assertEqual(coverage["ReserveAmerica:OR"]["with_detail"], 2)
        self.assertEqual(coverage["GoingToCamp:WA"]["with_detail"], 0)


# ------------- GoingToCamp publishes the vocabulary (2026-07-31) ----------------

GTC_ATTR_DEFS = {
    "-32706": {
        "attributeDefinitionId": -32706,
        "localizedValues": [{"cultureName": "en-US", "displayName": "Park Amenities"}],
        # Deliberately NOT in enumValue order, and with a gap — this is the
        # shape that makes index-based decoding wrong.
        "values": [
            {"enumValue": 12, "order": 1,
             "localizedValues": [{"displayName": "Boat Launch"}]},
            {"enumValue": 0, "order": 2,
             "localizedValues": [{"displayName": "Wifi"}]},
            {"enumValue": 25, "order": 3,
             "localizedValues": [{"displayName": "Lakes/Rivers/Beach"}]},
        ],
    },
}


class TestGoingToCampAttributes(unittest.TestCase):
    """The platform states in words what the others make us infer."""

    def provider(self):
        from app.providers.goingtocamp import GoingToCampProvider

        p = GoingToCampProvider("WA", "example.invalid", 3, state="WA",
                                fetcher=lambda path, params: GTC_ATTR_DEFS)
        return p

    def test_values_decode_by_enum_not_by_position(self):
        """`enumValue`, never the index.

        The values arrive ordered by an `order` field that is not the enum, so
        indexing the array relabels every amenity silently — "Boat Launch"
        landing on whatever sits at that offset.
        """
        p = self.provider()
        decoded = p.decode_attributes(
            [{"attributeDefinitionId": -32706, "values": [12, 25]}])
        self.assertEqual(decoded["Park Amenities"], ["Boat Launch", "Lakes/Rivers/Beach"])

    def test_an_unknown_definition_is_dropped_not_guessed(self):
        p = self.provider()
        decoded = p.decode_attributes(
            [{"attributeDefinitionId": -99999, "values": [1]}])
        self.assertEqual(decoded, {})


class TestWaterDerivation(unittest.TestCase):
    """`yes` or `unknown` — and `no` only from a person who looked."""

    def test_operator_amenities_beat_a_silent_name(self):
        from app import water
        status, evidence = water.derive(
            "Battle Ground", None, ["Boat Launch", "Swimming"])
        self.assertEqual(status, "yes")
        self.assertIn("the operator lists", evidence)

    def test_moorage_counts_as_water(self):
        """GoingToCamp's word, not RIDB's. A marker set tuned to one provider
        silently drops the next one's terms."""
        from app import water
        self.assertEqual(water.derive("Somewhere", None, ["Moorage"])[0], "yes")

    def test_a_dry_campground_is_unknown_never_no(self):
        from app import water
        status, evidence = water.derive("Ridge Camp", None, ["HIKING"])
        self.assertEqual(status, "unknown")
        self.assertIsNone(evidence)

    def test_only_a_human_verdict_may_say_no(self):
        from app import water
        status, evidence = water.derive(
            "Lake Something", None, ["Swimming"],
            curated={"water": "no", "note": "the lake is 3 miles away by road"},
        )
        self.assertEqual(status, "no")
        self.assertIn("3 miles", evidence)

    def test_the_curated_verdict_also_overrides_a_yes_name(self):
        from app import water
        self.assertEqual(
            water.derive("Beach Camp", None, None, curated={"water": "no"})[0],
            "no")


# ------------- normalized access, and per-site data that survives (2026-07-28) ----

class TestAccessNormalization(unittest.TestCase):
    """The source spells the access mode several ways.

    Across 6,822 real site rows: "Drive-In" 2711, "Drive In" 36, "Hike-In" 152,
    "Hike In" 2, "Walk-In" 11, "N/A" 7. A filter matching exact strings would
    silently miss 49 genuine sites — the same shortfall shape as every other
    bug found today.
    """

    def test_every_observed_spelling_maps_to_a_class(self):
        from app.inventory import normalize_access
        self.assertEqual(normalize_access("Drive-In"), "drive_in")
        self.assertEqual(normalize_access("Drive In"), "drive_in")
        self.assertEqual(normalize_access("Hike-In"), "hike_in")
        self.assertEqual(normalize_access("Hike In"), "hike_in")

    def test_walk_in_and_hike_in_are_one_class(self):
        # Both mean "park elsewhere and carry your gear" — Scott's access axis.
        from app.inventory import normalize_access
        self.assertEqual(normalize_access("Walk-In"), normalize_access("Hike-In"))

    def test_unstated_stays_unstated(self):
        from app.inventory import normalize_access
        for value in (None, "", "   ", "N/A"):
            self.assertIsNone(normalize_access(value))


class TestCampsiteSeedSurvivesRebuild(DBTestCase):
    """Measured once, committed — never re-fetched on a fresh checkout.

    The per-site inventory cost 339 campgrounds' worth of paced requests. If it
    lived only in somebody's local database, every clone would pay for it again.
    """

    def test_the_committed_seed_holds_the_site_rows(self):
        rows = catalog.load_campsite_seed()
        self.assertGreater(len(rows), 6000)

    def test_a_fresh_database_loads_them(self):
        catalog.seed_campsites(self.conn, now=NOW)
        n = self.conn.execute("SELECT COUNT(*) FROM campsites").fetchone()[0]
        self.assertGreater(n, 6000)

    def test_the_access_class_survives_the_round_trip(self):
        catalog.seed_campsites(self.conn, now=NOW)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM campsites WHERE access_class='hike_in'"
        ).fetchone()[0]
        self.assertGreater(n, 100)

    def test_seeding_is_idempotent(self):
        catalog.seed_campsites(self.conn, now=NOW)
        first = self.conn.execute("SELECT COUNT(*) FROM campsites").fetchone()[0]
        catalog.seed_campsites(self.conn, now=NOW)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM campsites").fetchone()[0], first)

    def test_lengths_and_coordinates_survive(self):
        catalog.seed_campsites(self.conn, now=NOW)
        row = self.conn.execute(
            "SELECT * FROM campsites WHERE max_vehicle_length IS NOT NULL "
            "AND latitude IS NOT NULL LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(row["max_vehicle_length"], 0)

# ------------- a failure must not tar what the same cycle just read ----

class TestFailureDoesNotOverreach(DBTestCase):
    """Found by the first full end-to-end scan, 2026-07-28.

    One source failed — Gifford Pinchot's rec-area id is wrong — and that
    marked all 545 recreation.gov campgrounds `stale`, including Clackamas
    Lake, which had just returned 556 open site-nights from a different source.
    The map would have said "we couldn't check this" about a campground checked
    successfully seconds earlier.

    Same overreach as the success path had, left behind in the failure path.
    """

    def setUp(self):
        super().setUp()
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="good", name="Checked Fine", state="OR"),
            Campground(provider="Mock", id="elsewhere", name="Never Touched",
                       state="OR"),
        ], now=NOW)

    def test_a_campground_read_this_cycle_is_never_marked_stale(self):
        """Belt and braces: fresh availability outranks any staleness sweep."""
        store.upsert_availability(self.conn, [a_site(
            provider="Mock", campsite_id="s1", facility_id="good", state="OR")],
            now=NOW)

        class Boom(MockProvider):
            def search(self, req):
                raise RuntimeError("upstream 503")

        config = parse_config({"round_pause_seconds": 0, "sources": [
            {"label": "Broken", "provider": "Mock", "state": "OR"}]})
        scan_once(self.conn, config, notifier=Notifier([]), start=START,
                  window_days=3, now=NOW,
                  provider_factory=lambda spec, state=None, **kw: Boom(state=state))
        cg = store.get_campground(self.conn, "Mock", "good")
        self.assertNotEqual(cg.status, STATUS_STALE,
                            "556 open site-nights must not read as unchecked")

    def test_a_rec_area_source_failing_marks_nothing_stale(self):
        # We cannot tell which campgrounds a rec-area source covers, so we
        # cannot attribute its failure to any of them.
        class Boom(MockProvider):
            def search(self, req):
                raise RuntimeError("No campgrounds found to search")

        config = parse_config({"round_pause_seconds": 0, "sources": [
            {"label": "Gifford Pinchot NF", "provider": "Mock", "state": "OR",
             "rec_area_ids": ["1131"]}]})
        report = scan_once(
            self.conn, config, notifier=Notifier([]), start=START, window_days=3,
            now=NOW, provider_factory=lambda spec, state=None, **kw: Boom(state=state))
        self.assertIn("Gifford Pinchot NF", report.provider_errors)
        for cg_id in ("good", "elsewhere"):
            self.assertNotEqual(
                store.get_campground(self.conn, "Mock", cg_id).status, STATUS_STALE)

    def test_the_failure_is_still_reported_loudly(self):
        # Not marking things stale must not mean swallowing the error.
        class Boom(MockProvider):
            def search(self, req):
                raise RuntimeError("No campgrounds found to search")

        config = parse_config({"round_pause_seconds": 0, "sources": [
            {"label": "Broken", "provider": "Mock", "state": "OR",
             "rec_area_ids": ["1131"]}]})
        report = scan_once(
            self.conn, config, notifier=Notifier([]), start=START, window_days=3,
            now=NOW, provider_factory=lambda spec, state=None, **kw: Boom(state=state))
        self.assertIn("Broken", report.provider_errors)
        self.assertIn("No campgrounds found", report.provider_errors["Broken"])



class TestBasemapConfig(unittest.TestCase):
    """The tile source is config, not code (§8h).

    The build plan rules out plain OSM road tiles because they show no trails,
    so the default has to be topographic and swapping it must never mean
    editing JavaScript.
    """

    def test_the_default_basemap_is_not_plain_osm_road_tiles(self):
        tiles = parse_config({}).map_settings["tiles"]
        self.assertIn("opentopomap", tiles["url"])
        self.assertNotIn("tile.openstreetmap.org", tiles["url"])

    def test_a_partial_override_keeps_the_rest_of_the_defaults(self):
        # Overriding only the URL must not silently drop max_zoom — asking
        # a server for a zoom it doesn't render returns blank tiles, which
        # reads as "nothing is there".
        cfg = parse_config({"map": {"tiles": {"url": "https://example/{z}.png"}}})
        tiles = cfg.map_settings["tiles"]
        self.assertEqual(tiles["url"], "https://example/{z}.png")
        self.assertEqual(tiles["max_zoom"], 17)
        self.assertTrue(tiles["attribution"])

    def test_the_map_centres_on_home_when_not_told_otherwise(self):
        cfg = parse_config({"home_base": {"latitude": 45.52, "longitude": -122.68}})
        self.assertEqual(cfg.map_settings["center"], [45.52, -122.68])

    def test_an_explicit_centre_wins_and_accepts_either_shape(self):
        as_list = parse_config({"map": {"center": [44.0, -121.0]}})
        as_dict = parse_config(
            {"map": {"center": {"latitude": 44.0, "longitude": -121.0}}})
        self.assertEqual(as_list.map_settings["center"], [44.0, -121.0])
        self.assertEqual(as_dict.map_settings["center"], [44.0, -121.0])

    def test_there_is_a_street_basemap_for_the_zooms_topo_cannot_carry(self):
        # Zoomed out, OpenTopoMap is a brown relief smear with almost no
        # labels — Crater Lake was unfindable until one zoom in (2026-07-29).
        # Street tiles cover exactly those zooms, so both ship.
        settings = parse_config({}).map_settings
        self.assertIn("tile.openstreetmap.org", settings["street_tiles"]["url"])
        self.assertIn("opentopomap", settings["tiles"]["url"])
        self.assertEqual(settings["topo_from_zoom"], 12)

    def test_the_page_paints_its_own_campground_names(self):
        # Neither basemap can be relied on for them: OpenTopoMap labels no
        # campgrounds at all and the street tiles label only some (2026-07-29).
        # Held back at wide zooms, where 774 names would be a smear.
        self.assertEqual(parse_config({}).map_settings["labels_from_zoom"], 11)
        cfg = parse_config({"map": {"labels_from_zoom": 13}})
        self.assertEqual(cfg.map_settings["labels_from_zoom"], 13)

    def test_the_handover_zoom_is_configurable(self):
        # It is a judgement call about legibility, and judgement calls belong
        # in config rather than in JavaScript.
        cfg = parse_config({"map": {"topo_from_zoom": 14}})
        self.assertEqual(cfg.map_settings["topo_from_zoom"], 14)

    def test_a_street_tile_override_keeps_the_rest_of_its_defaults(self):
        cfg = parse_config({"map": {"street_tiles": {"url": "https://x/{z}.png"}}})
        street = cfg.map_settings["street_tiles"]
        self.assertEqual(street["url"], "https://x/{z}.png")
        self.assertEqual(street["max_zoom"], 19)
        self.assertTrue(street["attribution"])

    def test_a_half_written_centre_is_no_centre_at_all(self):
        # (0, -121) is in the Gulf of Guinea. Better to fall back than to
        # silently point the map at the ocean.
        cfg = parse_config({"map": {"center": {"longitude": -121.0}}})
        self.assertIsNone(cfg.map_settings["center"])


class TestResponseHeaders(unittest.TestCase):
    """What the server promises about freshness.

    No socket is opened: the handler's send path is driven directly, because
    this sandbox cannot bind a port at all (`Operation not permitted`).
    """

    def _headers_for(self):
        from app.web import Handler

        handler = Handler.__new__(Handler)          # no socket, no __init__
        sent: dict[str, str] = {}
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: sent.__setitem__(k, v)
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler._send(200, b"hello", "text/plain")
        return sent

    def test_nothing_the_server_sends_may_be_cached(self):
        # Safari cached index.html when we sent no cache headers, so new
        # markup arrived stripped of the new markup and read as a broken
        # feature (2026-07-29). The same header protects the API, where a
        # cached answer about what is open is a wrong answer about what is open.
        self.assertIn("no-store", self._headers_for()["Cache-Control"])


class TestMapPayload(DBTestCase):
    """What `/api/state` hands the page."""

    def test_it_carries_the_basemap_settings(self):
        from app.web import build_state
        state = build_state(self.conn, cfg=parse_config({}))
        self.assertIn("opentopomap", state["map"]["tiles"]["url"])

    def test_campgrounds_with_no_location_are_counted_not_dropped(self):
        # The map can't draw them, so the page has to say so out loud rather
        # than let them vanish between the catalog count and the pin count.
        from app.web import build_state
        store.upsert_campgrounds(self.conn, [
            Campground(provider="Mock", id="here", name="Located",
                       state="OR", latitude=45.0, longitude=-122.0),
            Campground(provider="Mock", id="lost", name="Location unknown",
                       state="OR"),
        ], now=NOW)
        state = build_state(self.conn)
        self.assertEqual(state["total"], 2)
        self.assertEqual(state["unlocated"], 1)
        names = {c["name"] for c in state["campgrounds"]}
        self.assertIn("Location unknown", names)

    def test_it_still_works_with_no_config_file(self):
        # `load_config` returns defaults when config.yaml is absent, and the
        # page must render rather than 500 on a fresh checkout.
        from app.web import build_state
        state = build_state(self.conn)
        self.assertTrue(state["map"]["tiles"]["url"])
        self.assertEqual(state["unlocated"], 0)


if __name__ == "__main__":
    unittest.main()
