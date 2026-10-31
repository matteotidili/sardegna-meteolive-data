#!/usr/bin/env python3
"""Scopre le PWS Weather Underground presenti in Sardegna.

Richiede il secret GitHub Actions WU_APP_API_KEY.
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
import urllib.error
from wu_api_health import (
    load_state, in_cooldown, record_failure, record_success, save_state
)
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED_WU_STATIONS = {"ISERRA56"}  # PWS Serramanna non affidabile
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

# Ricerca incrementale: circa una ventina di giorni per coprire tutta
# la griglia, senza mai interrogare migliaia di punti in una sola run.
# È possibile usare il workflow sud-est con WU_DISCOVERY_REGION=southeast.
MIN_CATALOG_SIZE = 300
DISCOVERY_BATCH = 12


def discovery_points(region="all"):
    # La griglia regionale viene alternata a una più densa nel sud-est.
    # Le stazioni nuove vengono aggiunte, quelle vecchie mai cancellate.
    points = []
    for lat in frange(REGION["lat_min"], REGION["lat_max"], 0.16):
        for lon in frange(REGION["lon_min"], REGION["lon_max"], 0.16):
            if inside_region(lon, lat):
                points.append((lat, lon))
    southeast = [
        (lat, lon)
        for lat in frange(39.05, 39.52, 0.055)
        for lon in frange(9.41, 9.73, 0.055)
        if inside_region(lon, lat)
    ]
    if region == "southeast":
        return southeast
    # Inserisce dei punti SE ogni pochi punti regionali.
    mixed = []
    for i in range(max(len(points), len(southeast) * 4)):
        if i < len(points):
            mixed.append(points[i])
        if i % 4 == 0 and i // 4 < len(southeast):
            mixed.append(southeast[i // 4])
    # Stesso ordine a ogni ciclo: il cursore è persistente su GitHub.
    return list(dict.fromkeys(mixed))


def main():
    state = load_state()
    if in_cooldown(state):
        print("WU discovery sospesa per protezione quota, prossima prova dopo "
              + str(state.get("blocked_until")), flush=True)
        return
    key = os.environ.get("WU_APP_API_KEY")
    if not key:
        print("WU discovery: chiave dedicata WU_APP_API_KEY assente; " 
              "inventario conservato e nessun accesso alla chiave personale.", flush=True)
        return

    region = os.environ.get("WU_DISCOVERY_REGION", "all")
    budget = min(DISCOVERY_BATCH, max(1, int(os.environ.get("WU_DISCOVERY_BATCH", DISCOVERY_BATCH))))
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Inventario WU illeggibile: scansione annullata senza cancellazioni") from exc
        if not isinstance(previous, dict) or not isinstance(previous.get("stations"), list):
            raise RuntimeError("Inventario WU invalido: scansione annullata")
        if len(previous["stations"]) < MIN_CATALOG_SIZE:
            raise RuntimeError("Inventario WU sotto la soglia di sicurezza (300 PWS): scansione annullata")
    else:
        previous = {}

    found = {
        s["station_id"]: s for s in (previous.get("stations") or [])
        if isinstance(s, dict) and s.get("station_id")
    }
    for sid, row in KNOWN_PWS.items():
        found.setdefault(sid, row)
    count_before = len(found)
    points = discovery_points(region)
    if not points:
        print("WU discovery: nessun punto geografico disponibile")
        return
    cursor_key = "discovery_se_cursor" if region == "southeast" else "discovery_cursor"
    cursor = int(previous.get(cursor_key, 0) or 0) % len(points)
    attempted = 0
    completed = 0
    errors = 0
    throttle = None
    new_ids = []
    print(f"WU discovery: {region}, {len(points)} punti, fino a {budget} richieste, "
          f"cursore {cursor}", flush=True)

    for i in range(min(budget, len(points))):
        lat, lon = points[(cursor + i) % len(points)]
        q = urllib.parse.urlencode({
            "geocode": f"{lat:.4f},{lon:.4f}",
            "product": "pws", "format": "json", "apiKey": key,
        })
        attempted += 1
        try:
            payload = get_json(BASE + "?" + q)
            completed += 1
            for row in rows_from_location(payload):
                if not inside_region(row["lon"], row["lat"]):
                    continue
                sid = row["station_id"]
                if sid in EXCLUDED_WU_STATIONS:
                    continue
                if sid not in found:
                    new_ids.append(sid)
                found[sid] = row
        except urllib.error.HTTPError as exc:
            errors += 1
            if exc.code in (401, 403, 429):
                throttle = exc.code
                state = record_failure(state, exc.code, "Quota/autenticazione WU discovery")
                save_state(state)
                print(f"WU discovery: HTTP {exc.code}, circuit breaker attivato fino a "
                      + str(state.get("blocked_until")), flush=True)
                break
            print(f"WU discovery: errore HTTP {exc.code} sul punto {lat},{lon}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"WU discovery: errore transitorio al punto {lat},{lon}: "
                  + str(exc)[:100], flush=True)
        time.sleep(0.15)

    if completed:
        save_state(record_success(state)) if throttle is None else None
    if attempted:
        # È importante pubblicare anche l'avanzamento della ricerca:
        # così al prossimo giorno si esplorano nuove aree e non le stesse.
        previous[cursor_key] = (cursor + attempted) % len(points)
    if not attempted and not new_ids:
        return
    stations = sorted(found.values(),
        key=lambda x: ((x.get("name") or "").casefold(), x["station_id"]))
    out = dict(previous)
    out.update({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "Weather Underground / The Weather Company PWS",
        "region": "Sardegna",
        "station_count": len(stations),
        "discovery_calls": int(previous.get("discovery_calls", 0) or 0) + completed,
        "last_discovery_calls": completed,
        "last_discovery_errors": errors,
        "last_new_station_ids": new_ids,
        "discovery_last_status": "blocked" if throttle else "ok",
        "stations": stations,
    })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n",
                   encoding="utf-8")
    print(f"WU discovery: catalogo {count_before} -> {len(stations)}; "
          f"nuove={len(new_ids)}, query={completed}, errori={errors}", flush=True)


if __name__ == "__main__":
    main()
