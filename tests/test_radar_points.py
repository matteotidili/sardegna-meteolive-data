"""Regressioni encoding numerico radar: NoData, risoluzione, conversione reale."""
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from update_radar_points import pack_png, MIN_VALID, SCALE, BIAS, crop_window
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
import rasterio

class RadarPointsTests(unittest.TestCase):
    def test_lossless_tenth_values_and_missing(self):
        arr=np.array([[-9999, np.nan, 0.0, 10.5, 25.2, 55.5, 250.0],
                      [-5.0, -0.1, 0.1, 2.3, 18.7, 9999.0, np.inf]],dtype=np.float32)
        im=pack_png(arr)
        rgba=np.asarray(im)
        self.assertTrue(np.all(rgba[:,:,3]==255))
        codes=rgba[:,:,0].astype(np.uint16)+rgba[:,:,1].astype(np.uint16)*256
        self.assertEqual(int(codes[0,0]),0)
        self.assertEqual(int(codes[0,1]),0)
        self.assertEqual(int(codes[1,5]),0)
        self.assertEqual(int(codes[1,6]),0)
        for ix in [2,3,4,5,6]:
            self.assertAlmostEqual((int(codes[0,ix])-1)/SCALE-BIAS,float(arr[0,ix]),places=1)
        self.assertAlmostEqual((int(codes[1,0])-1)/SCALE-BIAS,-5.0,places=1)
    def test_source_bounds_and_projection(self):
        crs=rasterio.crs.CRS.from_proj4("+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
        with MemoryFile() as memory:
            with memory.open(driver="GTiff",width=1200,height=1400,count=1,dtype="float32",crs=crs,transform=from_origin(-600000,650000,1000,1000)) as src:
                window=crop_window(src)
                self.assertGreater(window.width,200)
                self.assertGreater(window.height,200)
                self.assertLess(window.width,550)
                self.assertLess(window.height,550)

if __name__=="__main__":unittest.main()
