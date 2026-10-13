#!/usr/bin/env python3
"""WeatherLink v2: ingest only Sardinian stations the configured API account can access.

The public WeatherLink map is NOT an open data API. /stations and /current
require a WeatherLink v2 API key and secret, plus per-station access rights.
No private WeatherLink stations or credentials are ever published.
"""
import json
import math
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.weatherlink.com/v2"
ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "weatherlink.json"
EXISTING = [ROOT / "data" / name for name in ("stations.json", "am.json", "wunderground.json")]
# Approximate bounding box for Sardinia and nearby islands, excluding Corsica.
BOUNDS = (38.65, 41.32, 7.70, 10.60)
MAX_STATIONS_PER_RUN = 200
MAX_AGE_SECONDS = 90 * 60
UTC = timezone.utc


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def celsius(f):
    return round((f - 32) * (5 / 9), 1) if f is not None else None


def kmh(mph):
    return round(mph * 1.609344, 1) if mph is not None else None


def first_num(item, *keys):
    for key in keys:
        value = number(item.get(key))
        if value is not None:
            return value
    return None


def weatherlink_get(path, key, secret):
    url = BASE + path + "?" + urllib.parse.urlencode({"api-key": key})
    req = urllib.request.Request(
        url, headers={"X-Api-Secret": secret, "Accept": "application/json",
                      "User-Agent": "SardegnaMeteoLive/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def in_sardinia(station):
    lat, lon = number(station.get("latitude")), number(station.get("longitude"))
    return (lat is not None and lon is not None and
            BOUNDS[0] <= lat <= BOUNDS[1] and BOUNDS[2] <= lon <= BOUNDS[3])


def normalized_name(name):
    basic = unicodedata.normalize("NFKD", str(name or ""))
    basic = "".join(c for c in basic if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", basic.lower())


def distance_m(a, b, c, d):
    lat1, lat2 = math.radians(a), math.radians(c)
    dlat, dlon = math.radians(c - a), math.radians(d - b)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 12742000 * math.asin(min(1, math.sqrt(h)))


def existing_stations():
    rows = []
    for path in EXISTING:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows.extend(payload.get("stations") or [])
        except (OSError, ValueError):
            pass
    return rows


def likely_duplicate(station, existing):
    name = normalized_name(station.get("station_name"))
    lat, lon = number(station.get("latitude")), number(station.get("longitude"))
    if not name or lat is None or lon is None:
        return False
    for other in existing:
        if normalized_name(other.get("name")) != name:
            continue
        y, x = number(other.get("lat")), number(other.get("lon"))
        # Suppress only very close stations with the same exact normalized name.
        # Identically named stations farther apart remain distinct.
        if y is not None and x is not None and distance_m(lat, lon, y, x) < 40:
            return True
    return False


def normalize_current(station, payload, now):
    # Do not merge indoor temperatures, AirLink temperatures, historic fields
    # or records from different timestamps. Only outdoor ISS current types.
    allowed = {1, 2, 6, 10, 23}
    candidates = []
    for sensor in payload.get("sensors") or []:
        if int(sensor.get("data_structure_type") or 0) not in allowed:
            continue
        for rec in sensor.get("data") or []:
            ts = number(rec.get("ts"))
            temp_f = first_num(rec, "temp_out", "temp")
            if ts is None or temp_f is None:
                continue
            if ts > now + 300 or now - ts > MAX_AGE_SECONDS:
                continue
            candidates.append((ts, rec))
    if not candidates:
        return None
    ts, obs = max(candidates, key=lambda pair: pair[0])
    temp_f = first_num(obs, "temp_out", "temp")
    if temp_f is None or not -100 <= temp_f <= 140:
        return None
    rh = first_num(obs, "hum_out", "hum")
    wind_mph = first_num(obs, "wind_speed_last", "wind_speed_avg_last_10_min",
                         "wind_speed_avg", "wind_speed")
    wind_dir = first_num(obs, "wind_dir_last", "wind_dir_scalar_avg_last_10_min",
                         "wind_dir_of_prevail")
    rate = first_num(obs, "rain_rate_last_mm")
    rainfall = first_num(obs, "rainfall_daily_mm")
    bar_inhg = first_num(obs, "bar_sea_level", "bar")
    # WeatherLink /current does not consistently provide today's true
    # temperature extrema. Do not mislabel a current sample as Tmin/Tmax.
    return {
        "station_id": str(station.get("station_id_uuid") or station["station_id"]),
        "network": "weatherlink",
        "name": str(station.get("station_name") or station.get("city") or "Davis WeatherLink"),
        "lat": number(station.get("latitude")),
        "lon": number(station.get("longitude")),
        "elev": number(station.get("elevation")),
        "temp": celsius(temp_f),
        "tmin": None, "tmax": None,
        "rh": round(rh) if rh is not None and 0 <= rh <= 100 else None,
        "wind_kmh": kmh(wind_mph) if wind_mph is not None and 0 <= wind_mph <= 250 else None,
        "wind_dir": round(wind_dir) if wind_dir is not None and 0 <= wind_dir <= 360 else None,
        "gust_kmh": None,
        "rain_rate": round(rate, 1) if rate is not None and rate >= 0 else None,
        "rain_today": round(rainfall, 1) if rainfall is not None and rainfall >= 0 else None,
        "pressure_hpa": round(bar_inhg * 33.8638867, 1) if bar_inhg is not None else None,
        "last": datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stale_min": round((now - ts) / 60),
    }


def main():
    key = os.getenv("WEATHERLINK_API_KEY", "").strip()
    secret = os.getenv("WEATHERLINK_API_SECRET", "").strip()
    if not key or not secret:
        print("WeatherLink non configurato: nessuna pubblicazione, dati esistenti invariati.")
        return
    stations = weatherlink_get("/stations", key, secret).get("stations") or []
    eligible = [s for s in stations if s.get("private") is False and
                s.get("active") is not False and in_sardinia(s)]
    seen = existing_stations()
    fresh, skipped_duplicates, errors = [], 0, 0
    now = int(time.time())
    for st in eligible[:MAX_STATIONS_PER_RUN]:
        if likely_duplicate(st, seen):
            skipped_duplicates += 1
            continue
        try:
            response = weatherlink_get("/current/" + urllib.parse.quote(str(st["station_id"]), safe=""), key, secret)
            row = normalize_current(st, response, now)
            if row:
                fresh.append(row)
        except (KeyError, ValueError, OSError, TimeoutError) as exc:
            errors += 1
            print(f"Stazione WeatherLink {st.get('station_id')}: lettura non disponibile ({type(exc).__name__})")
        time.sleep(0.12)
    if eligible and errors > max(3, len(eligible[:MAX_STATIONS_PER_RUN]) // 2):
        raise RuntimeError("WeatherLink: troppe chiamate fallite; archivio precedente conservato")
    # Empty authorized results can legitimately happen. Publish an empty dataset
    # only after a successful authorized /stations response.
    generated_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    document = {
        "generated_at": generated_at,
        "local_day": datetime.now().astimezone().date().isoformat(),
        "source": "Davis WeatherLink v2 (stazioni pubbliche autorizzate)",
        "station_count": len(fresh),
        "authorized_sardinia_stations": len(eligible),
        "excluded_likely_duplicates": skipped_duplicates,
        "errors": errors,
        "stations": sorted(fresh, key=lambda row: (row["name"].casefold(), row["station_id"]))
    }
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"WeatherLink: autorizzate {len(eligible)}, importate {len(fresh)}, "
          f"duplicati probabili {skipped_duplicates}, errori {errors}.")


if __name__ == "__main__":
    main()
