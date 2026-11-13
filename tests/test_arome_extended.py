"""Regressioni: nuovi prodotti AROME e coerenza cromatica dei raster."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from arome_parameters import PARAMETERS, Renderer, STOPS, palette_image
from update_arome_hd import PALETTE


class AromeExtendedTests(unittest.TestCase):
    def test_full_surface_catalog(self):
        for key in ("pressure_sfc", "winddir10", "rain_liquid_acc", "rain_1h", "snow_1h", "graupel_1h"):
            self.assertIn(key, PARAMETERS)
        self.assertEqual(len(PARAMETERS), 27)
        self.assertEqual(STOPS["precip_total"][-1][0], 500)
        self.assertEqual(PALETTE[-1][0], 80)
        # PNG orario e prodotti cumulati hanno scale distinte
        self.assertEqual([x for x, _ in PALETTE if x > 0], [x for x, _ in STOPS["precip"]])

    def test_generate_all_surface_products_from_three_existing_packages(self):
        a = np.full((2, 3), 1.0, dtype=np.float32)
        sp1 = {"2t": a*293.15, "2r": a*70, "10u": a*4, "10v": a*0,
               "max_10efg": a*6, "max_10nfg": a*3}
        sp2 = {"sp": a*100000, "CAPE_INS": a*500,
               "lcc": a*20, "mcc": a*40, "hcc": a*60,
               "tirf": a*3, "tsnowp": a*1, "tgrp": a*0.5,
               "sim_refl": a*30}
        sp3 = {"sim_ir": a*250}
        with tempfile.TemporaryDirectory() as tmp:
            renderer = Renderer(tmp)
            renderer.frame(1, sp1, sp2, sp3, None)
            self.assertEqual(renderer.frames, 27)
            self.assertEqual(set(renderer.manifest()), set(PARAMETERS))
            for key in PARAMETERS:
                path = Path(tmp) / "products" / key / "h01.webp"
                self.assertTrue(path.is_file(), key)
                with Image.open(path) as image:
                    self.assertEqual(image.size, (3, 2))
                    self.assertEqual(image.mode, "RGBA")
                    if key in ("pressure_sfc", "rain_liquid_acc", "rain_1h"):
                        self.assertGreater(image.getpixel((1, 0))[3], 0, key)

    def test_palette_marks_no_precipitation_transparent(self):
        p = palette_image(np.array([[0, 0.3, 4]], dtype=np.float32), "precip")
        self.assertEqual(p.getpixel((0, 0))[3], 0)
        self.assertGreater(p.getpixel((1, 0))[3], 0)


if __name__ == "__main__":
    unittest.main()
