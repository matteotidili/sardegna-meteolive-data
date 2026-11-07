#!/usr/bin/env python3
"""Acquisisci SOLO i dispositivi Ecowitt autorizzati dall'account (API v3).

Non interroga la mappa pubblica e non pubblica credenziali, indirizzi MAC,
IMEI o dati indoor. Le stazioni senza un dato outdoor recente non sono mostrate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ecowitt.json"
BASE = "https://api.ecowitt.net/api/v3"
ROME = ZoneInfo("Europe/Rome")
MAX_AGE_SECONDS = 90 * 60
# Esclude Corsica e stazioni continentali; comprende le isole minori sarde.
BOUNDS = (38.65, 41.32, 7.70, 10.60)
MAX_DEVICES = 8


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def get(api_path, keys, **params):
    args = {**keys, **params}
    url = BASE + api_path + "?" + urllib.parse.urlencode(args)
    # Non stampare mai url o HTTPError, perche' contengono le API key.
    with urllib.request.urlopen(urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "SardegnaMeteoLive/1.0"}), timeout=22) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        code = str(payload.get("code", "invalid")) if isinstance(payload, dict) else "invalid"
        raise ValueError("Ecowitt API errore (codice " + code[:20] + ")")
    return payload.get("data")


def device_rows(data):
    """Ecowitt restituisce liste con varianti di envelope tra revisioni API."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("list", "devices", "device_list", "rows", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [d for d in value if isinstance(d, dict)]
    if data.get("mac") or data.get("imei"):
        return [data]
    return []


def coordinates(*sources):
    for data in sources:
        if not isinstance(data, dict):
            continue
        for source in (data, data.get("location"), data.get("station")):
            if not isinstance(source, dict):
                continue
            lat = number(source.get("latitude", source.get("lat")))
            lon = number(source.get("longitude", source.get("lon", source.get("lng"))))
            if lat is not None and lon is not None:
                return lat, lon
    return None, None


def sensor(groups, group, field, now):
    section = groups.get(group) or {}
    entry = section.get(field) if isinstance(section, dict) else None
    if not isinstance(entry, dict):
        return None, None
    value, timestamp = number(entry.get("value")), number(entry.get("time"))
    if timestamp is None or value is None or timestamp > now + 300:
        return None, None
    if now - timestamp > MAX_AGE_SECONDS:
        return None, None
    return value, int(timestamp)


def pick(groups, group, fields, now):
    for field in fields:
        value, stamp = sensor(groups, group, field, now)
        if value is not None:
            return value, stamp
    return None, None


def daily_temperature_extremes(keys, locator, now):
    """Estremi campionati ogni 5 minuti dal giorno civile Europe/Rome.

    Ecowitt /device/history restituisce una mappa timestamp UTC -> stringa
    di temperatura, non il formato realtime. Nessun valore e' inventato
    se l'API non ha osservazioni utilizzabili.
    """
    current = datetime.fromtimestamp(now, timezone.utc)
    day = current.astimezone(ROME).date()
    begin = datetime.combine(day, time.min, tzinfo=ROME).astimezone(timezone.utc)
    data = get(
        "/device/history", keys, **locator,
        start_date=begin.strftime("%Y-%m-%d %H:%M:%S"),
        end_date=current.strftime("%Y-%m-%d %H:%M:%S"),
        call_back="outdoor.temperature", cycle_type="5min",
        temp_unitid=1,
    )
    values = ((data or {}).get("outdoor") or {}).get("temperature") or {}
    if not isinstance(values, dict) or not isinstance(values.get("list"), dict):
        return None
    unit = str(values.get("unit") or "").strip().lower()
    fahrenheit = "f" in unit and "c" not in unit
    readings = []
    for ts, raw in values["list"].items():
        stamp, temp = number(ts), number(raw)
        if stamp is None or temp is None or not begin.timestamp() <= stamp <= now + 300:
            continue
        if datetime.fromtimestamp(stamp, ROME).date() != day:
            continue
        if fahrenheit:
            temp = (temp - 32) * 5 / 9
        if -60 <= temp <= 65:
            readings.append(temp)
    if not readings:
        return None
    return round(min(readings), 1), round(max(readings), 1), len(readings)


def station(device, info, current, now):
    if not isinstance(current, dict):
        return None
    lat, lon = coordinates(info, device)
    if lat is None or not (BOUNDS[0] <= lat <= BOUNDS[1]
                           and BOUNDS[2] <= lon <= BOUNDS[3]):
        return None
    temp, t = pick(current, "outdoor", ("temperature",), now)
    if temp is None or not (-60 <= temp <= 65):
        return None
    humidity, _ = pick(current, "outdoor", ("humidity",), now)
    wind, _ = pick(current, "wind", ("wind_speed",), now)
    gust, _ = pick(current, "wind", ("wind_gust",), now)
    wind_dir, _ = pick(current, "wind", ("wind_direction",), now)
    rain_rate, _ = pick(current, "rainfall", ("rain_rate",), now)
    rain, rain_time = pick(current, "rainfall", ("daily",), now)
    if rain is None:
        rain_rate, _ = pick(current, "rainfall_piezo", ("rain_rate",), now)
        rain, rain_time = pick(current, "rainfall_piezo", ("daily",), now)
    pressure, _ = pick(current, "pressure", ("relative",), now)
    solar, _ = pick(current, "solar_and_uvi", ("solar",), now)
    uvi, _ = pick(current, "solar_and_uvi", ("uvi",), now)
    # Il contatore di pioggia giornaliero non deve provenire da ieri.
    today = datetime.fromtimestamp(now, ROME).date()
    if rain_time is not None and datetime.fromtimestamp(rain_time, ROME).date() != today:
        rain = None
    rain = round(rain, 1) if rain is not None and 0 <= rain <= 1800 else None
    rain_rate = round(rain_rate, 1) if rain_rate is not None and 0 <= rain_rate <= 600 else None
    humidity = round(humidity) if humidity is not None and 0 <= humidity <= 100 else None
    wind = round(wind * 3.6, 1) if wind is not None and 0 <= wind <= 90 else None
    gust = round(gust * 3.6, 1) if gust is not None and 0 <= gust <= 100 else None
    wind_dir = round(wind_dir) if wind_dir is not None and 0 <= wind_dir <= 360 else None
    ident = str(device.get("mac") or device.get("imei") or info.get("mac") or info.get("imei") or "")
    if not ident:
        return None
    # Nome pubblico richiesto per la PWS personale di Villaspeciosa.
    # Non rinominare eventuali altri dispositivi autorizzati.
    name = ("Villaspeciosa" if str(device.get("name") or "").startswith("EasyWeatherPro-780FE8")
            or str(info.get("name") or "").startswith("EasyWeatherPro-780FE8")
            else str(info.get("name") or device.get("name") or "Stazione Ecowitt")[:100].strip())
    elev = number(info.get("elevation", info.get("altitude",
                        device.get("elevation", device.get("altitude")))))
    if elev is not None and not (-20 <= elev <= 2000):
        elev = None
    obs = {
        "station_id": hashlib.sha256(ident.upper().encode()).hexdigest()[:14],
        "name": name, "network": "ecowitt", "lat": round(lat, 5),
        "lon": round(lon, 5), "elev": elev,
        "temp": round(temp, 1), "tmin": None, "tmax": None,
        "rh": humidity, "wind_kmh": wind, "wind_dir": wind_dir,
        "gust_kmh": gust, "rain_today": rain, "rain_rate": rain_rate,
        "pressure_hpa": round(pressure, 1) if pressure is not None and 850 <= pressure <= 1080 else None,
        "solar_wm2": round(solar, 1) if solar is not None and 0 <= solar <= 1600 else None,
        "uv": round(uvi, 1) if uvi is not None and 0 <= uvi <= 20 else None,
        "last": datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z"),
        "stale_min": round((now - t) / 60, 1),
        "available": {"temp": True, "rh": humidity is not None,
                      "wind": wind is not None, "wind_dir": wind_dir is not None,
                      "gust": gust is not None, "rain": rain is not None},
    }
    return obs


def run():
    now = datetime.now(timezone.utc)
    # La matrice principale gira ogni 10 minuti. Le chiamate ridondanti
    # non devono consumare quote Ecowitt tra due intervalli di raccolta.
    if OUT.exists() and str(os.environ.get("FORCE_UPDATE","")).lower() not in ("1","true","yes"):
        try:
            previous=json.loads(OUT.read_text(encoding="utf-8"))
            last=datetime.fromisoformat(str(previous.get("generated_at","")).replace("Z","+00:00"))
            # Dopo un aggiornamento del collettore, non saltare la prima
            # acquisizione quando il JSON non ha ancora minime e massime.
            extrema_ready=all(
                st.get("tmin") is not None and st.get("tmax") is not None
                for st in previous.get("stations", []) if isinstance(st, dict)
            ) and bool(previous.get("stations"))
            if 0 <= (now-last).total_seconds() < 9*60 and extrema_ready:
                print("Ecowitt: controllo eseguito meno di 9 minuti fa, salto API")
                return
        except (OSError,ValueError,TypeError):
            pass
    epoch = now.timestamp()
    # Ultimi estremi dello stesso giorno, utili se l'endpoint storico e'
    # temporaneamente indisponibile. Non usare mai gli estremi di ieri.
    prior_by_id = {}
    try:
        prior = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
        if prior.get("local_day") == now.astimezone(ROME).date().isoformat():
            prior_by_id = {st["station_id"]: st for st in prior.get("stations", [])
                           if isinstance(st, dict) and st.get("station_id")}
    except (OSError, ValueError, TypeError):
        pass
    out = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "local_day": now.astimezone(ROME).date().isoformat(),
        "source": "Ecowitt Cloud API v3 (dispositivi autorizzati)",
        "status": "awaiting_credentials",
        "station_count": 0, "stations": [],
    }
    app = os.environ.get("ECOWITT_APPLICATION_KEY", "").strip()
    api = os.environ.get("ECOWITT_API_KEY", "").strip()
    if app and api:
        keys = {"application_key": app, "api_key": api}
        try:
            # Le credenziali dell'account NON concedono accesso alla mappa
            # pubblica: /device/list include soltanto i device autorizzati.
            devices = device_rows(get("/device/list", keys))
            mac_filter = set(re.sub("[^0-9A-F]", "", x.upper())
                             for x in os.environ.get("ECOWITT_DEVICE_MAC", "").split(",") if x.strip())
            if mac_filter:
                devices = [d for d in devices if re.sub("[^0-9A-F]", "",
                    str(d.get("mac", "")).upper()) in mac_filter]
            seen = set()
            errors = 0
            for device in devices[:MAX_DEVICES]:
                ident = device.get("mac") or device.get("imei")
                if not ident or str(ident) in seen:
                    continue
                seen.add(str(ident))
                locator = {"mac": ident} if device.get("mac") else {"imei": ident}
                try:
                    info = get("/device/info", keys, **locator) or {}
                    current = get("/device/real_time", keys, **locator,
                                  call_back="outdoor,wind,rainfall,rainfall_piezo,pressure,solar_and_uvi",
                                  temp_unitid=1, pressure_unitid=3, wind_speed_unitid=6,
                                  rainfall_unitid=12, solar_irradiance_unitid=16) or {}
                    obs = station(device, info, current, epoch)
                    if obs:
                        # La temperatura istantanea e' indipendente dal
                        # servizio storico: un errore storia non fa sparire
                        # la stazione dalla mappa.
                        previous = prior_by_id.get(obs["station_id"], {})
                        try:
                            extrema = daily_temperature_extremes(keys, locator, epoch)
                        except (OSError, ValueError, TypeError):
                            extrema = None
                        if extrema:
                            low, high, samples = extrema
                            # Il dato realtime puo' essere piu' recente
                            # dell'ultimo campione storico disponibile.
                            obs["tmin"] = round(min(low, obs["temp"]), 1)
                            obs["tmax"] = round(max(high, obs["temp"]), 1)
                            obs["extrema_samples"] = samples
                            obs["extrema_source"] = "ecowitt_history_5min"
                        else:
                            older_min = number(previous.get("tmin"))
                            older_max = number(previous.get("tmax"))
                            if older_min is not None and older_max is not None:
                                obs["tmin"] = round(min(older_min, obs["temp"]), 1)
                                obs["tmax"] = round(max(older_max, obs["temp"]), 1)
                                obs["extrema_source"] = "last_valid_today"
                            else:
                                obs["extrema_source"] = "not_available"
                        obs["extrema_day"] = out["local_day"]
                        out["stations"].append(obs)
                except (OSError, ValueError, TypeError):
                    errors += 1
            out["status"] = "ok" if out["stations"] else "no_recent_stations"
            out["authorized_devices"] = len(devices)
            out["errors"] = errors
            out["station_count"] = len(out["stations"])
        except (OSError, ValueError, TypeError) as exc:
            out["status"] = "upstream_unavailable"
            out["error_type"] = type(exc).__name__
    # Non cancellare l'ultima osservazione valida se l'API non risponde.
    # Timestamp e valori rimangono QUELLI ORIGINALI, mai aggiornati artificialmente.
    # Non riproporre un pluviometro "oggi" proveniente dal giorno precedente.
    if not out["stations"] and OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            previous = [dict(s) for s in old.get("stations", [])
                        if isinstance(s, dict) and s.get("network") == "ecowitt"
                        and s.get("station_id") and s.get("last")
                        and number(s.get("lat")) is not None
                        and number(s.get("lon")) is not None]
            for st in previous:
                st["name"] = "Villaspeciosa" if (st.get("station_id") == "ba209016a64287"
                             or str(st.get("name", "")).startswith("EasyWeatherPro-780FE8")) else st.get("name")
                try:
                    last_dt = datetime.fromisoformat(str(st["last"]).replace("Z","+00:00"))
                    st["stale_min"] = round((now-last_dt).total_seconds()/60,1)
                    if last_dt.astimezone(ROME).date() != now.astimezone(ROME).date():
                        st["rain_today"] = None
                        st["tmin"] = None
                        st["tmax"] = None
                except (ValueError,TypeError):
                    continue
                out["stations"].append(st)
            out["station_count"] = len(out["stations"])
            if out["stations"]:
                out["status"] = "last_observation_only"
        except (ValueError, OSError, TypeError):
            pass
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n",
                   encoding="utf-8")
    print("Ecowitt:", out["status"], "-", out["station_count"], "stazioni recenti",
          "(nessuna credenziale o MAC in output)")


if __name__ == "__main__":
    run()
