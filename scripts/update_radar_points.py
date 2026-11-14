#!/usr/bin/env python3
"""Pubblichiamo raster numerici radar DPC, SENZA stime da RGB.

Sorgente: GeoTIFF originali DPC VMI (dBZ) e SRI (mm/h), scaricati
mediante API pubblica con Origin della webapp. Ritaglio sulla Sardegna
alla risoluzione originale di 1 km, nessuna interpolazione spaziale.
Ogni PNG contiene UInt16 nei canali R/G: codice 0 = NoData,
codice n = round(10 * (valore + 1000)) + 1; B=0, A=255.
L'encoding decimale a 0.1 conserva l'informazione visualizzabile e la
maschera degli assenti. Il manifest riporta CRS e trasformazione.
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import h5py
import numpy as np
import rasterio
from PIL import Image
from rasterio.warp import transform as geo_transform
from pyproj import Transformer

ROOT = Path("data/radar_points")
MANIFEST = ROOT / "manifest.json"
BASE = "https://radar-api.protezionecivile.it"
ORIGIN = "https://matteotidili.github.io"
PRODUCTS = ("SRI", "VMI")
STEP_MS = 5 * 60_000
HISTORY = 36  # 3 ore alle scadenze di 5 minuti, inclusa la corrente
FIELDS = ["SRI", "VMI"]
MIN_VALID = -1000.0
SCALE = 10
BIAS = 1000
MAX_CODE = 65535
CROP_BOUNDS = (7.0, 38.0, 11.1, 42.3)  # W S E N, solo per coprire Sardegna
PROJECTION = "+proj=tmerc +lat_0=42 +lon_0=12.5 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
OPERA_BASE = "https://object-store.os-api.cci1.ecmwf.int/openradar-24h-backup"
OPERA_PROJECTION = "+proj=laea +lat_0=55.0 +lon_0=10.0 +x_0=1950000.0 +y_0=-2100000.0 +ellps=WGS84"
OPERA_STEP_MS = 15 * 60_000
OPERA_HISTORY = 12  # ultime 3 ore, 15 minuti
OPERA_PIXEL_M = 2000


def api_json(url: str, payload: dict | None = None):
    headers = {"User-Agent": "SardegnaMeteoLive/1.0", "Origin": ORIGIN}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None, headers=headers,
                                      method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def source_latest(product: str) -> int:
    data = api_json(f"{BASE}/findLastProductByType?type={product}&lang=it")
    rows = data.get("lastProducts") or []
    if not rows:
        raise RuntimeError(f"{product}: nessun istante disponibile")
    timestamp = int(rows[0]["time"])
    if timestamp % STEP_MS:
        raise RuntimeError(f"{product}: timestamp fuori cadenza {timestamp}")
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if timestamp > now + STEP_MS or now - timestamp > 45 * 60_000:
        raise RuntimeError(f"{product}: ultima scansione troppo vecchia o futura")
    return timestamp


def crop_window(src):
    if src.width != 1200 or src.height != 1400 or src.count != 1 or src.dtypes[0] != "float32":
        raise ValueError(f"Geometria originale DPC inattesa: {src.width}x{src.height} {src.dtypes}")
    if not src.crs or abs(src.transform.a - 1000) > 0.01 or abs(src.transform.e + 1000) > 0.01 or abs(src.transform.c + 600000) > 0.01 or abs(src.transform.f - 650000) > 0.01:
        raise ValueError(f"CRS o trasformazione GeoTIFF DPC non conforme: {src.transform}")
    w,s,e,n = CROP_BOUNDS
    longitudes = [w + (e-w)*i/10 for i in range(11)]
    latitudes = [s + (n-s)*i/10 for i in range(11)]
    pairs = [(x,y) for x in longitudes for y in (s,n)] + [(x,y) for y in latitudes for x in (w,e)]
    px,py = geo_transform("EPSG:4326", src.crs,[x for x,_ in pairs],[y for _,y in pairs])
    left = max(0, int(math.floor((min(px)-src.transform.c)/1000))-1)
    right = min(src.width, int(math.ceil((max(px)-src.transform.c)/1000))+1)
    top = max(0, int(math.floor((src.transform.f-max(py))/1000))-1)
    bottom = min(src.height, int(math.ceil((src.transform.f-min(py))/1000))+1)
    if not (0 < left < right <= src.width and 0 < top < bottom <= src.height):
        raise ValueError("Ritaglio DPC fuori griglia")
    return rasterio.windows.Window(left,top,right-left,bottom-top)


def pack_png(arr: np.ndarray) -> Image.Image:
    valid = np.isfinite(arr) & (arr > MIN_VALID) & (arr < 5000)
    values = np.where(valid,arr,0)
    scaled = np.rint((values + BIAS) * SCALE + 1)
    packed = np.where(valid,np.clip(scaled,1,MAX_CODE),0).astype(np.uint16)
    pixels = np.empty((*packed.shape,4),dtype=np.uint8)
    pixels[:,:,0] = (packed & 255).astype(np.uint8)
    pixels[:,:,1] = (packed >> 8).astype(np.uint8)
    pixels[:,:,2] = 0
    pixels[:,:,3] = 255  # mai premoltiplicare i canali dei valori
    return Image.fromarray(pixels,"RGBA")


def fetch_frame(product: str, timestamp: int):
    stamp=datetime.fromtimestamp(timestamp/1000,timezone.utc).strftime("%Y%m%dT%H%M")
    path=ROOT / product / f"{stamp}.png"
    if path.is_file() and path.stat().st_size > 100:
        return timestamp,path,None
    try:
        signed=api_json(f"{BASE}/downloadProduct", {"productType":product,"productDate":timestamp})
        if not signed.get("url"):
            raise RuntimeError(f"Prodotto non pubblicato: {signed.get('error')}")
        request=urllib.request.Request(signed["url"],headers={"User-Agent":"SardegnaMeteoLive/1.0"})
        with urllib.request.urlopen(request,timeout=30) as resp:
            original=resp.read(9_000_001)
        if not original.startswith((b"II*\x00",b"MM\x00*")) or len(original)>9_000_000:
            raise ValueError("Sorgente non è GeoTIFF")
        from rasterio.io import MemoryFile
        with MemoryFile(original) as dataset:
            with dataset.open() as src:
                win=crop_window(src)
                field=src.read(1,window=win)
                # NoData nel prodotto Radar DPC può non essere dichiarato.
                field[(~np.isfinite(field))|(field<=MIN_VALID)] = np.nan
                valid_count=int(np.isfinite(field).sum())
                if valid_count < 200:
                    raise ValueError("Raster DPC senza copertura sufficiente")
                grid={
                    "x0_m":int(src.transform.c+win.col_off*1000),
                    "y0_m":int(src.transform.f-win.row_off*1000),
                    "pixel_m":1000,
                    "width":int(win.width),"height":int(win.height)
                }
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix(".tmp.png")
        pack_png(field).save(temp,"PNG",optimize=True)
        os.replace(temp,path)
        print(f"{product} {stamp}: {valid_count} celle valide, PNG={path.stat().st_size} B",flush=True)
        return timestamp,path,grid
    except (urllib.error.HTTPError,TimeoutError,ValueError,RuntimeError,OSError) as exc:
        print(f"{product} {stamp}: non disponibile {type(exc).__name__}: {str(exc)[:95]}",flush=True)
        return timestamp,None,None




def opera_crop_window(shape: tuple[int, int]):
    """Finestra OPERA RATE su Sardegna, restituita in coordinate pixel ODIM."""
    height, width = shape
    xscale, yscale = 3_800_000.0 / width, 4_400_000.0 / height
    w, s, e, n = CROP_BOUNDS
    transformer = Transformer.from_crs("EPSG:4326", OPERA_PROJECTION, always_xy=True)
    longitudes = [w + (e - w) * i / 10 for i in range(11)]
    latitudes = [s + (n - s) * i / 10 for i in range(11)]
    pairs = [(x, y) for x in longitudes for y in (s, n)] + [(x, y) for y in latitudes for x in (w, e)]
    px, py = transformer.transform([x for x, _ in pairs], [y for _, y in pairs])
    left = max(0, int(math.floor(min(px) / xscale)) - 1)
    right = min(width, int(math.ceil(max(px) / xscale)) + 1)
    top = max(0, int(math.floor((0 - max(py)) / yscale)) - 1)
    bottom = min(height, int(math.ceil((0 - min(py)) / yscale)) + 1)
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("Ritaglio OPERA fuori griglia")
    window = (left, top, right, bottom)
    grid = {
        "x0_m": int(round(left * xscale)),
        "y0_m": int(round(-top * yscale)),
        "pixel_m": int(round(xscale)),
        "width": right - left,
        "height": bottom - top,
    }
    return window, grid


def decode_opera_rate(payload: bytes) -> np.ndarray:
    """Decodifica il campo RATE ufficiale da un prodotto composite ODIM HDF5."""
    with h5py.File(io.BytesIO(payload), "r") as h5:
        for ds_name in sorted((n for n in h5 if n.startswith("dataset")), key=lambda n: int(n[7:]) if n[7:].isdigit() else 10**9):
            ds = h5[ds_name]
            for data_name in sorted((n for n in ds if n.startswith("data")), key=lambda n: int(n[4:]) if n[4:].isdigit() else 10**9):
                group = ds[data_name]
                if "what" not in group or "data" not in group:
                    continue
                attrs = {}
                for key, value in group["what"].attrs.items():
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    elif isinstance(value, np.generic):
                        value = value.item()
                    attrs[key] = value
                if str(attrs.get("quantity", "")).upper() != "RATE":
                    continue
                raw = group["data"][:]
                values = raw.astype(np.float32) * float(attrs.get("gain", 1.0)) + float(attrs.get("offset", 0.0))
                nodata, undetect = attrs.get("nodata"), attrs.get("undetect")
                if nodata is not None:
                    values[raw == nodata] = np.nan
                if undetect is not None:
                    values[raw == undetect] = 0.0
                return values
    raise ValueError("Prodotto OPERA senza quantità RATE")


def fetch_opera_frame(timestamp: int):
    stamp = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
    key = f"{stamp:%Y/%m/%d}/OPERA/COMP/OPERA@{stamp:%Y%m%dT%H%M}@0@RATE.h5"
    path = ROOT / "OPERA" / f"{stamp:%Y%m%dT%H%M}.png"
    if path.is_file() and path.stat().st_size > 100:
        return timestamp, path, None
    try:
        request = urllib.request.Request(f"{OPERA_BASE}/{key}", headers={"User-Agent": "SardegnaMeteoLive/1.0"})
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = response.read(15_000_001)
        if not payload.startswith(b"\x89HDF\r\n\x1a\n") or len(payload) > 15_000_000:
            raise ValueError("Sorgente OPERA non è ODIM HDF5")
        values = decode_opera_rate(payload)
        window, grid = opera_crop_window(values.shape)
        left, top, right, bottom = window
        values = values[top:bottom, left:right]
        if int(np.isfinite(values).sum()) < 100:
            raise ValueError("Campo OPERA RATE senza copertura sufficiente")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp.png")
        pack_png(values).save(temp, "PNG", optimize=True)
        os.replace(temp, path)
        print(f"OPERA RATE {stamp:%Y%m%dT%H%M}: {int(np.isfinite(values).sum())} celle valide, PNG={path.stat().st_size} B", flush=True)
        return timestamp, path, grid
    except (urllib.error.HTTPError, TimeoutError, ValueError, OSError, KeyError) as exc:
        print(f"OPERA RATE {stamp:%Y%m%dT%H%M}: non disponibile {type(exc).__name__}: {str(exc)[:95]}", flush=True)
        return timestamp, None, None


def collect_opera(previous: dict):
    """Conserva o aggiorna le ultime 3 ore del prodotto ufficiale OPERA RATE."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    latest = now_ms - (now_ms % OPERA_STEP_MS)
    timestamps = [latest - i * OPERA_STEP_MS for i in range(OPERA_HISTORY, -1, -1)]
    previous_meta = previous.get("opera") or {}
    grid = previous_meta.get("grid")
    frames = []
    for timestamp in timestamps:
        stamp, path, found_grid = fetch_opera_frame(timestamp)
        if path is None:
            continue
        if found_grid is not None:
            if grid is not None and grid != found_grid:
                raise ValueError(f"Griglia OPERA variata: {grid} != {found_grid}")
            grid = found_grid
        frames.append({
            "time": datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "file": path.as_posix(),
        })
    if not frames or not grid:
        return previous_meta or None
    return {
        "grid": grid,
        "crs_proj4": OPERA_PROJECTION,
        "encoding": {"format": "PNG-RG16", "nodata_code": 0, "scale": SCALE, "bias": BIAS,
                     "decode": "(R + 256*G - 1) / scale - bias", "precision": "0.1 mm/h"},
        "frames": frames,
    }


def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    previous={}
    if MANIFEST.is_file():
        try: previous=json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception: pass
    kept={}
    grid=previous.get("grid")
    max_times={}
    for product in PRODUCTS:
        try: latest=source_latest(product)
        except Exception as exc:
            print(f"{product}: API temporaneamente non disponibile: {exc}",flush=True)
            continue
        max_times[product]=latest
        tlist=[latest-i*STEP_MS for i in range(HISTORY,-1,-1)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda t:fetch_frame(product,t),tlist))
        entries=[]
        for stamp,path,g in results:
            if path is None: continue
            if g is not None:
                if grid is not None and grid != g:
                    raise ValueError(f"Griglia DPC variata: {grid} != {g}")
                grid=g
            entries.append({"time":datetime.fromtimestamp(stamp/1000,timezone.utc).isoformat().replace("+00:00","Z"),
                            "file":path.as_posix()})
        kept[product]=entries
        print(f"{product}: {len(entries)} fotogrammi numerici nell'ultima finestra di 3 h",flush=True)

    opera_meta = collect_opera(previous)
    if opera_meta:
        print(f"OPERA RATE: {len(opera_meta.get('frames', []))} fotogrammi numerici nell'ultima finestra di 3 h", flush=True)

    if not kept or not grid:
        print("Nessun nuovo dato completo; il precedente archivio non viene sovrascritto.",flush=True)
        return
    # Se la sorgente di un prodotto è momentaneamente assente, preservare i suoi
    # file e segnalare data ed età originali; mai inventare la scansione corrente.
    for product in PRODUCTS:
        if product not in kept:
            kept[product]=previous.get("frames",{}).get(product,[])
    manifest={
        "schema_version":1,
        "source":"Radar-DPC – Dipartimento della Protezione Civile",
        "source_url":"https://radar.protezionecivile.it",
        "license":"CC BY-SA 4.0",
        "generated_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "crs_proj4":PROJECTION,
        "grid":grid,
        "encoding":{"format":"PNG-RG16","nodata_code":0,"scale":SCALE,"bias":BIAS,
                    "decode":"(R + 256*G - 1) / scale - bias","precision":"0.1 dBZ / 0.1 mm/h"},
        "units":{"SRI":"mm/h","VMI":"dBZ"},
        "frames":kept,
    }
    if opera_meta:
        manifest["opera"] = opera_meta
    MANIFEST.write_text(json.dumps(manifest,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
    published={row["file"] for rows in kept.values() for row in rows}
    if opera_meta:
        published.update(row["file"] for row in opera_meta.get("frames", []))
    for path in ROOT.glob("*/*.png"):
        if path.as_posix() not in published:
            path.unlink()
    print("Manifest generato:",{k:len(v) for k,v in kept.items()},"grid",grid,flush=True)


if __name__ == "__main__":
    main()
