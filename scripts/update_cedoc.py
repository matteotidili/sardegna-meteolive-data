#!/usr/bin/env python3
"""Monitor pubblico CEDOC: accumuli pluviometrici giornalieri georeferenziati.

Usa esclusivamente le pagine pubbliche del sito della Regione Sardegna.
Nessun valore viene presentato come attuale senza timestamp verificato.
Il dato viene letto esclusivamente da «Pioggia cumulata oggi» delle schede sensore.
"""
from __future__ import annotations
import concurrent.futures
import datetime as dt
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = "https://www.sardegnacedoc.it"
BASE = ROOT + "/idrografico"
OUT = Path("data/cedoc.json")
CATALOG = Path("data/cedoc_sensors.json")
TZ = ZoneInfo("Europe/Rome")
NOW = dt.datetime.now(dt.timezone.utc)
MAX_AGE = dt.timedelta(hours=3)
TIME_RE = r"(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})"
DAILY_RE = re.compile(r"Pioggia\s+cumulata\s+oggi\s*:?\s*([\d.,]+)\s*mm", re.I)
SENSOR_RE = re.compile(r"(?:/idrografico)?/sensore/(\d+)/(\d+)/(20207|20406)(?!\d)", re.I)
STATION_OBS_RE = re.compile(r"Rilevamento\s+precipitazioni\s*:?\s*" + TIME_RE, re.I)
STATION_48_RE = re.compile(r"Rilevamento\s+precipitazioni\s*:?\s*" + TIME_RE + r"\s+([\d.,]+)\s*mm\s+nelle\s+ultime\s+48\s*h", re.I)
LAT_RE = re.compile(r"Latitudine\s*\(WGS84\)\s*:?\s*(\d{1,2}\.\d+)", re.I)
LON_RE = re.compile(r"Longitudine\s*\(WGS84\)\s*:?\s*(\d{1,2}\.\d+)", re.I)
SITE_RE = re.compile(r"Situazione\s+aggiornata\s+al\s*:?\s*" + TIME_RE, re.I)
STATION_RE = re.compile(r"/idrografico/stazione/(\d+)(?:/?(?:\?|$))")

def parse_time(value):
    return dt.datetime.strptime(value.strip(), "%d-%m-%Y %H:%M").replace(tzinfo=TZ).astimezone(dt.timezone.utc)

def parse_time_iso(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)

def plain(soup):
    for tag in soup.select("script,style,noscript"):
        tag.decompose()
    return soup.get_text(" ", strip=True)

def fetch(session, path):
    # Il portale espone pagine con diversi livelli di cache: per osservazioni
    # temporali richiediamo risposte nuove, non copie servite da CDN/browser.
    params={"_sml_refresh":NOW.strftime("%Y%m%d%H%M")} if path.startswith(("/stazione/", "/sensore/", "/temporeale")) else None
    response = session.get(
        BASE + path, params=params, timeout=(8, 17),
        headers={
            "User-Agent":"SardegnaMeteoLive/1.0 (public rain-data monitor; contact via GitHub matteotidili)",
            "Cache-Control":"no-cache, no-store, max-age=0",
            "Pragma":"no-cache",
        },
    )
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")

def get_catalog(session):
    stations = {}
    for page in range(1, 25):
        route = "/stazioni" if page == 1 else f"/stazioni/page/{page}"
        try:
            soup = fetch(session, route)
        except requests.HTTPError as exc:
            # Le ultime pagine della directory possono restituire 404:
            # dopo una directory consistente si tratta di fine elenco.
            if page > 1 and len(stations) >= 30 and exc.response is not None and exc.response.status_code == 404:
                break
            raise
        found = 0
        for a in soup.find_all("a", href=True):
            m = STATION_RE.search(a["href"])
            if not m:
                continue
            code = m.group(1)
            if code not in stations:
                stations[code] = a.get_text(" ", strip=True)
                found += 1
        if found == 0:
            if page == 1:
                raise ValueError("CEDOC: stazioni non individuate nella tabella pubblica")
            break
        if len(stations) >= 450:
            break
        time.sleep(0.2)
    if len(stations) < 30:
        raise ValueError(f"CEDOC: directory incompleta ({len(stations)} stazioni)")
    return stations

def discover_pluviometer(item):
    code, fallback = item
    session = requests.Session()
    try:
        soup = fetch(session, "/stazione/" + code)
        # I link ai sensori possono trovarsi in href o negli handler HTML.
        raw_html = str(soup).replace("\\/", "/")
        candidates = list(SENSOR_RE.finditer(raw_html))
        selected = next((m for m in candidates if m.group(1)==code and m.group(3) in {"20207","20406"}), None)
        if not selected:
            return None
        return {"station_id":code, "name":fallback, "sensor_path":"/sensore/" + "/".join(selected.groups())}
    except (requests.RequestException, ValueError, TypeError):
        return None
    finally:
        session.close()


def sensor_catalog(session):
    if CATALOG.exists():
        try:
            old = json.loads(CATALOG.read_text(encoding="utf-8"))
            created = dt.datetime.fromisoformat(old["generated_at"].replace("Z", "+00:00"))
            entries = old["stations"]
            if NOW - created < dt.timedelta(days=7) and len(entries) >= 15:
                return entries
        except (ValueError, KeyError, TypeError):
            pass
    index = get_catalog(session)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        result = list(filter(None, pool.map(discover_pluviometer, index.items())))
    if len(result) < 15:
        raise ValueError("Directory sensori CEDOC incompleta: " + str(len(result)) + " identificati.")
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text(json.dumps({
        "generated_at":NOW.isoformat().replace("+00:00", "Z"),
        "source_url":BASE+"/stazioni",
        "stations":sorted(result, key=lambda item:item["station_id"])
    }, ensure_ascii=False, separators=(",", ":"))+"\n", encoding="utf-8")
    return result


def one_station(item):
    code, fallback = item["station_id"], item["name"]
    previous = item.get("previous") or {}
    session = requests.Session()
    try:
        sensor_path = item["sensor_path"]
        if not re.fullmatch(r"/sensore/\d+/\d+/(?:20207|20406)", sensor_path):
            return None
        # L'orario osservato è quello della stazione, non quello del sito
        # riepilogativo: evita di pubblicare totali 'oggi' senza prova di freschezza.
        station_page = fetch(session, "/stazione/" + code)
        station_body = plain(station_page)
        rain_48_match = STATION_48_RE.search(station_body)
        last_match = STATION_OBS_RE.search(station_body)
        if not last_match:
            return None
        when = parse_time(last_match.group(1))
        rain_48h = None
        if rain_48_match:
            historic_at = parse_time(rain_48_match.group(1))
            historic_mm = float(rain_48_match.group(2).replace(",", "."))
            if (abs((when-historic_at).total_seconds())<=300 and
                    0<=historic_mm<=1800):
                rain_48h = round(historic_mm, 1)
        if NOW-when>MAX_AGE or when-NOW>dt.timedelta(minutes=6):
            return None
        if when.astimezone(TZ).date()!=NOW.astimezone(TZ).date():
            return None
        # Quando il pluviometro riporta ancora esattamente la stessa misura
        # recente, riutilizziamo il valore già letto: meno richieste sul CEDOC.
        # Per osservazioni ferme oltre 55 minuti interroghiamo comunque
        # la scheda sensore, anche se l'ora in stazione non si è mossa.
        stamp=when.isoformat().replace("+00:00", "Z")
        if (previous.get("last")==stamp and
            previous.get("observed_day")==when.astimezone(TZ).date().isoformat() and
            isinstance(previous.get("rain_today"),(int,float)) and
            (NOW-when)<=dt.timedelta(minutes=55) and
            isinstance(previous.get("rain_48h"),(int,float))):
            return previous
        sensor = fetch(session, sensor_path)
        body = plain(sensor)
        daily_part = re.search(
            r"Precipitazioni\s+cumulate\s+giornaliere\b(.*?)(?=Precipitazioni\s+cumulate\s+aggregate|Precipitazioni\s+cumulate\s+continue|$)",
            body, re.I | re.S,
        )
        match = DAILY_RE.search(daily_part.group(1)) if daily_part else None
        lat, lon = LAT_RE.search(body), LON_RE.search(body)
        if not (match and lat and lon and re.search(r"Tipo\s*:?\s*(?:Pluviometro|Pioggia\s+Cumulata)", body, re.I)):
            return None
        amount = float(match.group(1).replace(",", "."))
        y, x = float(lat.group(1)), float(lon.group(1))
        if not (38.6 <= y <= 41.5 and 7.5 <= x <= 10.4 and 0 <= amount <= 1800):
            return None
        heading = sensor.find("h2")
        name = heading.get_text(" ", strip=True) if heading else fallback
        return {
            "station_id": code, "name": name[:110], "lat":y, "lon":x,
            "rain_today": round(amount, 1), "rain_48h": rain_48h,
            "rain_48h_source": "cedoc_direct" if rain_48h is not None else None,
            "last": when.isoformat().replace("+00:00", "Z"),
            "observed_day": when.astimezone(TZ).date().isoformat(),
            "source_url": BASE + sensor_path
        }
    except (requests.RequestException, ValueError, TypeError, OverflowError):
        return None
    finally:
        session.close()


def main():
    session = requests.Session()
    state = {
        "generated_at": NOW.isoformat().replace("+00:00", "Z"),
        "source": "Sardegna CEDOC / ADIS – Regione Autonoma della Sardegna",
        "source_url": BASE + "/temporeale",
        "parameter": "rain_today",
        "unit": "mm",
        "accumulation": "giornaliero (Europe/Rome)",
        "refresh_minutes": 15,
        "max_observation_age_hours": 3,
        "status": "unavailable",
        "warning": "Dati automatici non validati ARPAS; verificare sempre l'ora delle misure.",
        "stations": [],
        "station_count": 0,
    }
    try:
        # Il riepilogo può avere una cache diversa dalle schede dei singoli
        # pluviometri: è utile per metadati, non è una prova di freschezza globale.
        try:
            overview = fetch(session, "/temporeale")
            match = SITE_RE.search(plain(overview))
            if match:
                state["overview_time"] = parse_time(match.group(1)).isoformat().replace("+00:00", "Z")
        except (requests.RequestException, ValueError):
            pass
        catalog = sensor_catalog(session)
        state["catalog_count"] = len(catalog)
        try:
            previous_json=json.loads(OUT.read_text(encoding="utf-8"))
            old_stations={str(st.get("station_id")):st for st in previous_json.get("stations",[]) if isinstance(st,dict)}
        except (OSError, ValueError, TypeError):
            old_stations={}
        catalog=[{**item,"previous":old_stations.get(str(item["station_id"]),{})} for item in catalog]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            stations = list(filter(None, pool.map(one_station, catalog)))
        if len(stations) < 15:
            state["status"] = "unavailable"
            state["warning"] = "CEDOC: meno di 15 pluviometri con lettura giornaliera recente e orario verificato (" + str(len(stations)) + ")."
        else:
            state["local_day"] = NOW.astimezone(TZ).date().isoformat()
            state["source_time"] = max(station["last"] for station in stations)
            state["freshness"] = {
                "under_60_minutes":sum(1 for x in stations if NOW-parse_time_iso(x["last"])<=dt.timedelta(minutes=60)),
                "under_90_minutes":sum(1 for x in stations if NOW-parse_time_iso(x["last"])<=dt.timedelta(minutes=90)),
                "over_90_minutes":sum(1 for x in stations if NOW-parse_time_iso(x["last"])>dt.timedelta(minutes=90)),
            }
            state["stations"] = sorted(stations, key=lambda station: station["station_id"])
            state["station_count"] = len(stations)
            state["status"] = "ok"
    except Exception as exc:
        state["warning"] = "CEDOC non raggiungibile o formato modificato: " + str(exc)[:180]
    finally:
        session.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in state.items() if k != "stations"},ensure_ascii=False))
    # Rilevazione dello stato (anche errore) senza pubblicare valori fittizi.
    if state["status"] != "ok" and os.getenv("STRICT_CEDOC") == "1":
        raise RuntimeError(state["warning"])

if __name__ == "__main__":
    main()
