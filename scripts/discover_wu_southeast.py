#!/usr/bin/env python3
"""Discovery WU ad alta risoluzione per la Sardegna sud-orientale.

Scansiona con passo ~1 km il settore Villasimius-Castiadas-Costa Rei-Muravera,
unisce le PWS trovate all'inventario regionale esistente e non elimina mai
stazioni già note.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("data/wunderground_stations.json")
BASE = "https://api.weather.com/v3/location/near"

LAT_MIN, LAT_MAX = 39.08, 39.48
LON_MIN, LON_MAX = 9.40, 9.72
STEP = 0.01

def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "sardegna-meteolive/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def as_list(v):
    return v if isinstance(v, list) else ([] if v is None else [v])

def rows_from_location(payload: dict):
    loc = payload.get("location") or {}
    ids = as_list(loc.get("stationId"))
    names = as_list(loc.get("stationName"))
    lats = as_list(loc.get("latitude"))
    lons = as_list(loc.get("longitude"))
    qc = as_list(loc.get("qcStatus"))
    upd = as_list(loc.get("updateTimeUtc"))
    n = max(map(len, [ids, names, lats, lons, qc, upd]), default=0)

    def at(arr, i):
        if not arr:
            return None
        if len(arr) == 1 and n > 1:
            return arr[0]
        return arr[i] if i < len(arr) else None

    for i in range(n):
        sid = at(ids, i)
        lat = at(lats, i)
        lon = at(lons, i)
        if not sid or lat is None or lon is None:
            continue
        yield {
            "station_id": str(sid),
            "name": at(names, i) or str(sid),
            "lat": float(lat),
            "lon": float(lon),
            "qc_status": at(qc, i),
            "update_time_utc": at(upd, i),
        }

def frange(start: float, stop: float, step: float):
    x = start
    while x <= stop + 1e-9:
        yield round(x, 4)
        x += step

def in_se_box(lat: float, lon: float) -> bool:
    # Buffer leggero: conserva anche PWS appena fuori dal rettangolo di query.
    return 39.02 <= lat <= 39.54 and 9.34 <= lon <= 9.78

def main():
    key = os.environ.get("WU_API_KEY")
    if not key:
        raise RuntimeError("Manca WU_API_KEY nei GitHub Actions Secrets")

    if OUT.exists():
        current = json.loads(OUT.read_text(encoding="utf-8"))
    else:
        current = {"stations": []}

    found = {
        row["station_id"]: row
        for row in (current.get("stations") or [])
        if row.get("station_id")
    }
    before = len(found)
    discovered_se = set()
    calls = 0
    errors = 0

    for lat in frange(LAT_MIN, LAT_MAX, STEP):
        for lon in frange(LON_MIN, LON_MAX, STEP):
            q = urllib.parse.urlencode({
                "geocode": f"{lat:.4f},{lon:.4f}",
                "product": "pws",
                "format": "json",
                "apiKey": key,
            })
            try:
                payload = get_json(BASE + "?" + q)
                calls += 1
                for row in rows_from_location(payload):
                    if in_se_box(row["lat"], row["lon"]):
                        found[row["station_id"]] = row
                        discovered_se.add(row["station_id"])
            except Exception as exc:
                errors += 1
                print(f"AVVISO {lat},{lon}: {exc}")
            time.sleep(0.05)

    stations = sorted(found.values(), key=lambda x: ((x.get("name") or "").casefold(), x["station_id"]))
    result = {
        **{k: v for k, v in current.items() if k != "stations"},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "Weather Underground / The Weather Company PWS",
        "region": "Sardegna",
        "station_count": len(stations),
        "se_discovery_calls": calls,
        "se_discovery_errors": errors,
        "se_station_count": len(discovered_se),
        "stations": stations,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        f"Discovery SE completata: {calls} query, {len(discovered_se)} PWS nel settore, "
        f"inventario {before} -> {len(stations)}, errori {errors}"
    )

if __name__ == "__main__":
    main()
