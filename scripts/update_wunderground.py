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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from wu_api_health import load_state, in_cooldown, record_failure, record_success, save_state

EXCLUDED_WU_STATIONS = {"ISERRA56"}  # Serramanna: sensore con temperatura errata
SRC = Path("data/wunderground_stations.json")
TEST = Path("data/wunderground_current_test.json")
OUT = Path("data/wunderground.json")
BASE = "https://api.weather.com/v2/pws/observations/all/1day"
# Margine per la durata del job: il cron resta ogni 5 minuti.
# Una chiave singola non può sostenere 333 richieste per ciclo a 5 minuti.
# Acquisizione a blocchi con rotazione e mantenimento dei dati precedenti.
MIN_RUN_INTERVAL_MIN = 15
# 6 richieste per ciclo (18/ora), non 333 richieste simultanee.
# La frequenza effettiva dipende comunque dalla quota del provider.
MIN_CATALOG_SIZE = 300
MAX_STATIONS_PER_RUN = 6
MAX_WORKERS = 1
# Modificabile solo in funzione della quota contrattuale assegnata al
# progetto. Sei query / 20 min = 432 richieste al giorno.
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
    # Proviamo sempre tutte le stazioni dell'inventario corrente.
    # Una PWS che non ha risposto a un vecchio test può tornare online in seguito.
    return [s for s in (inventory.get("stations") or []) if s.get("station_id") not in EXCLUDED_WU_STATIONS]

def should_skip_recent_run():
    if str(os.environ.get("FORCE_UPDATE","")).lower() in {"1","true","yes"}:
        return False
    if not OUT.exists():
        return False
    try:
        prev=json.loads(OUT.read_text(encoding="utf-8"))
        ts=prev.get("generated_at")
        if not ts:
            return False
        dt=parse_utc(ts)
        if not dt:
            return False

        # Se l'inventario PWS è stato rigenerato dopo l'ultimo dataset,
        # il refresh va eseguito subito anche dentro la finestra minima tra due controlli.
        if SRC.exists():
            inv=json.loads(SRC.read_text(encoding="utf-8"))
            inv_ts=parse_utc(inv.get("generated_at"))
            if inv_ts and inv_ts > dt:
                print("Inventario WU più recente del dataset: forzo aggiornamento")
                return False

        age=(datetime.now(UTC)-dt).total_seconds()/60
        if age < MIN_RUN_INTERVAL_MIN:
            print(f"Salto aggiornamento: ultimo aggiornamento {age:.1f} min fa")
            return True
    except Exception:
        pass
    return False

def process_station(st, key, now, today_local):
    sid = st.get("station_id")
    if not sid:
        return None, {"station_id": None, "error": "station_id mancante"}, False

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
            if dt:
                parsed.append((dt, o))
        if not parsed:
            raise RuntimeError("nessuna osservazione valida")

        parsed.sort(key=lambda x: x[0])
        last_dt, last = parsed[-1]
        lm = last.get("metric") or {}

        # Nessun dato del giorno locale: non usare gli estremi di ieri
        # come Tmin/Tmax di oggi. La lettura corrente viene verificata
        # separatamente per freschezza sul frontend.
        today_obs = [(dt, o) for dt, o in parsed if dt.astimezone(ROME).date() == today_local]

        tmins, tmaxs, gusts = [], [], []
        for _, o in today_obs:
            m = o.get("metric") or {}
            if num(m.get("tempLow")):
                tmins.append(float(m["tempLow"]))
            if num(m.get("tempHigh")):
                tmaxs.append(float(m["tempHigh"]))
            if num(m.get("windgustHigh")):
                gusts.append(float(m["windgustHigh"]))

        rain_today = lm.get("precipTotal") if today_obs else None
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
            "gust_kmh": round(max(gusts), 1) if gusts else (round1(lm.get("windgustHigh")) if today_obs else None),
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
        return row, None, False

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:250]
        except Exception:
            pass
        err = {"station_id": sid, "http": exc.code, "error": body or str(exc)}
        return None, err, exc.code in (401, 403, 429)
    except Exception as exc:
        return None, {"station_id": sid, "error": str(exc)[:250]}, False


def main():
    health = load_state()
    if in_cooldown(health):
        print("WU: circuit breaker attivo, nessuna richiesta API fino a "
              f"{health.get('blocked_until')}. Archivio precedente conservato.", flush=True)
        return
    if should_skip_recent_run():
        return
    key = os.environ.get("WU_APP_API_KEY")
    if not key:
        print("WU: accesso dedicato WU_APP_API_KEY non configurato; " 
              "chiave personale WU_API_KEY non utilizzata.", flush=True)
        return
    if not SRC.exists():
        raise RuntimeError("Manca data/wunderground_stations.json")

    inventory = json.loads(SRC.read_text(encoding="utf-8"))
    stations = station_ids(inventory)
    now = datetime.now(UTC)
    today_local = now.astimezone(ROME).date()

    # Se un archivio consolidato è danneggiato, fermarsi: non sostituire
    # centinaia di stazioni con un singolo blocco da sei risultati.
    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Archivio WU precedente illeggibile: aggiornamento annullato") from exc
        if not isinstance(previous, dict) or not isinstance(previous.get("stations"), list):
            raise RuntimeError("Archivio WU precedente invalido: aggiornamento annullato")
        if len(previous["stations"]) < MIN_CATALOG_SIZE:
            raise RuntimeError("Archivio WU sotto la soglia di sicurezza (300 PWS): aggiornamento annullato")
    old_rows = {
        str(row["station_id"]): row
        for row in (previous.get("stations") or [])
        if isinstance(row, dict) and row.get("station_id")
    }
    # Nessun reset dell'inventario a ogni run: la rotazione riprende
    # dall'ultimo blocco completato con successo.
    cursor = int(previous.get("next_cursor", 0) or 0)
    batch_limit = max(1, min(333, int(os.environ.get("WU_BATCH_SIZE", MAX_STATIONS_PER_RUN))))
    batch = [stations[(cursor + i) % len(stations)]
             for i in range(min(batch_limit, len(stations)))] if stations else []
    out_rows = []
    errors = []
    quota_stop = False
    completed = 0

    print(f"Aggiornamento WU: blocco {cursor % max(1, len(stations))}, "
          f"{len(batch)}/{len(stations)} stazioni, {MAX_WORKERS} richieste concorrenti", flush=True)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_station, st, key, now, today_local): st
            for st in batch
            if st.get("station_id")
        }
        for future in as_completed(futures):
            completed += 1
            try:
                row, error, fatal = future.result()
            except Exception as exc:
                st = futures[future]
                row, error, fatal = None, {
                    "station_id": st.get("station_id"),
                    "error": str(exc)[:250],
                }, False

            if row:
                out_rows.append(row)
            if error:
                errors.append(error)
            if fatal:
                quota_stop = True
                # Diagnostica del servizio senza esporre la chiave API.
                status = error.get("http", "n/d") if isinstance(error, dict) else "n/d"
                detail = str(error.get("error", "")) if isinstance(error, dict) else ""
                detail = detail.replace(key, "[chiave oscurata]")[:180]
                sid = (futures.get(future) or {}).get("station_id", "n/d")
                print(f"WU bloccato: HTTP {status}; dettaglio={detail!r}; stazione={sid}", flush=True)
                print(f"Stop anticipato per quota/autenticazione dopo {completed}/{len(futures)} risposte")
                for pending in futures:
                    if not pending.done():
                        pending.cancel()
                break

            if completed % 25 == 0:
                print(f"Processate {completed}/{len(futures)} stazioni")

    # Le risposte 401/403/429 non devono provocare retry ogni pochi minuti.
    # Il dataset delle stazioni rimane invariato; viene pubblicato solo il
    # blocco temporale comune a osservazioni e discovery.
    if quota_stop:
        fatal_error = next((e for e in errors if e.get("http") in (401, 403, 429)), {})
        new_state = record_failure(health, fatal_error.get("http"),
                                   fatal_error.get("error", "WU API throttled"))
        save_state(new_state)
        print("WU: protezione attiva fino a "+str(new_state.get("blocked_until"))+
              "; nessuna stazione eliminata, nessuna osservazione simulata.", flush=True)
        return

    minimum_ok = max(1, int(len(batch) * 0.60))
    if len(out_rows) < minimum_ok:
        raise RuntimeError(
            f"Aggiornamento WU non pubblicato: {len(out_rows)}/{len(batch)} risposte; "
            f"quota_stop={quota_stop}; errori={len(errors)}"
        )

    for row in out_rows:
        old_rows[row["station_id"]] = row
    # Le stazioni già archiviate non vengono mai cancellate per un
    # inventario temporaneamente incompleto o un errore della rete.
    merged = list(old_rows.values())
    merged.sort(key=lambda x: (x["name"].casefold(), x["station_id"]))
    result = {
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "local_day": str(today_local),
        "source": "Weather Underground / The Weather Company PWS",
        "station_count": len(merged),
        "requested_stations": len(stations),
        "batch_requested": len(batch),
        "batch_updated": len(out_rows),
        "next_cursor": (cursor + len(batch)) % max(1, len(stations)),
        "errors": len(errors),
        "variable_counts": {
            "temperature": sum(1 for x in merged if x["available"]["temp"]),
            "humidity": sum(1 for x in merged if x["available"]["rh"]),
            "wind": sum(1 for x in merged if x["available"]["wind"]),
            "gust": sum(1 for x in merged if x["available"]["gust"]),
            "rain": sum(1 for x in merged if x["available"]["rain"]),
        },
        "stations": merged,
        "error_details": errors[:50],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    save_state(record_success(health))
    print(f"Archivio WU: {len(merged)} stazioni, {len(out_rows)} aggiornate nel blocco; "
          f"{len(errors)} errori", flush=True)

if __name__ == "__main__":
    main()
