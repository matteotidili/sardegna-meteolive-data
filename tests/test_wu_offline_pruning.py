"""Regressioni: PWS inattive vengono escluse solo dopo scansione completa."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prune_wunderground_offline as pruning
import discover_wunderground as discovery
import wu_api_health as health

UTC = timezone.utc


class OfflinePruningTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.catalog = root / "inventory.json"
        self.observations = root / "observations.json"
        self.retired = root / "offline.json"
        self.health = root / "health.json"
        for obj, attr, value in (
            (pruning, "CATALOG", self.catalog),
            (pruning, "OBSERVATIONS", self.observations),
            (pruning, "RETIRED", self.retired),
            (pruning, "HEALTH", self.health),
            (discovery, "OUT", self.catalog),
            (discovery, "RETIRED", self.retired),
            (health, "STATE_FILE", self.health),
            (discovery, "MIN_CATALOG_SIZE", 1),
        ):
            patch = mock.patch.object(obj, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict("os.environ", {"WU_APP_API_KEY": "test-key"})
        patch.start()
        self.addCleanup(patch.stop)
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
        self.health.write_text(json.dumps({"status": "active", "blocked_until": None}))

    def write(self, path, obj):
        path.write_text(json.dumps(obj))

    def make_dataset(self, count=220, offline=15):
        old = self.now - timedelta(days=40)
        recent = self.now - timedelta(minutes=20)
        catalog = [{"station_id": f"PWS{i:03d}", "name": f"Site {i}",
                    "lat": 39.2, "lon": 9.3,
                    "update_time_utc": int(old.timestamp())}
                   for i in range(count)]
        rows = [{"station_id": f"PWS{i:03d}", "name": f"Site {i}",
                 "last": pruning.stamp(recent), "temp": 14,
                 "available": {"temp": True, "rh": False, "wind": False,
                               "gust": False, "rain": False}}
                for i in range(offline, count)]
        inventory = {"stations": catalog, "station_count": count}
        obs = {"checked_at": pruning.stamp(self.now - timedelta(minutes=5)),
               "requested_stations": count, "batch_requested": count,
               "batch_updated": count - offline, "station_count": len(rows),
               "stations": rows, "variable_counts": {"temperature": len(rows)}}
        self.write(self.catalog, inventory)
        self.write(self.observations, obs)
        return inventory, obs

    def test_removes_only_confirmed_long_offline_and_preserves_active(self):
        self.make_dataset()
        removed = pruning.prune(self.now)
        self.assertEqual(removed, 15)
        obs = json.loads(self.observations.read_text())
        sites = json.loads(self.catalog.read_text())
        offline = json.loads(self.retired.read_text())
        self.assertEqual(sites["station_count"], 205)
        self.assertEqual(obs["station_count"], 205)
        self.assertEqual(offline["station_count"], 15)
        self.assertEqual(obs["variable_counts"]["temperature"], 205)
        self.assertTrue(all(s["station_id"] not in
                            {f"PWS{i:03d}" for i in range(15)}
                            for s in sites["stations"]))

    def test_partial_scan_cannot_delete_stations(self):
        self.make_dataset()
        obs = json.loads(self.observations.read_text())
        obs["batch_requested"] = 28
        self.write(self.observations, obs)
        self.assertEqual(pruning.prune(self.now), 0)
        self.assertFalse(self.retired.exists())
        self.assertEqual(len(json.loads(self.catalog.read_text())["stations"]), 220)

    def test_quota_block_cannot_delete_stations(self):
        self.make_dataset()
        self.write(self.health, {"status": "cooldown",
                                 "blocked_until": pruning.stamp(self.now + timedelta(hours=8))})
        self.assertEqual(pruning.prune(self.now), 0)
        self.assertFalse(self.retired.exists())

    def test_discovery_can_reactivate_only_with_recent_vendor_timestamp(self):
        self.write(self.catalog, {"stations": [], "station_count": 0})
        self.write(self.retired, {"stations": [
            {"station_id": "OLDPWS", "name": "Offline", "retired_at": pruning.stamp(self.now)},
            {"station_id": "OTHER", "name": "Offline2", "retired_at": pruning.stamp(self.now)}
        ]})
        self.write(self.health, {"status": "active"})
        fresh = int(datetime.now(UTC).timestamp()) - 600
        stale = int((datetime.now(UTC) - timedelta(days=90)).timestamp())
        payload = {"location": {
            "stationId": ["OLDPWS", "OTHER"],
            "stationName": ["Revived", "Still offline"],
            "latitude": [39.4, 39.5], "longitude": [9.2, 9.3],
            "updateTimeUtc": [fresh, stale],
        }}
        with mock.patch.object(discovery, "discovery_points",
                               return_value=[(39.4,9.2)]), \
             mock.patch.object(discovery, "get_json", return_value=payload), \
             mock.patch.object(discovery.time, "sleep"):
            discovery.main()
        active = json.loads(self.catalog.read_text())
        inactive = json.loads(self.retired.read_text())
        ids = {x["station_id"] for x in active["stations"]}
        self.assertIn("OLDPWS", ids)
        self.assertNotIn("OTHER", ids)
        self.assertEqual({x["station_id"] for x in inactive["stations"]}, {"OTHER"})


if __name__ == "__main__":
    unittest.main()
