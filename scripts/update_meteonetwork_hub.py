#!/usr/bin/env python3
"""MeteoNetwork via MeteoHub, flusso indipendente dalla rete DPCN."""
import json, os, sys, urllib.parse, urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from update_meteohub import api, login, unpack, parse_dt, mval, get_id, wait_for_request

UTC = timezone.utc
OUT = Path("data/meteonetwork.json")
DATASET = "mnw"  # MeteoHub catalog: dataset id=mnw, name=meteonetwork
TEMP, RH, DIR, WIND, GUST, RAIN = "B12101", "B13003", "B11001", "B11002", "B11041", "B13011"
VAR = {TEMP, RH, DIR, WIND, GUST, RAIN}
WINDOW_MIN = 150
SARDINIA = (38.75, 41.4, 7.7, 10.3)

def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

def fnum(value, lo, hi):
    try:
        n = float(value)
        return round(n, 2) if lo <= n <= hi else None
    except (TypeError, ValueError, OverflowError):
        return None

def build_request(now=None):
    now = now or datetime.now(UTC)
    return {
        "request_name": "sardegna-meteolive-mnw",
        "reftime": {"from": iso(now - timedelta(minutes=WINDOW_MIN)), "to": iso(now)},
        "dataset_names": [DATASET],
        "output_format": "json",
        "only_reliable": True,
        "filters": {"product": [{"code": k, "active": True} for k in sorted(VAR)]},
    }

def should_poll(path=OUT, now=None):
    now = now or datetime.now(UTC)
    if os.environ.get("FORCE_UPDATE", "").lower() in ("true", "yes", "1"):
        return True
    if not path.exists():
        return True
    try:
        last = parse_dt(json.loads(path.read_text()).get("checked_at", ""))
        return (now - last).total_seconds() >= 25 * 60
    except (KeyError, TypeError, ValueError):
        return True

def quota_available(token):
    # Keep one extraction in reserve for the existing DPCN collector.
    try:
        d = api("GET", "/api/hourly", token)
        if isinstance(d, dict) and d.get("remaining") is not None and int(d["remaining"]) < 2:
            print("Quota MeteoHub insufficiente: salto MNW, DPCN invariata")
            return False
    except Exception as exc:
        print("Verifica quota non disponibile:", type(exc).__name__)
        return False
    return True

def latest(entries):
    return max(entries, key=lambda x: x[0]) if entries else (None, None, None)

def aggregate(raw, now=None):
    now = now or datetime.now(UTC)
    stations_by_key = {}
    max_dt, checked_records = None, 0
    for line in unpack(raw).decode("utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
            # L'estrazione ora puo' contenere DPCN e MNW insieme.
            # Mai etichettare stazioni DPCN come MeteoNetwork.
            if rec.get("network") != DATASET:
                continue
            dt = parse_dt(rec["date"])
            data = rec.get("data") or []
            meta = data[0].get("vars") or {}
            name = mval(meta, "B01019")
            lat = fnum(mval(meta, "B05001"), -90, 90)
            lon = fnum(mval(meta, "B06001"), -180, 180)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            continue
        if not name or lat is None or lon is None:
            continue
        if not (SARDINIA[0] <= lat <= SARDINIA[1] and SARDINIA[2] <= lon <= SARDINIA[3]):
            continue
        if dt > now + timedelta(minutes=10) or dt < now - timedelta(days=2):
            continue
        checked_records += 1
        max_dt = dt if max_dt is None else max(max_dt, dt)
        key = (str(name).strip(), lat, lon)
        if key not in stations_by_key:
            stations_by_key[key] = {
                "name": key[0], "lat": lat, "lon": lon,
                "elev": fnum(mval(meta, "B07030"), -100, 2100),
                "vars": defaultdict(list),
            }
        st = stations_by_key[key]
        for item in data[1:]:
            for code, obj in (item.get("vars") or {}).items():
                if code not in VAR or not isinstance(obj, dict):
                    continue
                try:
                    val = float(obj.get("v"))
                    if abs(val) < 1e7:
                        st["vars"][code].append((dt, val, item.get("timerange")))
                except (TypeError, ValueError, OverflowError):
                    pass
    stations = []
    local_day = now.astimezone(ZoneInfo("Europe/Rome")).date().isoformat()
    for (name, lat, lon), st in stations_by_key.items():
        vs = st["vars"]
        timestamp = max((t for arr in vs.values() for t, _, _ in arr), default=None)
        if timestamp is None:
            continue
        tk = latest(vs[TEMP])[1]
        temp = fnum(tk - 273.15, -45, 55) if tk is not None and 170 <= tk <= 350 else None
        rh = fnum(latest(vs[RH])[1], 0, 100)
        wind_ms = fnum(latest(vs[WIND])[1], 0, 90)
        gust_ms = fnum(latest(vs[GUST])[1], 0, 120)
        wdir = fnum(latest(vs[DIR])[1], 0, 360)
        # BUFR B13011 is interval rainfall, not necessarily today's cumulative rainfall.
        rain_v, rain_tr = latest(vs[RAIN])[1:]
        rain_rate = None
        if rain_v is not None and isinstance(rain_tr, (list, tuple)) and len(rain_tr) >= 3:
            indicator, _, duration = rain_tr[:3]
            if indicator in (1, 2) and duration in (60, 3600):
                rain_rate = fnum(rain_v * (3600 / duration), 0, 500)
        if all(v is None for v in (temp, rh, wind_ms, rain_rate)):
            continue
        station_id = f"mnw-{name.casefold().replace(' ', '-')}-{lat:.4f}-{lon:.4f}"
        stations.append({
            "station_id": station_id, "name": name, "network": "meteonetwork",
            "lat": lat, "lon": lon, "elev": st["elev"],
            "temp": temp, "tmin": None, "tmax": None, "rh": rh,
            "wind_kmh": round(wind_ms * 3.6, 1) if wind_ms is not None else None,
            "wind_dir": wdir,
            "gust_kmh": round(gust_ms * 3.6, 1) if gust_ms is not None else None,
            "rain_rate": rain_rate, "rain_today": None, "pressure_hpa": None,
            "last": iso(timestamp),
            "available": {
                "temp": temp is not None, "rh": rh is not None,
                "wind": wind_ms is not None, "wind_dir": wdir is not None,
                "gust": gust_ms is not None, "rain": rain_rate is not None,
            },
            "source_url": "https://www.meteonetwork.it/rete/livemap/",
        })
    if not stations:
        raise ValueError(f"MNW: nessuna stazione sarda con osservazioni valide ({checked_records} record sardi)")
    stations.sort(key=lambda x: (x["name"].casefold(), x["station_id"]))
    return {
        "generated_at": iso(max_dt), "checked_at": iso(now), "local_day": local_day,
        "source": "MeteoHub / MeteoNetwork",
        "license": "CC BY - MeteoNetwork / Agenzia ItaliaMeteo",
        "station_count": len(stations), "stations": stations,
    }


def preserve_previous(doc, path=OUT):
    """Keep missing stations with their original observation time (max 48h)."""
    if not path.exists():
        return doc
    try:
        prev = json.loads(path.read_text())
        found = {s["station_id"] for s in doc["stations"]}
        kept = 0
        current = parse_dt(doc["checked_at"])
        for station in prev.get("stations", []):
            if not isinstance(station, dict):
                continue
            sid = station.get("station_id")
            last = station.get("last")
            if not sid or sid in found or not last:
                continue
            try:
                age = (current - parse_dt(last)).total_seconds()
            except (TypeError, ValueError):
                continue
            if 0 <= age <= 48 * 3600:
                found.add(sid)
                doc["stations"].append(station)
                kept += 1
        doc["stations"].sort(key=lambda s: (s["name"].casefold(), s["station_id"]))
        doc["station_count"] = len(doc["stations"])
        doc["retained_previous_station_count"] = kept
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return doc

def main():
    if not should_poll():
        print("MNW: acquisizione recente, nessuna richiesta")
        return
    token = login()
    if not quota_available(token):
        return
    rid = None
    try:
        try:
            response = api("POST", "/api/data", token, build_request())
        except urllib.error.HTTPError as exc:
            error_body = exc.read(900).decode("utf-8", "replace")
            print("MeteoHub MNW: errore estrazione", exc.code, error_body[:700])
            if exc.code in (403, 404):
                print("Dataset MNW non accessibile con queste credenziali/richiesta: DPCN invariata")
                return
            raise
        rid = get_id(response)
        if rid is None:
            raise RuntimeError("MeteoHub: request_id mancante")
        filename = wait_for_request(token, rid)
        raw = api("GET", "/api/data/" + urllib.parse.quote(str(filename), safe=""),
                  token, timeout=240, parse_json=False)
        doc = preserve_previous(aggregate(raw if isinstance(raw, bytes) else json.dumps(raw).encode()))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"MeteoHub MNW: {doc['station_count']} stazioni sarde, ultima osservazione {doc['generated_at']}")
    finally:
        if rid is not None:
            try:
                api("DELETE", f"/api/requests/{urllib.parse.quote(str(rid), safe='')}", token)
            except Exception as exc:
                print("Pulizia richiesta MNW non riuscita:", type(exc).__name__, file=sys.stderr)

if __name__ == "__main__":
    main()
