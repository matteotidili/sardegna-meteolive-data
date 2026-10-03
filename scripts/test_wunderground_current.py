#!/usr/bin/env python3
"""Test una tantum delle osservazioni correnti Weather Underground PWS.

Legge data/wunderground_stations.json, interroga l'endpoint ufficiale
/v2/pws/observations/current con units=m e numericPrecision=decimal,
e scrive data/wunderground_current_test.json.

Il test NON e' schedulato.
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

SRC = Path("data/wunderground_stations.json")
OUT = Path("data/wunderground_current_test.json")
BASE = "https://api.weather.com/v2/pws/observations/current"

def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "sardegna-meteolive/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    key = os.environ.get("WU_API_KEY")
    if not key:
        raise RuntimeError("Manca WU_API_KEY nei GitHub Actions Secrets")
    if not SRC.exists():
        raise RuntimeError("Manca data/wunderground_stations.json")

    inventory = json.loads(SRC.read_text(encoding="utf-8"))
    stations = inventory.get("stations") or []

    rows = []
    errors = []
    status_counts = {}
    fresh = 0
    now = datetime.now(timezone.utc)

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
            obs = (payload.get("observations") or [None])[0]
            if not obs:
                raise RuntimeError("risposta senza observations")
            metric = obs.get("metric") or {}
            obs_time = obs.get("obsTimeUtc")
            age_min = None
            if obs_time:
                try:
                    dt = datetime.fromisoformat(obs_time.replace("Z", "+00:00"))
                    age_min = round((now - dt).total_seconds() / 60, 1)
                    if age_min <= 90:
                        fresh += 1
                except Exception:
                    pass
            qc = obs.get("qcStatus")
            status_counts[str(qc)] = status_counts.get(str(qc), 0) + 1
            rows.append({
                "station_id": sid,
                "name": st.get("name") or obs.get("neighborhood") or sid,
                "lat": obs.get("lat", st.get("lat")),
                "lon": obs.get("lon", st.get("lon")),
                "obs_time_utc": obs_time,
                "age_min": age_min,
                "qc_status": qc,
                "temp_c": metric.get("temp"),
                "dewpoint_c": metric.get("dewpt"),
                "humidity": obs.get("humidity"),
                "wind_dir": obs.get("winddir"),
                "wind_kmh": metric.get("windSpeed"),
                "gust_kmh": metric.get("windGust"),
                "pressure_hpa": metric.get("pressure"),
                "rain_rate_mmh": metric.get("precipRate"),
                "rain_total_mm": metric.get("precipTotal"),
                "solar_wm2": obs.get("solarRadiation"),
                "uv": obs.get("uv"),
                "software": obs.get("softwareType"),
            })
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            errors.append({"station_id": sid, "http": exc.code, "error": body or str(exc)})
            # Se la chiave va in quota o autenticazione fallisce, evitiamo centinaia di chiamate inutili.
            if exc.code in (401, 403, 429):
                print(f"Stop anticipato su HTTP {exc.code} alla stazione {idx}/{len(stations)}")
                break
        except Exception as exc:
            errors.append({"station_id": sid, "error": str(exc)[:300]})

        if idx % 25 == 0:
            print(f"Processate {idx}/{len(stations)} stazioni")
        time.sleep(0.06)

    out = {
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "Weather Underground / The Weather Company PWS current observations",
        "requested_stations": len(stations),
        "successful_stations": len(rows),
        "fresh_within_90_min": fresh,
        "errors": len(errors),
        "qc_status_counts": status_counts,
        "stations": rows,
        "error_details": errors[:50],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        f"Test completato: {len(rows)} risposte, {fresh} fresche <=90 min, "
        f"{len(errors)} errori su {len(stations)} stazioni"
    )

if __name__ == "__main__":
    main()
