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

# PWS confermate direttamente su Weather Underground e usate come seed.
# Restano nell'inventario anche quando l'endpoint "near" non le restituisce.
KNOWN_PWS = {
    "ICASTI188": {"station_id": "ICASTI188", "name": "Olia Speciosa", "lat": 39.28, "lon": 9.53},
    "ICASTI47": {"station_id": "ICASTI47", "name": "Castiadas", "lat": 39.20, "lon": 9.55},
    "IVILLA845": {"station_id": "IVILLA845", "name": "Villasimius", "lat": 39.15, "lon": 9.52},
    "IVILLA927": {"station_id": "IVILLA927", "name": "Villasimius", "lat": 39.14, "lon": 9.53},
}

# Inviluppo geografico prudente della Sardegna e delle isole minori.
# Il precedente poligono semplificato tagliava parte della costa sud-orientale
# (Costa Rei/Villasimius) e quindi escludeva PWS reali prima della discovery.
REGION = {
    "lat_min": 38.80,
    "lat_max": 41.32,
    "lon_min": 7.95,
    "lon_max": 9.90,
}

def inside_region(lon: float, lat: float) -> bool:
    return (
        REGION["lon_min"] <= lon <= REGION["lon_max"]
        and REGION["lat_min"] <= lat <= REGION["lat_max"]
    )

def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

def saturated_nearby(rows, lat: float, lon: float, max_km: float = 14.0) -> bool:
    if len(rows) < 10:
        return False
    return any(distance_km(lat, lon, row["lat"], row["lon"]) <= max_km for row in rows)

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

    # Griglia uniforme abbastanza fitta su tutta la Sardegna.
    # In questo modo intercettiamo anche piccoli cluster costieri/rurali che non
    # saturano l'endpoint e che una griglia regionale troppo larga può saltare.
    base_step = 0.07
    latitudes = list(frange(REGION["lat_min"], REGION["lat_max"], base_step))
    longitudes = list(frange(REGION["lon_min"], REGION["lon_max"], base_step))

    # Manteniamo le stazioni già note: una discovery successiva deve aggiungere,
    # non perdere PWS solo perché l'endpoint "near" non le restituisce quel giorno.
    found = dict(KNOWN_PWS)
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
            for row in previous.get("stations") or []:
                sid = row.get("station_id")
                if sid and row.get("lat") is not None and row.get("lon") is not None:
                    found[sid] = row
        except Exception as exc:
            print("AVVISO inventario precedente non leggibile:", exc)

    calls = 0
    visited = set()

    def query_point(lat, lon):
        nonlocal calls
        keypt = (round(lat, 4), round(lon, 4))
        if keypt in visited or not inside_region(lon, lat):
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
                if inside_region(row["lon"], row["lat"]):
                    found[row["station_id"]] = row
            time.sleep(0.08)
            return rows
        except Exception as exc:
            print(f"AVVISO punto {lat},{lon}: {exc}")
            return []

    # Passaggio dedicato sul sud-est (Villasimius-Castiadas-Costa Rei-Muravera):
    # qui piccoli cluster di 2-5 PWS possono sfuggire alla griglia regionale perché
    # non saturano mai il limite di 10 risultati dell'endpoint.
    for lat in frange(39.05, 39.52, 0.025):
        for lon in frange(9.42, 9.72, 0.025):
            query_point(lat, lon)

    saturated = []
    for lat in latitudes:
        for lon in longitudes:
            rows = query_point(lat, lon)
            if saturated_nearby(rows, lat, lon):
                saturated.append((lat, lon))

    # Raffinamento nelle celle dense: circa 2-3 km.
    fine_step = 0.025
    saturated_fine = []
    for lat, lon in saturated:
        for dy in (-fine_step, 0.0, fine_step):
            for dx in (-fine_step, 0.0, fine_step):
                rows = query_point(lat + dy, lon + dx)
                if saturated_nearby(rows, lat + dy, lon + dx):
                    saturated_fine.append((lat + dy, lon + dx))

    # Ultimo passaggio nei cluster ancora saturi: circa 1 km.
    micro_step = 0.010
    for lat, lon in saturated_fine[:220]:
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
