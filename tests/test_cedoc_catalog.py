"""CEDOC parsing, coverage and retention regressions."""
from contextlib import ExitStack
import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from bs4 import BeautifulSoup

SPEC = importlib.util.spec_from_file_location(
    "cedoc_collector", Path(__file__).resolve().parents[1] / "scripts" / "update_cedoc.py"
)
cedoc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cedoc)

NOW = dt.datetime(2026, 11, 26, 10, 45, tzinfo=dt.timezone.utc)
STATIONS = ("451800", "426400", "505900")
SENSORS = ("35523", "33147", "38286")


def station(code, sensor, measurement):
    return BeautifulSoup(
        f"""<h2>Pluviometro {code}</h2><table><tr><td>Codice statione</td><td>{code}</td></tr>
        <tr><td>Longitudine (WGS84)</td><td>8.56361</td><td>Latitudine(WGS84)</td><td>39.30139</td></tr></table>
        <a href="/idrografico/sensore/{code}/{sensor}/20207">Pluviometro</a>
        <h3>Rilevamento precipitazioni:</h3><span>{measurement}</span>""", "html.parser"
    )


def daily(code, sensor, measurement="7.00"):
    return BeautifulSoup(
        f"""<table><tr><td>Codice stazione</td><td>{code}</td></tr>
        <tr><td>Codice sensore</td><td>{sensor}</td><td>Tipo</td><td>Pluviometro</td></tr></table>
        <h3>Precipitazioni cumulate giornaliere</h3><table>
        <tr><td>Pioggia cumulata oggi</td><td>{measurement} mm</td></tr></table>
        <h3>Precipitazioni cumulate aggregate</h3><table>
        <tr><td>Pioggia cumulata oggi</td><td>50.00 mm</td></tr></table>""", "html.parser"
    )


def geo(code, value):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [8.56, 39.30]},
            "properties": {"StatNum": int(code), "StatName": "Iglesias",
                           "url": "/idrografico/stazione/" + code, "value": value}}


class FakeClient:
    workers = 2

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, path, **_):
        self.requested.append(path)
        response = self.pages.get(path)
        if response is None:
            raise requests.ConnectionError("503")
        return response, NOW


class CedocCollectorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("OUT", "LAST_VALID", "SITES", "CATALOG"):
            self.stack.enter_context(patch.object(cedoc, name, folder / f"{name}.json"))
        self.stack.enter_context(patch.object(cedoc, "utcnow", return_value=NOW))
        cedoc.write_json(cedoc.CATALOG, {"stations": [
            {"station_id": code, "name": code, "sensor_path": f"/sensore/{code}/{sensor}/20207"}
            for code, sensor in zip(STATIONS, SENSORS)
        ]})
        cedoc.write_json(cedoc.SITES, {"stations": [
            {"station_id": code, "name": code, "lat": 39.3, "lon": 8.56}
            for code in STATIONS
        ]})

    def legacy(self, code, sensor, last, daily_total=8, rolling_total=22):
        return {"station_id": code, "name": code, "lat": 39.3, "lon": 8.56, "last": last,
                "rain_today": daily_total, "rain_48h": rolling_total,
                "source_url": cedoc.BASE + f"/sensore/{code}/{sensor}/20207"}

    def test_partial_run_preserves_old_rows_and_replaces_only_verified_values(self):
        a, b, c = STATIONS
        x, y, _ = SENSORS
        cedoc.write_json(cedoc.LAST_VALID, {"stations": [
            self.legacy(a, x, "2026-11-26T07:00:00Z"),
            self.legacy(b, y, "2026-11-25T22:30:00Z"),
        ]})
        client = FakeClient({
            cedoc.BULK: {"type": "FeatureCollection", "features": [geo(a, "50.6"), geo(a, "50.9")]},
            f"/stazione/{a}": station(a, x, "26-11-2026 10:35 53,8 mm nelle ultime 48 h"),
            f"/sensore/{a}/{x}/20207": daily(a, x),
            f"/stazione/{c}": BeautifulSoup("", "html.parser"),
        })
        report = cedoc.acquire(client)
        self.assertTrue(cedoc.validate(report))
        rows = {row["station_id"]: row for row in report["stations"]}
        self.assertEqual({p for p in client.requested if p.startswith("/stazione/")}, {f"/stazione/{c}" for c in STATIONS})
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["coverage"], {"censused": 3, "succeeded": 1, "failed": 2, "partial": 0,
                                             "updated": 1, "unreachable": 1, "with_value": 2,
                                             "without_value": 1, "fresh": 1, "older": 1})
        self.assertEqual(rows[a]["variables"]["rain_48h"]["value"], 53.8)
        self.assertEqual(rows[a]["last"], "2026-11-26T09:35:00Z")
        self.assertEqual(rows[a]["fetched_at"], "2026-11-26T10:45:00Z")
        self.assertEqual(rows[a]["variables"]["rain_today"]["value"], 7)
        self.assertEqual(rows[b]["variables"]["rain_today"]["status"], "historic")
        self.assertEqual(rows[b]["variables"]["rain_today"]["local_day"], "2026-11-25")
        self.assertEqual(rows[b]["last"], "2026-11-25T22:30:00Z")
        self.assertIsNone(rows[b]["rain_today"])
        self.assertIsNone(rows[c]["variables"]["rain_48h"]["value"])
        self.assertEqual(rows[c]["reading_status"], "invalid")
        self.assertEqual(len(cedoc.data_file(cedoc.SITES)["stations"]), 3)
        before = cedoc.LAST_VALID.read_bytes()
        outage = cedoc.acquire(FakeClient({}))
        self.assertEqual(outage["coverage"]["with_value"], 2)
        self.assertEqual(outage["coverage"]["unreachable"], 3)
        self.assertEqual(before, cedoc.LAST_VALID.read_bytes())

    def test_missing_negative_or_unverifiable_values_never_become_zero(self):
        code, sensor = STATIONS[0], SENSORS[0]
        for text in ("", "n.d.", "26-11-2026 10:35 -1 mm nelle ultime 48 h", "0 mm nelle ultime 48 h"):
            with self.subTest(text=text):
                self.assertIsNone(cedoc.read_station(station(code, sensor, text), code, NOW)[2])
        good = "26/11/2026 10:35:00 – 0,00 mm ultime 48 ore"
        self.assertEqual(cedoc.read_station(station(code, sensor, good), code, NOW)[2]["value"], 0)
        self.assertIsNone(cedoc.read_daily(daily(code, sensor, "n.d."), code, sensor))
        self.assertEqual(cedoc.read_daily(daily(code, sensor, "0"), code, sensor), 0)
        with self.assertRaises(ValueError):
            cedoc.read_daily(daily(code, sensor), code, SENSORS[1])
        self.assertEqual(len(cedoc.bulk_sites({"type": "FeatureCollection", "features": [geo(code, 21), geo(code, 29)]})), 1)

    def test_older_source_page_cannot_replace_last_valid(self):
        code, sensor = STATIONS[0], SENSORS[0]
        cedoc.write_json(cedoc.OUT, {"stations": [self.legacy(code, sensor, "2026-11-26T09:00:00Z")]})
        client = FakeClient({f"/stazione/{code}": station(code, sensor, "26-11-2026 08:35 0 mm nelle ultime 48 h"),
                             f"/sensore/{code}/{sensor}/20207": daily(code, sensor, "0")})
        rows = {row["station_id"]: row for row in cedoc.acquire(client)["stations"]}
        self.assertEqual(rows[code]["last"], "2026-11-26T09:00:00Z")
        self.assertEqual(rows[code]["rain_48h"], 22)
        self.assertTrue(rows[code]["variables"]["rain_48h"]["retained"])

    def test_empty_response_keeps_catalog_coordinates(self):
        report = cedoc.acquire(FakeClient({}))
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["coverage"]["without_value"], 3)
        self.assertEqual(len(cedoc.data_file(cedoc.SITES)["stations"]), 3)
        self.assertFalse(cedoc.LAST_VALID.exists())


if __name__ == "__main__":
    unittest.main()
