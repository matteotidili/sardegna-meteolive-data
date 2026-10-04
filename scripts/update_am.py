#!/usr/bin/env python3
"""Aggiorna alcune stazioni della rete Aeronautica Militare da METAR internazionali."""\n# Trigger iniziale workflow AM
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path("data/am.json")
ROME = ZoneInfo("Europe/Rome")
UTC = timezone.utc

STATIONS = {
    "LIED": {"name": "Decimomannu", "wmo": "16546", "lat": 39.3500, "lon": 8.9667, "elev": 29},
    "LIEH": {"name": "Capo Caccia", "wmo": "16522", "lat": 40.5608, "lon": 8.1631, "elev": 204},
    "LIEB": {"name": "Capo Bellavista", "wmo": "16550", "lat": 39.9333, "lon": 9.7167, "elev": 150},
    "LIEC": {"name": "Capo Carbonara", "wmo": "16564", "lat": 39.1000, "lon": 9.5100, "elev": 116},
}

API = "https://aviationweather.gov/api/data/metar"


def parse_obs_time(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def fnum(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rel_humidity(temp_c, dew_c):
    if temp_c is None or dew_c is None:
        return None
    a, b = 17.625, 243.04
    rh = 100.0 * math.exp((a * dew_c) / (b + dew_c) - (a * temp_c) / (b + temp_c))
    return max(0.0, min(100.0, rh))


def fetch_metars():
    params = urllib.parse.urlencode({
        "ids": ",".join(STATIONS),
        "format": "json",
        "hours": "24",
    })
    req = urllib.request.Request(
        API + "?" + params,
        headers={
            "User-Agent": "sardegna-meteolive/1.0 (github.com/matteotidili/sardegna-meteolive)",
            "Accept": "application/json",
        },
    )
    last_exc = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                if resp.status == 204:
                    return []
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                if not isinstance(data, list):
                    raise RuntimeError("Risposta METAR non valida")
                return data
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Download METAR fallito: {last_exc}")


def load_previous():
    if not OUT.exists():
        return {}
    try:
        obj = json.loads(OUT.read_text(encoding="utf-8"))
        return {s.get("station_id"): s for s in obj.get("stations", []) if s.get("station_id")}
    except Exception:
        return {}


def main():
    now = datetime.now(UTC)
    local_day = now.astimezone(ROME).date()
    rows = fetch_metars()
    if not rows:
        raise RuntimeError("Nessun METAR ricevuto: mantengo il file precedente")

    grouped = defaultdict(list)
    for row in rows:
        icao = str(row.get("icaoId") or "").upper()
        if icao not in STATIONS:
            continue
        dt = parse_obs_time(row.get("obsTime") or row.get("reportTime"))
        if dt is None:
            continue
        grouped[icao].append((dt, row))

    previous = load_previous()
    output = []

    for icao, meta in STATIONS.items():
        series = sorted(grouped.get(icao, []), key=lambda x: x[0])
        if not series:
            old = previous.get(icao)
            if old:
                rec = dict(old)
                last = parse_obs_time(rec.get("last"))
                rec["stale_min"] = round((now - last.astimezone(UTC)).total_seconds() / 60) if last else None
                output.append(rec)
            continue

        latest_dt, latest = series[-1]
        today = [(dt, r) for dt, r in series if dt.astimezone(ROME).date() == local_day]

        temp = fnum(latest.get("temp"))
        dew = fnum(latest.get("dewp"))
        temps = [fnum(r.get("temp")) for _, r in today]
        temps = [v for v in temps if v is not None]
        gusts = [fnum(r.get("wgst")) for _, r in today]
        gusts = [v for v in gusts if v is not None]

        wspd = fnum(latest.get("wspd"))
        wgst = fnum(latest.get("wgst"))
        wdir = fnum(latest.get("wdir"))
        altim = fnum(latest.get("altim"))

        rec = {
            "name": meta["name"],
            "network": "aeronautica-militare",
            "station_id": icao,
            "wmo": meta["wmo"],
            "lat": fnum(latest.get("lat")) if fnum(latest.get("lat")) is not None else meta["lat"],
            "lon": fnum(latest.get("lon")) if fnum(latest.get("lon")) is not None else meta["lon"],
            "elev": fnum(latest.get("elev")) if fnum(latest.get("elev")) is not None else meta["elev"],
            "temp": round(temp, 1) if temp is not None else None,
            "tmin": round(min(temps), 1) if temps else None,
            "tmax": round(max(temps), 1) if temps else None,
            "rh": round(rel_humidity(temp, dew)) if rel_humidity(temp, dew) is not None else None,
            "dewpoint": round(dew, 1) if dew is not None else None,
            "pressure_hpa": round(altim, 1) if altim is not None else None,
            "wind_dir": round(wdir) if wdir is not None else None,
            "wind_ms": round(wspd * 0.514444, 1) if wspd is not None else None,
            "wind_kmh": round(wspd * 1.852, 1) if wspd is not None else None,
            "gust_ms": round((max(gusts) if gusts else wgst) * 0.514444, 1) if (gusts or wgst is not None) else None,
            "gust_kmh": round((max(gusts) if gusts else wgst) * 1.852, 1) if (gusts or wgst is not None) else None,
            "rain_1m": None,
            "rain_rate": None,
            "rain_today": None,
            "last": latest_dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "stale_min": round((now - latest_dt.astimezone(UTC)).total_seconds() / 60),
            "raw_metar": latest.get("rawOb"),
            "available": {
                "temp": temp is not None,
                "rh": temp is not None and dew is not None,
                "wind": wspd is not None,
                "wind_dir": wdir is not None,
                "gust": bool(gusts) or wgst is not None,
                "rain": False,
            },
        }
        output.append(rec)

    if not output:
        raise RuntimeError("Nessuna stazione AM utilizzabile: mantengo il file precedente")

    newest = max((parse_obs_time(s.get("last")) for s in output if s.get("last")), default=now)
    result = {
        "generated_at": newest.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "checked_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "local_day": str(local_day),
        "source": "Aeronautica Militare / METAR via Aviation Weather Center",
        "station_count": len(output),
        "stations": sorted(output, key=lambda x: x["name"].casefold()),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Scritte {len(output)} stazioni AM; osservazione più recente {result['generated_at']}")


if __name__ == "__main__":
    main()
