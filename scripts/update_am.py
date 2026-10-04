#!/usr/bin/env python3
"""Aggiorna alcune stazioni della rete Aeronautica Militare da bollettini SYNOP."""
from __future__ import annotations

import csv
import io
import json
import math
import os
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
AWC = "https://aviationweather.gov/api/data/metar"
MIN_RUN_INTERVAL_MIN = 25

STATIONS = {
    "16520": {"name": "Alghero-Fertilia", "icao": "LIEA", "lat": 40.6333, "lon": 8.2833, "elev": 23},
    "16522": {"name": "Capo Caccia", "lat": 40.5608, "lon": 8.1631, "elev": 204},
    "16531": {"name": "Olbia-Costa Smeralda", "icao": "LIEO", "lat": 40.9000, "lon": 9.5167, "elev": 11},
    "16532": {"name": "Monte Limbara", "icao": "LIEW", "lat": 40.8525, "lon": 9.1764, "elev": 1363},
    "16539": {"name": "Capo Frasca", "icao": "LIEF", "lat": 39.7500, "lon": 8.4667, "elev": 89},
    "16542": {"name": "Capo San Lorenzo", "icao": "LIEL", "lat": 39.4981, "lon": 9.6292, "elev": 5},
    "16546": {"name": "Decimomannu", "icao": "LIED", "lat": 39.3461, "lon": 8.9675, "elev": 28},
    "16550": {"name": "Capo Bellavista", "icao": "LIEB", "lat": 39.9333, "lon": 9.7167, "elev": 150},
    "16560": {"name": "Cagliari-Elmas", "icao": "LIEE", "lat": 39.2500, "lon": 9.0667, "elev": 4},
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
        return [], None, []
    iw = None
    if "AAXX" in parts:
        try:
            a = parts.index("AAXX")
            if a + 1 < len(parts) and len(parts[a + 1]) >= 5 and parts[a + 1][-1].isdigit():
                iw = int(parts[a + 1][-1])
        except ValueError:
            pass

    groups = []
    section333 = []
    mode = "main"
    for token in parts[idx + 1:]:
        if token == "333" or token.startswith("333"):
            mode = "333"
            continue
        if token in {"222", "444", "555"} or token.startswith(("222", "444", "555")):
            if mode == "main":
                break
            if mode == "333":
                break
        if mode == "main":
            groups.append(token)
        elif mode == "333":
            section333.append(token)
    return groups, iw, section333


def decode_report(report, wmo):
    groups, iw, section333 = report_groups(report, wmo)
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

    reported_tmax = reported_tmin = None
    for g in section333:
        if len(g) != 5 or not g.isdigit():
            continue
        if g[0] == "1" and reported_tmax is None:
            reported_tmax = synop_temp(g)
        elif g[0] == "2" and reported_tmin is None:
            reported_tmin = synop_temp(g)

    rh = rh_direct if rh_direct is not None else rel_humidity(temp, dew)
    return {
        "temp": temp,
        "dewpoint": dew,
        "rh": rh,
        "wind_dir": wind_dir,
        "wind_kmh": wind_speed,
        "pressure_hpa": pressure,
        "reported_tmax": reported_tmax,
        "reported_tmin": reported_tmin,
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


def fetch_metars():
    ids = ",".join(meta["icao"] for meta in STATIONS.values() if meta.get("icao"))
    params = urllib.parse.urlencode({"ids": ids, "format": "json", "hours": "48"})
    req = urllib.request.Request(
        AWC + "?" + params,
        headers={"User-Agent": "sardegna-meteolive/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            if resp.status == 204:
                return []
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, list) else []
    except Exception as exc:
        print("ATTENZIONE: fallback METAR non disponibile:", exc)
        return []


def decode_metar(row):
    temp = fnum(row.get("temp"))
    dew = fnum(row.get("dewp"))
    wspd = fnum(row.get("wspd"))
    wgst = fnum(row.get("wgst"))
    wdir = fnum(row.get("wdir"))
    altim = fnum(row.get("altim"))
    return {
        "temp": temp,
        "dewpoint": dew,
        "rh": rel_humidity(temp, dew),
        "wind_dir": wdir,
        "wind_kmh": wspd * 1.852 if wspd is not None else None,
        "gust_kmh": wgst * 1.852 if wgst is not None else None,
        "pressure_hpa": altim,
    }


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
    force = str(os.environ.get("FORCE_UPDATE", "")).lower() in {"1", "true", "yes"}
    if OUT.exists() and not force:
        try:
            prev_meta = json.loads(OUT.read_text(encoding="utf-8"))
            checked = prev_meta.get("checked_at")
            if checked:
                dt = datetime.fromisoformat(str(checked).replace("Z", "+00:00")).astimezone(UTC)
                age = (now - dt).total_seconds() / 60
                if age < MIN_RUN_INTERVAL_MIN:
                    print(f"AM già controllata {age:.1f} minuti fa: salto aggiornamento")
                    return
        except Exception:
            pass

    local_day = now.astimezone(ROME).date()
    raw = fetch_synops()
    metars = fetch_metars()
    rows = defaultdict(list)
    metar_rows = defaultdict(list)

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

    icao_to_wmo = {meta.get("icao"): wmo for wmo, meta in STATIONS.items() if meta.get("icao")}
    for row in metars:
        icao = str(row.get("icaoId") or "").upper()
        wmo = icao_to_wmo.get(icao)
        if not wmo:
            continue
        dt = parse_obs_time(row.get("obsTime") or row.get("reportTime"))
        if dt is None:
            continue
        decoded = decode_metar(row)
        if decoded.get("temp") is None and decoded.get("wind_kmh") is None:
            continue
        metar_rows[wmo].append((dt, row.get("rawOb") or "", decoded))

    previous = load_previous()
    output = []

    for wmo, meta in STATIONS.items():
        synop_series = sorted(rows.get(wmo, []), key=lambda x: x[0])
        metar_series = sorted(metar_rows.get(wmo, []), key=lambda x: x[0])
        combined = [(dt, raw, dec, "SYNOP") for dt, raw, dec in synop_series] + [
            (dt, raw, dec, "METAR") for dt, raw, dec in metar_series
        ]
        combined.sort(key=lambda x: x[0])
        if not combined:
            old = previous.get(wmo)
            if old:
                rec = dict(old)
                last = datetime.fromisoformat(rec["last"].replace("Z", "+00:00")) if rec.get("last") else None
                rec["stale_min"] = round((now - last.astimezone(UTC)).total_seconds() / 60) if last else None
                output.append(rec)
            continue

        latest_dt, latest_raw, latest, latest_kind = combined[-1]
        today = [x for x in combined if x[0].astimezone(ROME).date() == local_day]
        temps = [x[2].get("temp") for x in today if x[2].get("temp") is not None]
        synop_today = [x for x in synop_series if x[0].astimezone(ROME).date() == local_day]

        # In Europa i gruppi di estrema della sezione 333 sono normalmente
        # utilizzabili come Tmin alle 06 UTC e come Tmax alle 18 UTC.
        # Gruppi 1xxxx/2xxxx presenti ad altre ore possono avere uso regionale
        # o nazionale diverso e non devono essere interpretati come estremi.
        reported_mins = [
            x[2].get("reported_tmin")
            for x in synop_today
            if x[0].hour == 6
            and x[2].get("reported_tmin") is not None
            and -60 <= x[2].get("reported_tmin") <= 60
        ]
        reported_maxs = [
            x[2].get("reported_tmax")
            for x in synop_today
            if x[0].hour == 18
            and x[2].get("reported_tmax") is not None
            and -60 <= x[2].get("reported_tmax") <= 60
        ]

        min_candidates = list(temps) + reported_mins
        max_candidates = list(temps) + reported_maxs
        tmin = min(min_candidates) if min_candidates else None
        tmax = max(max_candidates) if max_candidates else None

        rec = {
            "name": meta["name"],
            "network": "aeronautica-militare",
            "station_id": meta.get("icao") or wmo,
            "wmo": wmo,
            "icao": meta.get("icao"),
            "lat": meta["lat"],
            "lon": meta["lon"],
            "elev": meta["elev"],
            "temp": round(latest["temp"], 1) if latest.get("temp") is not None else None,
            "tmin": round(tmin, 1) if tmin is not None else None,
            "tmax": round(tmax, 1) if tmax is not None else None,
            "tmin_provisional": not bool(reported_mins),
            "tmax_provisional": not bool(reported_maxs),
            "rh": round(latest["rh"]) if latest.get("rh") is not None else None,
            "dewpoint": round(latest["dewpoint"], 1) if latest.get("dewpoint") is not None else None,
            "pressure_hpa": round(latest["pressure_hpa"], 1) if latest.get("pressure_hpa") is not None else None,
            "wind_dir": round(latest["wind_dir"]) if latest.get("wind_dir") is not None else None,
            "wind_ms": round(latest["wind_kmh"] / 3.6, 1) if latest.get("wind_kmh") is not None else None,
            "wind_kmh": round(latest["wind_kmh"], 1) if latest.get("wind_kmh") is not None else None,
            "gust_ms": round(latest.get("gust_kmh") / 3.6, 1) if latest.get("gust_kmh") is not None else None,
            "gust_kmh": round(latest["gust_kmh"], 1) if latest.get("gust_kmh") is not None else None,
            "rain_1m": None,
            "rain_rate": None,
            "rain_today": None,
            "last": latest_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "stale_min": round((now - latest_dt).total_seconds() / 60),
            "observation_type": latest_kind,
            "raw_synop": latest_raw if latest_kind == "SYNOP" else None,
            "raw_metar": latest_raw if latest_kind == "METAR" else None,
            "available": {
                "temp": latest.get("temp") is not None,
                "rh": latest.get("rh") is not None,
                "wind": latest.get("wind_kmh") is not None,
                "wind_dir": latest.get("wind_dir") is not None,
                "gust": latest.get("gust_kmh") is not None,
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
        "source": "Rete aeronautica / SYNOP via OGIMET + METAR via Aviation Weather Center",
        "station_count": len(output),
        "stations": sorted(output, key=lambda x: x["name"].casefold()),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print("Stazioni AM:", ", ".join(f"{s['name']} {s['last']}" for s in output))
    print(f"Scritte {len(output)} stazioni AM; osservazione più recente {result['generated_at']}")


if __name__ == "__main__":
    main()
