#!/usr/bin/env python3
"""Acquisisce le quattro boe sarde dal feed cartografico ISPRA/Mareografico."""
from __future__ import annotations

import ast
from marine_forecast import marine_forecast
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PAGE = "https://www.mareografico.it/it/mappa-interattiva.html"
STATIONS = {
    "ARBATAX": ("Arbatax", 39.941542, 9.705),
    "CAGLIARI": ("Cagliari", 39.210190, 9.114254),
    "CARLOFORTE": ("Carloforte", 39.1479682922363, 8.30945301055908),
    "PORTO TORRES": ("Porto Torres", 40.8422050476074, 8.4039134979248),
}
OUT = Path("data/mareografico.json")


def get(url: str) -> str:
    req = Request(url, headers={"User-Agent": "SardegnaMeteoLive/1.0 (ISPRA public data integration)"})
    with urlopen(req, timeout=25) as response:
        return response.read().decode("utf-8", errors="replace")


def js_array(source: str, name: str):
    match = re.search(rf"\bvar\s+{re.escape(name)}\s*=\s*(\[.*?\]);", source, re.S)
    if not match:
        raise RuntimeError(f"Array {name} non trovato nel feed ISPRA")
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
    html = get(PAGE)
    scripts = re.findall(r"(?:src|SRC)=[\"']([^\"']+\.map(?:\?[^\"']*)?)[\"']", html)
    if not scripts:
        raise RuntimeError("URL del feed cartografico ISPRA non trovato nella pagina")
    feed_url = urljoin(PAGE, scripts[0])
    source = get(feed_url)
    lats, lons, rows = js_array(source, "La"), js_array(source, "Lo"), js_array(source, "Da")
    names = [str(row[0]).strip().upper() for row in rows]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    file_time = re.search(r"_(\d{12})\.map(?:\?|$)", feed_url)
    snapshot = (
        datetime.strptime(file_time.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        .isoformat(timespec="seconds").replace("+00:00", "Z")
        if file_time else now
    )
    out = []
    for key, (name, lat, lon) in STATIONS.items():
        if key not in names:
            raise RuntimeError(f"Boa {name} assente nel feed ISPRA")
        row_index = names.index(key)
        values = rows[row_index]
        # Le coordinate del feed sono allineate alle righe dati con indice +1.
        lat = float(lats[row_index + 1]) if row_index + 1 < len(lats) else lat
        lon = float(lons[row_index + 1]) if row_index + 1 < len(lons) else lon
        sea_level, water_temp, air_temp, humidity, pressure, wind_dir, wind_ms, solar, precip = [number(v) for v in values[1:10]]
        out.append({
            "station_id": "ispra-" + key.lower().replace(" ", "-"), "name": name,
            "lat": lat, "lon": lon, "elev": None,
            "last": snapshot, "snapshot_at": snapshot,
            "temp": air_temp, "rh": humidity, "pressure_hpa": pressure,
            "wind_kmh": wind_ms * 3.6 if wind_ms is not None else None,
            "wind_dir": wind_dir, "gust_kmh": None,
            "sea_level_m": sea_level, "water_temp_c": water_temp,
            "solar_wm2": solar, "precip_mm": precip,
            "marine_forecast": marine_forecast(lat, lon),
            "available": {"temp": air_temp is not None, "rh": humidity is not None,
                          "wind": wind_ms is not None, "wind_dir": wind_dir is not None,
                          "gust": False, "rain": False},
            "source_url": "https://www.mareografico.it/it/mappa-interattiva.html",
        })
    payload = {
        "source": "Rete Mareografica Nazionale · ISPRA",
        "license": "CC BY 4.0",
        "generated_at": now,
        "feed_url": feed_url,
        "station_count": len(out),
        "stations": out,
        "note": "Dati dinamici non validati da ISPRA; generated_at indica l'acquisizione del feed.",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Acquisite {len(out)} boe sarde dal feed ISPRA ({now})")


if __name__ == "__main__":
    main()
