"""AROME wind must match its raster cycle, forecast times and binary grid."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "arome_bundle", Path(__file__).resolve().parents[1] / "scripts/arome_bundle.py")
BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUNDLE)
CYCLE = "2026-11-11T03:00:00Z"


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "data/arome"
        (self.path / "wind_vectors").mkdir(parents=True)
        start = datetime(2026, 11, 11, 3, tzinfo=timezone.utc)
        self.forecast = {"model": "AROME-France HD", "run_time": CYCLE,
                         "forecast_horizon_hours": 51, "frames": []}
        self.vectors = {"model": "AROME-France HD", "run_time": CYCLE,
                        "grid": copy.deepcopy(BUNDLE.GRID), "frames": []}
        # A real signed, little-endian U/V field; missing pairs are allowed.
        cells = 281 * 101
        binary = struct.pack("<hh", 125, -250) * (cells - 1) + struct.pack("<hh", -32768, -32768)
        for lead in range(1, 52):
            valid = (start + timedelta(hours=lead)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.forecast["frames"].append({"lead_hours": lead, "valid_time": valid})
            self.vectors["frames"].append({"lead_hours": lead, "valid_time": valid,
                                          "file": f"data/arome/wind_vectors/h{lead:02d}.bin"})
            (self.path / f"wind_vectors/h{lead:02d}.bin").write_bytes(binary)
        self.write_manifests()

    def write_manifests(self):
        (self.path / "forecast.json").write_text(json.dumps(self.forecast), encoding="utf-8")
        (self.path / "wind_vectors.json").write_text(json.dumps(self.vectors), encoding="utf-8")

    def test_accepts_matching_full_h51_bundle(self):
        self.assertEqual(BUNDLE.validate_bundle(self.root), CYCLE)

    def test_rejects_non_object_manifest_as_repairable_validation_error(self):
        for name in ("forecast.json", "wind_vectors.json"):
            with self.subTest(manifest=name):
                file = self.path / name
                original = file.read_text()
                file.write_text("[]")
                with self.assertRaisesRegex(ValueError, "oggetto JSON"):
                    BUNDLE.validate_bundle(self.root)
                file.write_text(original)

    def test_rejects_a_different_vector_cycle_before_publication(self):
        self.vectors["run_time"] = "2026-11-10T18:00:00Z"
        self.write_manifests()
        with patch.object(BUNDLE, "git") as git:
            with self.assertRaisesRegex(ValueError, "corsa dei vettori"):
                BUNDLE.publish(root=self.root)
            git.assert_not_called()

    def test_rejects_missing_and_duplicate_forecast_leads(self):
        for manifest in (self.forecast, self.vectors):
            original = copy.deepcopy(manifest["frames"])
            for frames in (original[:-1], original[:-1] + [original[0]]):
                with self.subTest(manifest="raster" if manifest is self.forecast else "vectors",
                                  count=len(frames)):
                    manifest["frames"] = frames
                    self.write_manifests()
                    with self.assertRaisesRegex(ValueError, "H\\+01..51"):
                        BUNDLE.validate_bundle(self.root)
            manifest["frames"] = original

    def test_rejects_wrong_valid_time_in_either_manifest(self):
        for manifest in (self.forecast, self.vectors):
            with self.subTest(manifest="raster" if manifest is self.forecast else "vectors"):
                previous = manifest["frames"][0]["valid_time"]
                manifest["frames"][0]["valid_time"] = "2026-11-11T05:00:00Z"
                self.write_manifests()
                with self.assertRaisesRegex(ValueError, "validità"):
                    BUNDLE.validate_bundle(self.root)
                manifest["frames"][0]["valid_time"] = previous

    def test_rejects_missing_or_truncated_binary(self):
        file = self.path / "wind_vectors/h01.bin"
        file.unlink()
        with self.assertRaises(FileNotFoundError):
            BUNDLE.validate_bundle(self.root)
        file.write_bytes(struct.pack("<hh", 125, -250))
        with self.assertRaisesRegex(ValueError, "dimensione binaria"):
            BUNDLE.validate_bundle(self.root)

    def test_rejects_empty_field_and_partial_missing_pair(self):
        file = self.path / "wind_vectors/h01.bin"
        cells = 281 * 101
        file.write_bytes(struct.pack("<hh", -32768, -32768) * cells)
        with self.assertRaisesRegex(ValueError, "insufficienti"):
            BUNDLE.validate_bundle(self.root)
        file.write_bytes(struct.pack("<hh", 125, -32768) * cells)
        with self.assertRaisesRegex(ValueError, "coppia U/V incompleta"):
            BUNDLE.validate_bundle(self.root)

    def test_rejects_incompatible_grid_encoding(self):
        self.vectors["grid"]["encoding"] = "signed_int16_big_endian"
        self.write_manifests()
        with self.assertRaisesRegex(ValueError, "codifica"):
            BUNDLE.validate_bundle(self.root)


class PublicationTests(unittest.TestCase):
    def test_remote_cycle_guard_preserves_newer_raster(self):
        old = "2026-11-11T00:00:00Z"
        newer = "2026-11-11T06:00:00Z"
        self.assertTrue(BUNDLE.candidate_is_current(CYCLE, old, "full"))
        self.assertTrue(BUNDLE.candidate_is_current(CYCLE, CYCLE, "full"))
        self.assertFalse(BUNDLE.candidate_is_current(CYCLE, newer, "full"))
        self.assertTrue(BUNDLE.candidate_is_current(CYCLE, CYCLE, "vectors"))
        self.assertFalse(BUNDLE.candidate_is_current(CYCLE, old, "vectors"))
        self.assertFalse(BUNDLE.candidate_is_current(CYCLE, newer, "vectors"))

    def test_rejected_push_refetches_and_never_overwrites_newer_cycle(self):
        calls = []
        remote_cycles = iter([CYCLE, "2026-11-11T06:00:00Z"])
        def git(root, *args, check=True, capture_output=False):
            calls.append(args)
            if args[0] == "show":
                return subprocess.CompletedProcess(args, 0, json.dumps({"run_time": next(remote_cycles)}))
            code = 1 if args[0] in ("diff", "push") else 0
            return subprocess.CompletedProcess(args, code)
        with patch.object(BUNDLE, "validate_bundle", return_value=CYCLE), \
             patch.object(BUNDLE, "git", side_effect=git), \
             patch.object(BUNDLE.time, "sleep"):
            self.assertFalse(BUNDLE.publish())
        self.assertEqual(sum(args[0] == "fetch" for args in calls), 2)
        self.assertEqual([args for args in calls if args[0] == "push"],
                         [("push", "origin", "HEAD:main")])


if __name__ == "__main__":
    unittest.main()
