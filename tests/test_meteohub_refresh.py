"""Regressione: MeteoHub deve funzionare anche dopo mezzanotte e mantenere quota."""
import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import call, patch
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

    def test_combined_request_limits_both_networks_to_sardinia_at_source(self):
        body=body_at(datetime(2026,11,3,23,25,tzinfo=ROME))
        self.assertEqual(body["dataset_names"],["dpcn-sardegna","mnw"])
        self.assertEqual(body["filters"]["bounding_box"],
                         {"lonmin":7.7,"latmin":38.75,"lonmax":10.3,"latmax":41.4})
        self.assertEqual(when(body["reftime"]["from"]).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-03 00:00")
        self.assertNotIn("force_obs_download",body)

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

class MeteoHubNotificationTests(unittest.TestCase):
    def test_login_turns_off_success_emails_and_verifies_before_returning(self):
        with patch.dict(M.os.environ,{"METEOHUB_USERNAME":"test-user","METEOHUB_PASSWORD":"test-password"}), \
             patch.object(M,"api",side_effect=[{"access_token":"test-token"},
                          {"notify_on_successful_request":True},b"",
                          {"notify_on_successful_request":False}]) as request:
            self.assertEqual(M.login(),"test-token")
        self.assertEqual(request.call_args_list,[
            call("POST","/auth/login",body={"username":"test-user","password":"test-password"}),
            call("GET","/auth/profile","test-token"),
            call("PATCH","/auth/profile","test-token",{"notify_on_successful_request":False},parse_json=False),
            call("GET","/auth/profile","test-token"),
        ])

    def test_already_disabled_preference_does_not_modify_profile(self):
        with patch.object(M,"api",return_value={"notify_on_successful_request":False}) as request:
            M.disable_success_notifications("test-token")
        request.assert_called_once_with("GET","/auth/profile","test-token")

    def test_no_extraction_when_preference_cannot_be_confirmed(self):
        with patch.dict(M.os.environ,{"METEOHUB_USERNAME":"test-user","METEOHUB_PASSWORD":"test-password"}), \
             patch.object(M,"should_skip_recent_run",return_value=False), \
             patch.object(M,"api",side_effect=[{"access_token":"test-token"},
                          {"notify_on_successful_request":True},b"",
                          {"notify_on_successful_request":True}]) as request:
            with self.assertRaisesRegex(RuntimeError,"nessuna estrazione avviata"):
                M.main()
        self.assertFalse(any(entry.args[1]=="/api/data" for entry in request.call_args_list))

    def test_profile_api_error_does_not_start_an_extraction(self):
        with patch.dict(M.os.environ,{"METEOHUB_USERNAME":"test-user","METEOHUB_PASSWORD":"test-password"}), \
             patch.object(M,"should_skip_recent_run",return_value=False), \
             patch.object(M,"api",side_effect=[{"access_token":"test-token"},RuntimeError("profile unavailable")]) as request:
            with self.assertRaisesRegex(RuntimeError,"profile unavailable"):
                M.main()
        self.assertFalse(any(entry.args[1]=="/api/data" for entry in request.call_args_list))

if __name__=="__main__":
    unittest.main()

