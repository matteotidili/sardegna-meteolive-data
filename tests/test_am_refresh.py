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
