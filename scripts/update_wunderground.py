#!/usr/bin/env python3
"""Aggiorna le PWS Weather Underground della Sardegna.

Usa l'inventario prodotto da discover_wunderground.py e l'endpoint ufficiale
PWS Recent History 1 Day Rapid, che fornisce osservazioni circa ogni 5 minuti.
Da una sola chiamata per stazione ricaviamo:
- valori più recenti (media dell'ultimo intervallo rapido);
- Tmin/Tmax del giorno locale;
- raffica massima del giorno;
- pioggia istantanea e cumulata giornaliera.

Output: data/wunderground.json
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path("data/wunderground_stations.json")
TEST = Path("data/wunderground_current_test.json")
OUT = Path("data/wunderground.json")
BASE = "https://api.weather.com/v2/pws/observations/all/1day"
ROME = ZoneInfo("Europe/Rome")
UTC = timezone.utc

def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "sardegna-meteolive/1.0"})
    with urllib.request.urlopen(req, timeout=35) as r:
        raw = r.read()
        if not raw:
            raise RuntimeError("risposta vuota")
        return json.loads(raw.decode("utf-8"))

def parse_utc(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None

def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def round1(v):
    return round(float(v), 1) if num(v) else None

def station_ids(inventory):
    rows = inventory.get("stations") or []
    # Dopo il test iniziale, preferiamo gli ID che hanno risposto almeno una volta.
    if TEST.exists():
        try:
            test = json.loads(TEST.read_text(encoding="utf-8"))
            ok = {x.get("station_id") for x in (test.get("stations") or []) if x.get("station_id")}
            filtered = [x for x in rows if x.get("station_id") in ok]
            if filtered:
                return filtered
        except Exception:
            pass
    return rows

def main():
    key = os.environ.get("WU_API_KEY")
    if not key:
        raise RuntimeError("Manca WU_API_KEY nei GitHub Actions Secrets")
    if not SRC.exists():
        raise RuntimeError("Manca data/wunderground_stations.json")

    inventory = json.loads(SRC.read_text(encoding="utf-8"))
    stations = station_ids(inventory)
    now = datetime.now(UTC)
    today_local = now.astimezone(ROME).date()

    out_rows = []
    errors = []
    quota_stop = False

    for idx, st in enumerate(stations, 1):
        sid = st.get("station_id")
        if not sid:
            continue
        q = urllib.parse.urlencode({
            "stationId": sid,
            "format": "json",
            "units": "m",
            "numericPrecision": "decimal",
            "apiKey": key,
        })
        try:
            payload = fetch_json(BASE + "?" + q)
            obs_all = payload.get("observations") or []
            parsed = []
            for o in obs_all:
                dt = parse_utc(o.get("obsTimeUtc"))
                if not dt:
                    continue
                parsed.append((dt, o))
            if not parsed:
                raise RuntimeError("nessuna osservazione valida")

            parsed.sort(key=lambda x: x[0])
            last_dt, last = parsed[-1]
            lm = last.get("metric") or {}

            today_obs = [(dt, o) for dt, o in parsed if dt.astimezone(ROME).date() == today_local]
            if not today_obs:
                today_obs = [(last_dt, last)]

            tmins, tmaxs, gusts = [], [], []
            for _, o in today_obs:
                m = o.get("metric") or {}
                if num(m.get("tempLow")):
                    tmins.append(float(m["tempLow"]))
                if num(m.get("tempHigh")):
                    tmaxs.append(float(m["tempHigh"]))
                if num(m.get("windgustHigh")):
                    gusts.append(float(m["windgustHigh"]))

            # precipTotal è il cumulato giornaliero: usiamo il record più recente.
            rain_today = lm.get("precipTotal")
            pressure = None
            pmin, pmax = lm.get("pressureMin"), lm.get("pressureMax")
            if num(pmin) and num(pmax):
                pressure = (float(pmin) + float(pmax)) / 2
            elif num(pmax):
                pressure = float(pmax)
            elif num(pmin):
                pressure = float(pmin)

            age_min = max(0.0, (now - last_dt).total_seconds() / 60)
            row = {
                "station_id": sid,
                "name": st.get("name") or sid,
                "network": "wunderground",
                "lat": float(last.get("lat", st.get("lat"))),
                "lon": float(last.get("lon", st.get("lon"))),
                "elev": None,
                "temp": round1(lm.get("tempAvg")),
                "tmin": round(min(tmins), 1) if tmins else None,
                "tmax": round(max(tmaxs), 1) if tmaxs else None,
                "rh": round1(last.get("humidityAvg")),
                "wind_kmh": round1(lm.get("windspeedAvg")),
                "wind_dir": round(float(last["winddirAvg"])) if num(last.get("winddirAvg")) else None,
                "gust_kmh": round(max(gusts), 1) if gusts else round1(lm.get("windgustHigh")),
                "rain_rate": round1(lm.get("precipRate")),
                "rain_today": round1(rain_today),
                "dewpoint": round1(lm.get("dewptAvg")),
                "pressure_hpa": round1(pressure),
                "solar_wm2": round1(last.get("solarRadiationHigh")),
                "uv": round1(last.get("uvHigh")),
                "qc_status": last.get("qcStatus"),
                "last": last_dt.isoformat().replace("+00:00", "Z"),
                "stale_min": round(age_min),
                "available": {
                    "temp": num(lm.get("tempAvg")),
                    "rh": num(last.get("humidityAvg")),
                    "wind": num(lm.get("windspeedAvg")),
                    "wind_dir": num(last.get("winddirAvg")),
                    "gust": bool(gusts) or num(lm.get("windgustHigh")),
                    "rain": num(rain_today) or num(lm.get("precipRate")),
                },
            }
            out_rows.append(row)

        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:250]
            except Exception:
                pass
            errors.append({"station_id": sid, "http": exc.code, "error": body or str(exc)})
            if exc.code in (401, 403, 429):
                quota_stop = True
                print(f"Stop anticipato su HTTP {exc.code} alla stazione {idx}/{len(stations)}")
                break
        except Exception as exc:
            errors.append({"station_id": sid, "error": str(exc)[:250]})

        if idx % 25 == 0:
            print(f"Processate {idx}/{len(stations)} stazioni")
        time.sleep(0.06)

    # Non sostituiamo un dataset buono con uno tronco in caso di quota/autenticazione.
    minimum_ok = max(1, int(len(stations) * 0.70))
    if quota_stop or len(out_rows) < minimum_ok:
        raise RuntimeError(
            f"Aggiornamento WU non pubblicato: {len(out_rows)}/{len(stations)} risposte; "
            f"quota_stop={quota_stop}; errori={len(errors)}"
        )

    out_rows.sort(key=lambda x: (x["name"].casefold(), x["station_id"]))
    result = {
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "local_day": str(today_local),
        "source": "Weather Underground / The Weather Company PWS",
        "station_count": len(out_rows),
        "requested_stations": len(stations),
        "errors": len(errors),
        "variable_counts": {
            "temperature": sum(1 for x in out_rows if x["available"]["temp"]),
            "humidity": sum(1 for x in out_rows if x["available"]["rh"]),
            "wind": sum(1 for x in out_rows if x["available"]["wind"]),
            "gust": sum(1 for x in out_rows if x["available"]["gust"]),
            "rain": sum(1 for x in out_rows if x["available"]["rain"]),
        },
        "stations": out_rows,
        "error_details": errors[:50],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Pubblicate {len(out_rows)} stazioni WU; {len(errors)} errori")

if __name__ == "__main__":
    main()
