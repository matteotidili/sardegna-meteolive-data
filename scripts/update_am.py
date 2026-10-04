#!/usr/bin/env python3
"""Aggiorna alcune stazioni della rete Aeronautica Militare da bollettini SYNOP."""
from __future__ import annotations

import csv
import io
import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path("data/am.json")
ROME = ZoneInfo("Europe/Rome")
UTC = timezone.utc
OGIMET = "https://www.ogimet.com/cgi-bin/getsynop"

STATIONS = {
    "16522": {"name": "Capo Caccia", "lat": 40.5608, "lon": 8.1631, "elev": 204},
    "16546": {"name": "Decimomannu", "lat": 39.3461, "lon": 8.9675, "elev": 28},
    "16550": {"name": "Capo Bellavista", "lat": 39.9307, "lon": 9.7132, "elev": 156},
    "16564": {"name": "Capo Carbonara", "lat": 39.1039, "lon": 9.5135, "elev": 118},
}


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


def synop_temp(group):
    if not group or len(group) != 5 or group[0] not in "12" or not group[1:].isdigit():
        return None
    sign = group[1]
    if sign == "9":
        return None
    if sign not in "01":
        return None
    value = int(group[2:]) / 10.0
    return -value if sign == "1" else value


def sea_level_pressure(group):
    if not group or len(group) != 5 or group[0] != "4" or not group[1:].isdigit():
        return None
    value = int(group[1:]) / 10.0
    return value + 1000.0 if value < 500.0 else value


def report_groups(report, wmo):
    parts = report.replace("=", " ").split()
    try:
        idx = parts.index(wmo)
    except ValueError:
        return [], None
    iw = None
    if "AAXX" in parts:
        try:
            a = parts.index("AAXX")
            if a + 1 < len(parts) and len(parts[a + 1]) >= 5 and parts[a + 1][-1].isdigit():
                iw = int(parts[a + 1][-1])
        except ValueError:
            pass
    groups = []
    for token in parts[idx + 1:]:
        if token in {"222", "333", "444", "555"} or token.startswith(("222", "333", "444", "555")):
            break
        groups.append(token)
    return groups, iw


def decode_report(report, wmo):
    groups, iw = report_groups(report, wmo)
    if len(groups) < 2:
        return {}

    # Dopo l'indicativo WMO: iRiXhVV, Nddff.
    wind_group = groups[1] if len(groups[1]) == 5 and groups[1][1:].isdigit() else None
    wind_dir = wind_speed = None
    if wind_group:
        dd = int(wind_group[1:3])
        ff = int(wind_group[3:5])
        if dd == 0 and ff == 0:
            wind_dir = None
            wind_speed = 0.0
        else:
            wind_dir = None if dd == 99 else dd * 10
            # iw 0/1 = m/s; iw 3/4 = knots.
            if iw in (3, 4):
                wind_speed = ff * 1.852
            else:
                wind_speed = ff * 3.6

    temp = dew = rh_direct = pressure = None
    for g in groups[2:]:
        if len(g) != 5 or not g.isdigit():
            continue
        if g[0] == "1" and temp is None:
            temp = synop_temp(g)
        elif g[0] == "2" and dew is None and rh_direct is None:
            if g[1] == "9":
                rh_direct = fnum(g[2:])
            else:
                dew = synop_temp(g)
        elif g[0] == "4" and pressure is None:
            pressure = sea_level_pressure(g)

    rh = rh_direct if rh_direct is not None else rel_humidity(temp, dew)
    return {
        "temp": temp,
        "dewpoint": dew,
        "rh": rh,
        "wind_dir": wind_dir,
        "wind_kmh": wind_speed,
        "pressure_hpa": pressure,
    }


def fetch_synops():
    now = datetime.now(UTC)
    # Dall'inizio del giorno locale, con margine di 2 ore, per Tmin/Tmax.
    local_start = now.astimezone(ROME).replace(hour=0, minute=0, second=0, microsecond=0)
    start = local_start.astimezone(UTC) - timedelta(hours=2)
    params = urllib.parse.urlencode({
        "block": "165",
        "begin": start.strftime("%Y%m%d%H%M"),
        "end": now.strftime("%Y%m%d%H%M"),
        "header": "yes",
        "lang": "eng",
    })
    req = urllib.request.Request(
        OGIMET + "?" + params,
        headers={"User-Agent": "sardegna-meteolive/1.0"},
    )
    last_exc = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                raise RuntimeError("Risposta SYNOP vuota")
            return raw
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Download SYNOP fallito: {last_exc}")


def load_previous():
    if not OUT.exists():
        return {}
    try:
        obj = json.loads(OUT.read_text(encoding="utf-8"))
        return {str(s.get("wmo")): s for s in obj.get("stations", []) if s.get("wmo")}
    except Exception:
        return {}


def main():
    now = datetime.now(UTC)
    local_day = now.astimezone(ROME).date()
    raw = fetch_synops()
    rows = defaultdict(list)

    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 7:
            continue
        wmo = row[0].strip()
        if wmo not in STATIONS:
            continue
        try:
            dt = datetime(
                int(row[1]), int(row[2]), int(row[3]), int(row[4]), int(row[5]), tzinfo=UTC
            )
        except (ValueError, TypeError):
            continue
        report = ",".join(row[6:]).strip()
        decoded = decode_report(report, wmo)
        if decoded.get("temp") is None and decoded.get("wind_kmh") is None:
            continue
        rows[wmo].append((dt, report, decoded))

    previous = load_previous()
    output = []

    for wmo, meta in STATIONS.items():
        series = sorted(rows.get(wmo, []), key=lambda x: x[0])
        if not series:
            old = previous.get(wmo)
            if old:
                rec = dict(old)
                last = datetime.fromisoformat(rec["last"].replace("Z", "+00:00")) if rec.get("last") else None
                rec["stale_min"] = round((now - last.astimezone(UTC)).total_seconds() / 60) if last else None
                output.append(rec)
            continue

        latest_dt, latest_raw, latest = series[-1]
        today = [x for x in series if x[0].astimezone(ROME).date() == local_day]
        temps = [x[2].get("temp") for x in today if x[2].get("temp") is not None]

        rec = {
            "name": meta["name"],
            "network": "aeronautica-militare",
            "station_id": wmo,
            "wmo": wmo,
            "lat": meta["lat"],
            "lon": meta["lon"],
            "elev": meta["elev"],
            "temp": round(latest["temp"], 1) if latest.get("temp") is not None else None,
            "tmin": round(min(temps), 1) if temps else None,
            "tmax": round(max(temps), 1) if temps else None,
            "rh": round(latest["rh"]) if latest.get("rh") is not None else None,
            "dewpoint": round(latest["dewpoint"], 1) if latest.get("dewpoint") is not None else None,
            "pressure_hpa": round(latest["pressure_hpa"], 1) if latest.get("pressure_hpa") is not None else None,
            "wind_dir": round(latest["wind_dir"]) if latest.get("wind_dir") is not None else None,
            "wind_ms": round(latest["wind_kmh"] / 3.6, 1) if latest.get("wind_kmh") is not None else None,
            "wind_kmh": round(latest["wind_kmh"], 1) if latest.get("wind_kmh") is not None else None,
            "gust_ms": None,
            "gust_kmh": None,
            "rain_1m": None,
            "rain_rate": None,
            "rain_today": None,
            "last": latest_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "stale_min": round((now - latest_dt).total_seconds() / 60),
            "raw_synop": latest_raw,
            "available": {
                "temp": latest.get("temp") is not None,
                "rh": latest.get("rh") is not None,
                "wind": latest.get("wind_kmh") is not None,
                "wind_dir": latest.get("wind_dir") is not None,
                "gust": False,
                "rain": False,
            },
        }
        output.append(rec)

    if not output:
        raise RuntimeError("Nessuna stazione AM/SYNOP ricevuta: mantengo il file precedente")

    newest = max(
        (datetime.fromisoformat(s["last"].replace("Z", "+00:00")) for s in output if s.get("last")),
        default=now,
    )
    result = {
        "generated_at": newest.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "checked_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "local_day": str(local_day),
        "source": "Aeronautica Militare / SYNOP via OGIMET",
        "station_count": len(output),
        "stations": sorted(output, key=lambda x: x["name"].casefold()),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print("Stazioni AM:", ", ".join(f"{s['name']} {s['last']}" for s in output))
    print(f"Scritte {len(output)} stazioni AM; osservazione più recente {result['generated_at']}")


if __name__ == "__main__":
    main()
