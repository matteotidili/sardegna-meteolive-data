"""Regression tests: WU must never disappear after API failures.

All calls are mocked; this suite never uses the real Weather Underground key.
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wu_api_health as health
import discover_wunderground as discovery
import update_wunderground as updater


class WUResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.catalog = root / "inventory.json"
        self.data = root / "observations.json"
        self.state = root / "health.json"
        for target, name in [
            (health, "STATE_FILE", self.state),
            (discovery, "OUT", self.catalog),
            (updater, "SRC", self.catalog),
            (updater, "OUT", self.data),
        ]:
            patcher = mock.patch.object(target, name, {
                "STATE_FILE": self.state, "OUT": (self.catalog if target is discovery else self.data),
                "SRC": self.catalog,
            }[name])
            patcher.start()
            self.addCleanup(patcher.stop)
        keypatch = mock.patch.dict(os.environ, {"WU_API_KEY": "test-key"}, clear=False)
        keypatch.start()
        self.addCleanup(keypatch.stop)

    def write(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_circuit_breaker_exponential_and_recovery(self):
        state = health.record_failure({}, 401, "API key is throttled")
        self.assertTrue(health.in_cooldown(state))
        self.assertEqual(state["last_http_status"], 401)
        state2 = health.record_failure(state, 429, "quota")
        first = health.parse_stamp(state["blocked_until"])
        second = health.parse_stamp(state2["blocked_until"])
        self.assertGreater((second - first).total_seconds(), 20 * 3600)
        self.assertFalse(health.in_cooldown(health.record_success(state2)))

    def test_discovery_additive_deduplicated_and_budgeted(self):
        previous = {"stations": [{"station_id":"OLD1","name":"Existing","lat":40.0,"lon":9.0}],
                    "discovery_cursor":0}
        self.write(self.catalog, previous)
        payload = {"location": {
            "stationId":["OLD1","NEW2"], "stationName":["Existing","New site"],
            "latitude":[40.0,40.1], "longitude":[9.0,9.1],
        }}
        with mock.patch.object(discovery, "discovery_points", return_value=[(40.0,9.0),(40.1,9.1)]), \
             mock.patch.object(discovery, "get_json", return_value=payload) as get, \
             mock.patch.object(discovery.time, "sleep"):
            discovery.main()
        saved = self.read(self.catalog)
        self.assertEqual({x["station_id"] for x in saved["stations"]} - set(discovery.KNOWN_PWS), {"OLD1","NEW2"})
        self.assertEqual(saved["last_new_station_ids"], ["NEW2"])
        self.assertEqual(get.call_count, 2)

    def test_throttled_discovery_keeps_catalog(self):
        self.write(self.catalog, {"stations":[{"station_id":"OLD1","name":"X","lat":40,"lon":9}]})
        with mock.patch.object(discovery, "discovery_points", return_value=[(40,9)]), \
             mock.patch.object(discovery, "get_json",
                               side_effect=urllib.error.HTTPError("https://example.com",401,"throttled",{},None)), \
             mock.patch.object(discovery.time, "sleep"):
            discovery.main()
        saved = self.read(self.catalog)
        self.assertTrue(any(s["station_id"]=="OLD1" for s in saved["stations"]))
        self.assertTrue(health.in_cooldown(self.read(self.state)))
        self.assertEqual(saved["discovery_last_status"], "blocked")

    def test_updater_skips_during_cooldown_without_touching_data(self):
        existing = {"generated_at":"2026-10-29T00:00:00Z",
                    "stations":[{"station_id":"OLD1","temp":12}]}
        self.write(self.data, existing)
        self.write(self.state, {"blocked_until":"2099-01-01T00:00:00Z","status":"cooldown"})
        with mock.patch.object(updater, "process_station", side_effect=AssertionError("API call forbidden")):
            updater.main()
        self.assertEqual(self.read(self.data), existing)

    def test_successful_updater_keeps_unpolled_stations(self):
        self.write(self.catalog, {"stations":[
            {"station_id":"ABC","name":"A","lat":40,"lon":9},
            {"station_id":"DEF","name":"D","lat":40.1,"lon":9.1},
        ]})
        oldrow = {"station_id":"OLD1","name":"O","lat":40.2,"lon":9.2,
                  "temp":18,"available":{"temp":True,"rh":False,"wind":False,"gust":False,"rain":False}}
        self.write(self.data, {"generated_at":"2026-10-28T00:00:00Z","stations":[oldrow]})
        def fake_station(st, key, now, today):
            row = dict(oldrow, station_id=st["station_id"], name=st["name"],
                       lat=st["lat"], lon=st["lon"], temp=20)
            return row, None, False
        with mock.patch.object(updater, "should_skip_recent_run", return_value=False), \
             mock.patch.object(updater, "process_station", side_effect=fake_station):
            updater.main()
        saved = self.read(self.data)
        self.assertEqual({x["station_id"] for x in saved["stations"]}, {"OLD1","ABC","DEF"})
        self.assertEqual(saved["batch_updated"], 2)
        self.assertEqual(saved["station_count"], 3)


if __name__ == "__main__":
    unittest.main()
