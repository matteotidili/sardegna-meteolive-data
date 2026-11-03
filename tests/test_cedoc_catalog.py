"""Regressioni: CEDOC non perde il catalogo quando la fonte risponde 503."""
import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

SPEC = importlib.util.spec_from_file_location(
    "cedoc_collector", Path(__file__).resolve().parents[1] / "scripts" / "update_cedoc.py"
)
cedoc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cedoc)


class CedocCatalogTests(unittest.TestCase):
    def catalog(self, path, age_days):
        entries = [
            {"station_id": str(400000 + i),
             "name": "Pluviometro " + str(i),
             "sensor_path": f"/sensore/{400000+i}/{80000+i}/20207"}
            for i in range(18)
        ]
        payload = {
            "generated_at": (cedoc.NOW - dt.timedelta(days=age_days)).isoformat(),
            "stations": entries,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return entries

    def test_expired_catalog_survives_http_503(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cedoc_sensors.json"
            expected = self.catalog(path, age_days=10)
            with patch.object(cedoc, "CATALOG", path), patch.object(
                cedoc, "get_catalog", side_effect=requests.HTTPError("503 Service Unavailable")
            ):
                with requests.Session() as session:
                    actual = cedoc.sensor_catalog(session)
            self.assertEqual(actual, expected)

    def test_recent_catalog_does_not_request_public_directory(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cedoc_sensors.json"
            expected = self.catalog(path, age_days=1)
            with patch.object(cedoc, "CATALOG", path), patch.object(
                cedoc, "get_catalog", side_effect=AssertionError("unnecessary network call")
            ):
                with requests.Session() as session:
                    actual = cedoc.sensor_catalog(session)
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
