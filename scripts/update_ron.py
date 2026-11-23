#!/usr/bin/env python3
"""Acquisisce le tre boe sarde della Rete Ondametrica Nazionale (ISPRA)."""
from __future__ import annotations

import ast
from marine_forecast import marine_forecast
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

PAGE = "https://www.mareografico.it/it/mappa-interattiva.html"
# Coordinate e indici sono verificati sulle matrici La/Lo/Da del feed cartografico RON.
STATIONS = {"ALGHERO", "CAGLIARI", "SINISCOLA"}
OUT = Path("data/ron.json")


def get(url: str) -> str:
    req = Request(url, headers={"User-Agent": "SardegnaMeteoLive/1.0 (ISPRA public data integration)"})
    with urlopen(req, timeout=25) as response:
        return response.read().decode("utf-8", errors="replace")


def js_array(source: str, name: str):
    match = re.search(rf"\bvar\s+{re.escape(name)}\s*=\s*(\[.*?\]);", source, re.S)
    if not match:
        raise RuntimeError(f"Array {name} non trovato nel feed RON")
    return ast.literal_eval(match.group(1))


def number(value):
    if value is None or str(value).strip().upper() in {"", "N/D", "ND", "-"}:
        return None
    try:
        result = float(value)
        return result if result == result else None
    except (TypeError, ValueError):
        return None


def main() -> None:
    # ISPRA publishes RON snapshots as _jS4_<UTC>.map; discover the newest file.
    now_dt = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now_dt = now_dt.replace(minute=(now_dt.minute // 10) * 10)
    feed_url = None
    source = None
    for step in range(0, 24 * 6 + 1):
        stamp = (now_dt - __import__("datetime").timedelta(minutes=10 * step)).strftime("%Y%m%d%H%M")
        candidate = f"https://www.mareografico.it/FILE/TEMP/_jS4_{stamp}.map"
        try:
            candidate_source = get(candidate)
            if "var La=" in candidate_source and "var Da=" in candidate_source:
                feed_url, source = candidate, candidate_source
                break
        except (HTTPError, URLError, TimeoutError):
            continue
    if not feed_url or not source:
        raise RuntimeError("Nessun feed RON ISPRA trovato nelle ultime 24 ore")
    lats, lons, rows = js_array(source, "La"), js_array(source, "Lo"), js_array(source, "Da")
    names = [str(row[0]).strip().upper() for row in rows]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    file_time = re.search(r"_(\d{12})\.map(?:\?|$)", feed_url)
    snapshot = (datetime.strptime(file_time.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z") if file_time else now)
    out = []
    for key in ("ALGHERO", "SINISCOLA", "CAGLIARI"):
        if key not in names:
            raise RuntimeError(f"Boa RON {key} assente nel feed ISPRA")
        i = names.index(key)
        values = rows[i]
        # Campi Da: nome, T acqua, T aria, UR, pressione, direzione vento, velocità vento,
        # periodo medio, periodo di picco, Hs, direzione media onda, Hmax, radiazione,
        # precipitazione cumulata, corrente prima cella (velocità, direzione).
        vals = [number(v) for v in values[1:16]]
        (water, air, rh, pressure, wind_dir, wind_ms, period_mean, period_peak,
         wave_hs, wave_dir, wave_max, solar, precip, current_ms, current_dir) = vals
        lat, lon = float(lats[i + 1]), float(lons[i + 1])
        out.append({
            "station_id": "ispra-ron-" + key.lower(), "name": key.title(),
            "lat": lat, "lon": lon, "elev": None, "last": snapshot, "snapshot_at": snapshot,
            "temp": air, "rh": rh, "pressure_hpa": pressure,
            "wind_kmh": wind_ms * 3.6 if wind_ms is not None else None,
            "wind_dir": wind_dir, "gust_kmh": None,
            "water_temp_c": water, "solar_wm2": solar, "precip_mm": precip,
            "marine_forecast": marine_forecast(lat, lon),
            "period_mean_s": period_mean, "period_peak_s": period_peak,
            "wave_height_significant_m": wave_hs, "wave_direction_mean_deg": wave_dir,
            "wave_height_max_m": wave_max, "current_speed_ms": current_ms,
            "current_direction_deg": current_dir,
            "available": {"temp": air is not None, "rh": rh is not None,
                          "wind": wind_ms is not None, "wind_dir": wind_dir is not None,
                          "gust": False, "rain": False},
            "source_url": "https://www.mareografico.it/it/mappa-interattiva.html",
        })
    # Rolling 7-day time series from the real RON snapshots.
    try:
        previous = json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    old = {item.get("station_id"): item.get("history", []) for item in previous.get("stations", [])}
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    history_fields = ("water_temp_c", "temp", "pressure_hpa", "wind_kmh", "wind_dir", "period_mean_s", "period_peak_s", "wave_height_significant_m", "wave_direction_mean_deg", "wave_height_max_m", "current_speed_ms", "current_direction_deg", "solar_wm2", "precip_mm")
    for row in out:
        points = [p for p in old.get(row["station_id"], []) if float(p.get("t", 0)) >= cutoff]
        stamp = datetime.fromisoformat(row["last"].replace("Z", "+00:00"))
        point = {"t": int(stamp.timestamp() * 1000)}
        point.update({key: row.get(key) for key in history_fields})
        if not any(p.get("t") == point["t"] for p in points): points.append(point)
        row["history"] = sorted(points, key=lambda p: p["t"])[-1008:]
    payload = {"source": "Rete Ondametrica Nazionale · ISPRA", "license": "CC BY 4.0",
               "generated_at": now, "feed_url": feed_url, "station_count": len(out),
               "stations": out,
               "note": "Dati dinamici non validati da ISPRA; precipitazione cumulata della fonte, periodo non specificato."}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Acquisite {len(out)} boe RON sarde ({now})")


if __name__ == "__main__":
    main()
