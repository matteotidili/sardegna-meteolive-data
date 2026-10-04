#!/usr/bin/env python3
"""Scopre le PWS Weather Underground presenti in Sardegna.

Richiede il secret GitHub Actions WU_API_KEY.
Scrive data/wunderground_stations.json.

La ricerca usa l'endpoint ufficiale Location Service Near, che restituisce
fino a 10 PWS vicine a ciascun punto della griglia. I risultati vengono
deduplicati per stationId e filtrati con un poligono approssimato della Sardegna.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("data/wunderground_stations.json")
BASE = "https://api.weather.com/v3/location/near"

# Profilo approssimato della Sardegna (lon, lat), sufficiente per escludere
# stazioni esterne restituite come "nearest" nei punti costieri.
SARDINIA = [
    (8.10, 39.00), (8.20, 38.86), (8.55, 38.86), (8.85, 38.93),
    (9.12, 39.08), (9.36, 39.18), (9.52, 39.45), (9.63, 39.82),
    (9.71, 40.16), (9.80, 40.48), (9.79, 40.82), (9.67, 41.05),
    (9.48, 41.25), (9.20, 41.30), (8.90, 41.24), (8.58, 41.16),
    (8.32, 40.98), (8.12, 40.74), (8.05, 40.45), (8.03, 40.10),
    (8.00, 39.72), (8.02, 39.35), (8.10, 39.00)
]

def inside_polygon(lon: float, lat: float) -> bool:
    inside = False
    j = len(SARDINIA) - 1
    for i, (xi, yi) in enumerate(SARDINIA):
        xj, yj = SARDINIA[j]
        hit = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if hit:
            inside = not inside
        j = i
    return inside

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

def main():
    key = os.environ.get("WU_API_KEY")
    if not key:
        raise RuntimeError("Manca WU_API_KEY nei GitHub Actions Secrets")

    # Griglia regionale + raffinamento adattivo nelle aree dense.
    # L'endpoint restituisce al massimo 10 PWS: quando una query è satura,
    # interroghiamo punti più ravvicinati attorno a quella cella per non perdere
    # le stazioni dei cluster urbani (Cagliari, Sassari, Olbia, Alghero, ecc.).
    base_step = 0.18
    latitudes = list(frange(38.90, 41.30, base_step))
    longitudes = list(frange(8.05, 9.75, base_step))

    found = {}
    calls = 0
    visited = set()

    def query_point(lat, lon):
        nonlocal calls
        keypt = (round(lat, 4), round(lon, 4))
        if keypt in visited or not inside_polygon(lon, lat):
            return []
        visited.add(keypt)
        q = urllib.parse.urlencode({
            "geocode": f"{lat:.4f},{lon:.4f}",
            "product": "pws",
            "format": "json",
            "apiKey": key,
        })
        try:
            payload = get_json(BASE + "?" + q)
            calls += 1
            rows = list(rows_from_location(payload))
            for row in rows:
                if inside_polygon(row["lon"], row["lat"]):
                    found[row["station_id"]] = row
            time.sleep(0.08)
            return rows
        except Exception as exc:
            print(f"AVVISO punto {lat},{lon}: {exc}")
            return []

    saturated = []
    for lat in latitudes:
        for lon in longitudes:
            rows = query_point(lat, lon)
            if len(rows) >= 10:
                saturated.append((lat, lon))

    # Primo raffinamento: circa 5 km tra i punti.
    fine_step = 0.045
    saturated_fine = []
    for lat, lon in saturated:
        for dy in (-fine_step, 0.0, fine_step):
            for dx in (-fine_step, 0.0, fine_step):
                rows = query_point(lat + dy, lon + dx)
                if len(rows) >= 10:
                    saturated_fine.append((lat + dy, lon + dx))

    # Secondo raffinamento, solo dove anche la griglia fine è ancora satura:
    # circa 1.7 km tra i punti. Un tetto evita esplosioni di chiamate.
    micro_step = 0.015
    for lat, lon in saturated_fine[:120]:
        for dy in (-micro_step, 0.0, micro_step):
            for dx in (-micro_step, 0.0, micro_step):
                query_point(lat + dy, lon + dx)

    stations = sorted(found.values(), key=lambda x: (x["name"].casefold(), x["station_id"]))
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "Weather Underground / The Weather Company PWS",
        "region": "Sardegna",
        "discovery_calls": calls,
        "station_count": len(stations),
        "stations": stations,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Trovate {len(stations)} PWS in Sardegna con {calls} query")

if __name__ == "__main__":
    main()
