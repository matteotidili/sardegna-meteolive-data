"""MNW display labels must not change source identity or retained observations."""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("meteonetwork_names_test", ROOT / "scripts/update_meteonetwork_hub.py")
MNW = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(ROOT / "scripts"))
try:
    SPEC.loader.exec_module(MNW)
finally:
    sys.path.pop(0)

NOW = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)


def observation(code="srd013", network="mnw", date="2026-11-09T15:00:00Z"):
    return {
        "network": network,
        "date": date,
        "data": [
            {"vars": {"B01019": {"v": code}, "B05001": {"v": 40.68},
                      "B06001": {"v": 8.59}, "B07030": {"v": 339}}},
            {"vars": {"B12101": {"v": 293.15}}, "timerange": [254, 0, 0]},
        ],
    }


def aggregate(*records):
    raw = "\n".join(json.dumps(record) for record in records).encode()
    return MNW.aggregate(raw, now=NOW)


class MeteoNetworkNameTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.places = Path(self.directory.name) / "places.json"
        self.places.write_text(json.dumps({"srd013": "Ossi", "srd025": "Iglesias"}), encoding="utf-8")
        self.places_patch = patch.object(MNW, "PLACES", self.places)
        self.places_patch.start()
        self.addCleanup(self.places_patch.stop)

    def test_place_rename_keeps_identity_across_observations(self):
        before = aggregate(observation())
        self.places.write_text(json.dumps({"srd013": "Ossi — centro"}), encoding="utf-8")
        after = aggregate(observation(date="2026-11-09T16:00:00Z"))
        self.assertEqual(before["stations"][0]["name"], "Ossi")
        self.assertEqual(after["stations"][0]["name"], "Ossi — centro")
        self.assertEqual(before["stations"][0]["station_id"], "mnw-srd013-40.6800-8.5900")
        self.assertEqual(after["stations"][0]["station_id"], before["stations"][0]["station_id"])
        self.assertEqual(after["stations"][0]["station_code"], "srd013")
        self.assertEqual(after["stations"][0]["last"], "2026-11-09T16:00:00Z")

    def test_dpcn_records_are_not_published_as_mnw(self):
        result = aggregate(observation(), observation(code="DPCN station", network="dpcn-sardegna"))
        self.assertEqual(result["station_count"], 1)
        self.assertEqual([row["station_code"] for row in result["stations"]], ["srd013"])

    def test_unmapped_codes_and_missing_catalog_keep_source_name(self):
        result = aggregate(observation(code="srd999"))
        self.assertEqual(result["stations"][0]["name"], "srd999")
        self.places.unlink()
        result = aggregate(observation())
        self.assertEqual(result["stations"][0]["name"], "srd013")

    def test_retained_legacy_row_is_renamed_without_duplicate_or_mutation(self):
        current = aggregate(observation())
        original = copy.deepcopy(current)
        previous_rows = [
            {"station_id": "mnw-srd013-40.6800-8.5900", "name": "srd013",
             "last": "2026-11-09T14:00:00Z", "temp": 17},
            {"station_id": "mnw-srd025-39.3100-8.5200", "name": "srd025",
             "last": "2026-11-09T12:00:00Z", "temp": 18},
            {"station_id": "mnw-srd999-39.0000-8.0000", "name": "srd999",
             "last": "2026-11-06T12:00:00Z", "temp": 19},
        ]
        path = Path(self.directory.name) / "previous.json"
        path.write_text(json.dumps({"stations": previous_rows}))
        result = MNW.preserve_previous(current, path)
        self.assertEqual(current, original)
        self.assertEqual(result["station_count"], 2)
        self.assertEqual(result["retained_previous_station_count"], 1)
        self.assertEqual([row["name"] for row in result["stations"]], ["Iglesias", "Ossi"])
        retained = result["stations"][0]
        self.assertEqual(retained["station_code"], "srd025")
        self.assertEqual(retained["last"], "2026-11-09T12:00:00Z")
        self.assertEqual(retained["temp"], 18)
        self.assertEqual(len({row["station_id"] for row in result["stations"]}), 2)
        self.assertEqual(json.loads(path.read_text())["stations"], previous_rows)

    def test_only_known_legacy_identity_format_supplies_a_code(self):
        station = {"station_id": "mnw-srd013-wrong-format", "name": "Existing place"}
        result = MNW.named_station(station, {"srd013": "Ossi"})
        self.assertEqual(result, station)
        self.assertIsNot(result, station)


if __name__ == "__main__":
    unittest.main()
