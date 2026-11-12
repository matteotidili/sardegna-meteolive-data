import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("am", Path(__file__).parents[1]/"scripts/update_am.py")
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

class RefreshTests(unittest.TestCase):
    def test_metar_survives_ogimet_failure(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)/"am.json"
            with patch.object(am, "OUT", out), patch.object(am, "fetch_synops", side_effect=RuntimeError()), patch.object(am, "fetch_metars", return_value=[{"icaoId":"LIEE", "obsTime":now.timestamp(), "temp":20}]):
                am.main()
            data = json.loads(out.read_text())
            self.assertEqual(data["stations"][0]["temp"], 20)
            self.assertEqual(data["stations"][0]["observation_type"], "METAR")
            self.assertEqual(data["source_errors"], ["synop_unavailable"])

    def test_sunop_official_tmax_overrides_rounded_metar(self):
        # METAR delle 12:20 UTC: 28°C arrotondati; SYNOP 18 UTC: 27,7°C.
        fixed = datetime(2026, 11, 12, 18, 15, tzinfo=timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        synops = ("16560,2026,11,12,18,00,"
                  "AAXX 12184 16560 12680 53207 10220 20162 "
                  "30007 40011 52021 60002 85100 333 10277 85835==\\n")
        metars = [{
            "icaoId": "LIEE", "obsTime": datetime(2026,11,12,12,20,tzinfo=timezone.utc).timestamp(),
            "temp": 28.0, "rawOb": "METAR LIEE 121220Z 26013KT 9999 28/17 Q0997"
        }]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "am.json"
            with patch.object(am, "OUT", out), patch.object(am, "datetime", Clock), \\
                 patch.object(am, "fetch_synops", return_value=synops), \\
                 patch.object(am, "fetch_metars", return_value=metars):
                am.main()
            elmas = next(st for st in json.loads(out.read_text())["stations"] if st["station_id"] == "LIEE")
            self.assertEqual(elmas["tmax"], 27.7)
            self.assertFalse(elmas["tmax_provisional"])
            self.assertEqual(elmas["tmax_source"], "synop_reported")

    def test_metar_only_tmax_remains_provisional(self):
        fixed = datetime(2026, 11, 12, 14, 15, tzinfo=timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        metars = [{
            "icaoId": "LIEE", "obsTime": datetime(2026,11,12,12,20,tzinfo=timezone.utc).timestamp(),
            "temp": 28.0,
        }]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "am.json"
            with patch.object(am, "OUT", out), patch.object(am, "datetime", Clock), \\
                 patch.object(am, "fetch_synops", return_value=""), \\
                 patch.object(am, "fetch_metars", return_value=metars):
                am.main()
            elmas = next(st for st in json.loads(out.read_text())["stations"] if st["station_id"] == "LIEE")
            self.assertEqual(elmas["tmax"], 28)
            self.assertTrue(elmas["tmax_provisional"])
            self.assertEqual(elmas["tmax_source"], "sampled_synop_metar")

    def test_post_report_new_maximum_is_provisional(self):
        fixed = datetime(2026, 11, 12, 19, 15, tzinfo=timezone.utc)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        synops = ("16560,2026,11,12,18,00,"
                  "AAXX 12184 16560 12680 53207 10220 20162 "
                  "30007 40011 52021 60002 85100 333 10277 85835==\\n")
        metars = [{
            "icaoId": "LIEE", "obsTime": datetime(2026,11,12,19,00,tzinfo=timezone.utc).timestamp(),
            "temp": 29.0,
        }]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "am.json"
            with patch.object(am, "OUT", out), patch.object(am, "datetime", Clock), \\
                 patch.object(am, "fetch_synops", return_value=synops), \\
                 patch.object(am, "fetch_metars", return_value=metars):
                am.main()
            elmas = next(st for st in json.loads(out.read_text())["stations"] if st["station_id"] == "LIEE")
            self.assertEqual(elmas["tmax"], 29)
            self.assertTrue(elmas["tmax_provisional"])
            self.assertEqual(elmas["tmax_source"], "post_synop_sampled")

    def test_total_outage_preserves_file_and_check_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)/"am.json"
            original = '{"checked_at":"2000-01-01T00:00:00Z","stations":[]}'
            out.write_text(original)
            with patch.object(am, "OUT", out), patch.object(am, "fetch_synops", side_effect=RuntimeError()), patch.object(am, "fetch_metars", return_value=[]):
                with self.assertRaises(RuntimeError):
                    am.main()
            self.assertEqual(out.read_text(), original)

    def test_older_provider_response_never_replaces_newer_station(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)/"am.json"
            previous = {"wmo":"16560", "station_id":"LIEE", "name":"Cagliari-Elmas", "temp":22, "last":now.isoformat()}
            out.write_text(json.dumps({"stations":[previous]}))
            with patch.object(am, "OUT", out), patch.object(am, "fetch_synops", return_value=""), patch.object(am, "fetch_metars", return_value=[{"icaoId":"LIEE", "obsTime":(now-timedelta(hours=1)).timestamp(), "temp":18}]):
                am.main()
            self.assertEqual(json.loads(out.read_text())["stations"][0]["temp"], 22)

if __name__ == "__main__":
    unittest.main()
