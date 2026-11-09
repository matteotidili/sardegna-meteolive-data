"""Regressione: MeteoHub deve funzionare anche dopo mezzanotte e mantenere quota."""
import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

SCRIPT=Path(__file__).resolve().parents[1]/"scripts"/"update_meteohub.py"
SPEC=importlib.util.spec_from_file_location("meteohub_refresh_test",SCRIPT)
M=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
ROME=ZoneInfo("Europe/Rome")


def body_at(dt):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return dt.astimezone(tz) if tz else dt.replace(tzinfo=None)
    with patch.object(M,"datetime",Clock):
        return M.request_body()


def when(iso):
    return datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(ROME)


class MeteoHubRefreshTests(unittest.TestCase):
    def test_rate_under_default_ten_per_hour(self):
        self.assertGreater(M.MIN_RUN_INTERVAL_MIN,6)
        self.assertLessEqual(M.MIN_RUN_INTERVAL_MIN,10)

    def test_first_minutes_include_previous_evening(self):
        body=body_at(datetime(2026,11,3,0,20,tzinfo=ROME))
        self.assertEqual(when(body["reftime"]["from"]).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-02 21:00")
        self.assertEqual(when(body["reftime"]["to"]).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-03 00:20")

    def test_near_three_am_includes_previous_evening(self):
        body=body_at(datetime(2026,11,3,2,55,tzinfo=ROME))
        self.assertEqual(when(body["reftime"]["from"]).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-02 21:00")

    def test_after_three_am_starts_at_local_midnight(self):
        body=body_at(datetime(2026,11,3,3,5,tzinfo=ROME))
        self.assertEqual(when(body["reftime"]["from"]).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-03 00:00")

    def test_current_afternoon_retains_daily_extremes_window(self):
        body=body_at(datetime(2026,11,3,16,25,tzinfo=ROME))
        self.assertEqual(when(body["reftime"]["from"]).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-03 00:00")

if __name__=="__main__":
    unittest.main()

