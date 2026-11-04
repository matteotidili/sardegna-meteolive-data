#!/usr/bin/env python3
"""Quota del terreno presso le coordinate delle PWS prive di quota dichiarata.

Fonte prioritaria: RAS DTM 10m Altimetria REV01, WMS GetFeatureInfo
(raster:DTM_10M_ALTIMETRIA_REV01_GEOS). SRTM30m e' fallback distinto.
Non altera l'altitudine ufficiale, gli updater osservativi ne' le API WU.
La cache e' persistente e invalidata se cambia la posizione della stazione.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("data/station_elevations.json")
SOURCES = (
    ("wunderground", Path("data/wunderground_stations.json")),
    ("cedoc", Path("data/cedoc_sites.json")),
    ("weatherlink", Path("data/weatherlink.json")),
    ("ecowitt", Path("data/ecowitt.json")),
)
WMS = "https://webgis.regione.sardegna.it/geoserverraster/ows"
LAYER = "raster:DTM_10M_ALTIMETRIA_REV01_GEOS"
SRTM = "https://api.opentopodata.org/v1/srtm30m"
MAX_WORKERS = 4


def read_json(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def location_is_valid(lat, lon):
    return (is_number(lat) and is_number(lon)
            and 38.6 <= lat <= 41.5 and 7.5 <= lon <= 10.4)


def key(site):
    return str(site["network"]) + "|" + str(site["station_id"])


def matches_cached(a, b):
    return (abs(a["lat"] - b["lat"]) <= 0.00005
            and abs(a["lon"] - b["lon"]) <= 0.00005
            and is_number(a.get("elev"))
            and -20 <= a["elev"] <= 2100
            and a.get("method") in {"regional_dtm_10m", "srtm30m"})


def request_json(url, timeout=14):
    req = urllib.request.Request(url, headers={"User-Agent": "SardegnaMeteoLive/1.0 (terrain-station-elevations)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dtm_at_point(site):
    """Raster GRAY_INDEX e' una quota numerica; -9999 indica NoData."""
    lat, lon = site["lat"], site["lon"]
    d = 0.0002  # bbox ~40 m, pixel centrale corrispondente alla PWS
    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
        "layers": LAYER, "query_layers": LAYER, "styles": "",
        "srs": "EPSG:4326",
        "bbox": f"{lon-d:.7f},{lat-d:.7f},{lon+d:.7f},{lat+d:.7f}",
        "width": 101, "height": 101, "x": 50, "y": 50,
        "feature_count": 1, "info_format": "application/json",
    }
    payload = request_json(WMS + "?" + urllib.parse.urlencode(params))
    features = payload.get("features") or []
    attrs = (features[0].get("properties") or {}) if features else {}
    h = attrs.get("GRAY_INDEX")
    if is_number(h) and -20 <= h <= 2100:
        return {
            **site, "elev": round(h), "method": "regional_dtm_10m",
            "source": "Regione Sardegna - DTM 10m Altimetria REV01",
            "resolution_m": 10,
        }
    return None


def fallback_srtm(sites):
    """Solo i punti per cui il WMS regionale non ha fornito una quota."""
    results = {}
    for start in range(0, len(sites), 75):
        batch = sites[start:start + 75]
        loc = "|".join(f"{s['lat']:.6f},{s['lon']:.6f}" for s in batch)
        params = urllib.parse.urlencode({"locations": loc, "interpolation": "bilinear"})
        try:
            payload = request_json(SRTM + "?" + params, timeout=20)
            data = payload.get("results") or []
            if payload.get("status") != "OK" or len(data) != len(batch):
                raise ValueError("Risposta SRTM incompleta o non valida")
            for s, record in zip(batch, data):
                h = record.get("elevation")
                if is_number(h) and -20 <= h <= 2100:
                    results[key(s)] = {
                        **s, "elev": round(h), "method": "srtm30m",
                        "source": "NASA SRTM 30m (Open Topo Data)",
                        "resolution_m": 30,
                    }
        except Exception as exc:
            print(f"Quota SRTM batch {start}: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        if start + 75 < len(sites):
            time.sleep(1.2)
    return results


def gather_sites():
    found = {}
    for network, path in SOURCES:
        obj = read_json(path)
        for row in obj.get("stations", []):
            if not isinstance(row, dict):
                continue
            sid = row.get("station_id")
            lat, lon = row.get("lat"), row.get("lon")
            if not sid or not location_is_valid(lat, lon):
                continue
            # L'altitudine dichiarata resta sempre prioritaria.
            if network != "ecowitt" and is_number(row.get("elev")):
                continue
            site = {
                "network": network, "station_id": str(sid),
                "name": str(row.get("name") or sid),
                "lat": round(float(lat), 6), "lon": round(float(lon), 6),
            }
            found[key(site)] = site
    return list(found.values())


def main():
    sites = gather_sites()
    cached = read_json(OUT)
    previous = {
        key(item): item for item in cached.get("stations", [])
        if isinstance(item, dict) and item.get("network") and item.get("station_id")
    }
    results = {}
    pending = []
    for s in sites:
        old = previous.get(key(s))
        if old and matches_cached(old, s):
            results[key(s)] = {**old, "name": s["name"]}
        else:
            pending.append(s)

    print(f"Stazioni da stimare: {len(sites)}; in cache: {len(results)}; nuove: {len(pending)}", flush=True)
    fallback = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(dtm_at_point, s): s for s in pending}
        for i, future in enumerate(as_completed(futures), start=1):
            site = futures[future]
            try:
                row = future.result()
                if row:
                    results[key(site)] = row
                else:
                    fallback.append(site)
            except Exception as exc:
                print(f"DTM {site['station_id']}: {type(exc).__name__}: {str(exc)[:150]}", flush=True)
                fallback.append(site)
            if i % 75 == 0:
                print(f"DTM analizzate: {i}/{len(pending)}", flush=True)

    results.update(fallback_srtm(fallback))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    stations = sorted(results.values(), key=lambda x: (x["network"], x["station_id"]))
    counts = {name: sum(s["method"] == name for s in stations)
              for name in ("regional_dtm_10m", "srtm30m")}
    document = {
        "generated_at": stamp,
        "description": "Quota del terreno stimata presso coordinate; non e' quota dichiarata del sensore",
        "primary_source": "Regione Sardegna DTM 10m Altimetria REV01",
        "fallback_source": "NASA SRTM 30m / Open Topo Data",
        "requested_station_count": len(sites),
        "station_count": len(stations),
        "missing_station_count": len(sites) - len(stations),
        "source_counts": counts,
        "stations": stations,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
                   encoding="utf-8")
    print(f"Quote pubblicate: {len(stations)}/{len(sites)}; regionali: "
          f"{counts['regional_dtm_10m']}; SRTM: {counts['srtm30m']}", flush=True)


if __name__ == "__main__":
    main()
