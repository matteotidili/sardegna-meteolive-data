import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "station_monitor", Path(__file__).parents[1] / "scripts" / "monitor_station_health.py")
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("data").mkdir()
        self.write("data/stations.json", [
            {"name": "DPCN test", "temp": 12, "last": (NOW-timedelta(minutes=12)).isoformat()}])
        self.write("data/am.json", [
            {"name": "AM test", "temp": 13, "last": (NOW-timedelta(minutes=10)).isoformat()}])
        self.write("data/ecowitt.json", [
            {"name": "Ecowitt test", "temp": 14, "last": (NOW-timedelta(minutes=5)).isoformat()}])
        Path("data/cedoc.json").write_text(json.dumps({
            "status": "ok", "stations": [
                {"station_id": "001", "rain_today": 2.4,
                 "last": (NOW-timedelta(minutes=30)).isoformat()}]
        }))
        self.write("data/wunderground.json", [
            {"station_id":"PWS1","temp":10,"last":(NOW-timedelta(days=1)).isoformat()}])
        Path("data/wunderground_app_api_health.json").write_text(json.dumps(
            {"status":"cooldown","blocked_until":"2026-11-03T17:22:21Z",
             "last_error":"API key is throttled"}))

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def write(self, path, stations):
        Path(path).write_text(json.dumps({"stations":stations,
            "checked_at": max((s["last"] for s in stations), default=None)}))

    def test_provider_wu_paused_and_other_networks_ok(self):
        report=monitor.evaluate(NOW)
        self.assertEqual(report["DPCN"]["status"],"ok")
        self.assertEqual(report["Aeronautica Militare"]["status"],"ok")
        self.assertEqual(report["CEDOC"]["status"],"ok")
        self.assertEqual(report["Weather Underground"]["status"],"paused")
        self.assertEqual(report["Weather Underground"]["fresh"],0)

    def test_old_acquisition_is_detected_even_with_recent_observation(self):
        p = Path("data/stations.json")
        data = json.loads(p.read_text())
        data["checked_at"] = (NOW-timedelta(minutes=31)).isoformat()
        p.write_text(json.dumps(data))
        info = monitor.evaluate(NOW)["DPCN"]
        self.assertEqual(info["status"], "acquisition_stale")
        self.assertTrue(info["recovery_due"])

    def test_source_delay_does_not_trigger_repeated_acquisition(self):
        p = Path("data/am.json")
        p.write_text(json.dumps({"checked_at": NOW.isoformat(), "stations": [
            {"temp": 13, "last": (NOW-timedelta(hours=2)).isoformat()}]}))
        info = monitor.evaluate(NOW)["Aeronautica Militare"]
        self.assertEqual(info["status"], "source_delayed")
        self.assertFalse(info["recovery_due"])

    def test_preventive_recovery_at_twenty_minutes(self):
        p = Path("data/stations.json")
        data = json.loads(p.read_text())
        data["checked_at"] = (NOW-timedelta(minutes=20)).isoformat()
        p.write_text(json.dumps(data))
        self.assertTrue(monitor.evaluate(NOW)["DPCN"]["recovery_due"])

    def test_cedoc_fallback_stale_triggers_independent_recovery(self):
        Path("data/cedoc.json").write_text(json.dumps({
            "status": "unavailable", "stations": []}))
        report=monitor.evaluate(NOW)
        self.assertEqual(report["CEDOC"]["status"],"stale")
        self.assertEqual(report["DPCN"]["status"],"ok")
        calls=[]
        def fake_api(method, path, payload=None):
            calls.append((method,path,payload))
            return {"workflow_runs":[]}
        with patch.object(monitor,"github",side_effect=fake_api):
            monitor.self_repair(report,NOW)
        self.assertTrue(any(m=="POST" and "update-cedoc.yml/dispatches" in p
                            for m,p,_ in calls))
        self.assertFalse(any(m=="POST" and "update-observations.yml" in p
                             for m,p,_ in calls))

    def test_stale_am_triggers_recovery_without_wu_api_call(self):
        self.write("data/am.json",[
            {"temp":12,"last":(NOW-timedelta(hours=4)).isoformat()}])
        report=monitor.evaluate(NOW)
        calls=[]
        def fake_api(method, path, payload=None):
            calls.append((method,path,payload))
            return {"workflow_runs": []}
        with patch.object(monitor,"github",side_effect=fake_api):
            monitor.self_repair(report,NOW)
        self.assertTrue(any(m=="POST" and p.endswith("/dispatches")
                            for m,p,_ in calls))
        self.assertTrue(all("weather.com" not in p for _,p,_ in calls))

    def test_recent_running_update_prevents_duplicate_dispatch(self):
        self.write("data/stations.json", [
            {"temp":12,"last":(NOW-timedelta(hours=4)).isoformat()}])
        report=monitor.evaluate(NOW)
        calls=[]
        def fake_api(method,path,payload=None):
            calls.append((method,path))
            return {"workflow_runs":[{"created_at":(NOW-timedelta(minutes=5)).isoformat(),
                                      "status":"in_progress"}]}
        with patch.object(monitor,"github",side_effect=fake_api):
            monitor.self_repair(report,NOW)
        self.assertEqual([m for m,p in calls if m=="POST"],[])

    def test_only_wu_degraded_does_not_restart_other_networks(self):
        report=monitor.evaluate(NOW)
        with patch.object(monitor,"github") as mocked:
            monitor.self_repair(report,NOW)
        mocked.assert_not_called()

    def test_issue_change_throttling(self):
        self.write("data/stations.json", [
            {"temp":12,"last":(NOW-timedelta(hours=4)).isoformat()}])
        report=monitor.evaluate(NOW)
        previous=monitor.description(report,NOW-timedelta(minutes=15))
        calls=[]
        def fake_api(method,path,payload=None):
            calls.append((method,path))
            return [{"title":monitor.TITLE,"number":6,"state":"open",
                     "body":previous}] if method=="GET" else {}
        with patch.object(monitor,"github",side_effect=fake_api):
            monitor.sync_issue(report,NOW)
        self.assertEqual([m for m,_ in calls if m=="PATCH"],[])

    def test_disabled_wu_does_not_keep_incident_open(self):
        report=monitor.evaluate(NOW)
        calls=[]
        def fake_api(method,path,payload=None):
            calls.append((method,path,payload))
            return [{"title":monitor.TITLE,"number":6,"state":"open",
                     "body":"WU indisponibile"}] if method=="GET" else {}
        with patch.object(monitor,"github",side_effect=fake_api):
            monitor.sync_issue(report,NOW)
        self.assertTrue(any(m=="PATCH" and payload.get("state")=="closed"
                            for m,p,payload in calls))

if __name__=="__main__":
    unittest.main()

