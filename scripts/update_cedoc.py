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
SENSOR_RE = re.compile(r"(?:/idrografico)?/sensore/(\d+)/(\d+)/(\d+)", re.I)
LAT_RE = re.compile(r"Latitudine\s*\(WGS84\)\s*:?\s*(\d{1,2}\.\d+)", re.I)
LON_RE = re.compile(r"Longitudine\s*\(WGS84\)\s*:?\s*(\d{1,2}\.\d+)", re.I)
SITE_RE = re.compile(r"Situazione\s+aggiornata\s+al\s*:?\s*" + TIME_RE, re.I)
STATION_RE = re.compile(r"/idrografico/stazione/(\d+)(?:/?(?:\?|$))")

def parse_time(value):
    return dt.datetime.strptime(value.strip(), "%d-%m-%Y %H:%M").replace(tzinfo=TZ).astimezone(dt.timezone.utc)

def plain(soup):
    for tag in soup.select("script,style,noscript"):
        tag.decompose()
    return soup.get_text(" ", strip=True)

def fetch(session, path):
    response = session.get(BASE + path, timeout=(8, 17), headers={"User-Agent":"SardegnaMeteoLive/1.0 (public rain-data monitor; contact via GitHub matteotidili)"})
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
        raw_html = str(soup).replace("\\\\/", "/")
        candidates = list(SENSOR_RE.finditer(raw_html))
        selected = next((m for m in candidates if m.group(1)==code and m.group(3)=="20207"), None)
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
    session = requests.Session()
    try:
        sensor_path = item["sensor_path"]
        if not re.fullmatch(r"/sensore/\d+/\d+/20207", sensor_path):
            return None
        sensor = fetch(session, sensor_path)
        body = plain(sensor)
        daily_part = re.search(
            r"Precipitazioni\s+cumulate\s+giornaliere\b(.*?)(?=Precipitazioni\s+cumulate\s+aggregate|Precipitazioni\s+cumulate\s+continue|$)",
            body, re.I | re.S,
        )
        match = DAILY_RE.search(daily_part.group(1)) if daily_part else None
        lat, lon = LAT_RE.search(body), LON_RE.search(body)
        if not (match and lat and lon and re.search(r"Tipo\s*:?\s*Pluviometro", body, re.I)):
            return None
        amount = float(match.group(1).replace(",", "."))
        y, x = float(lat.group(1)), float(lon.group(1))
        if not (38.6 <= y <= 41.5 and 7.5 <= x <= 10.4 and 0 <= amount <= 1800):
            return None
        heading = sensor.find("h2")
        name = heading.get_text(" ", strip=True) if heading else fallback
        return {
            "station_id": code, "name": name[:110], "lat":y, "lon":x,
            "rain_today": round(amount, 1), "source_url": BASE + sensor_path
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
        "refresh_minutes": 30,
        "max_observation_age_hours": 3,
        "status": "unavailable",
        "warning": "Dati automatici non validati ARPAS; verificare sempre l'ora delle misure.",
        "stations": [],
        "station_count": 0,
    }
    try:
        soup = fetch(session, "/temporeale")
        m = SITE_RE.search(plain(soup))
        if not m:
            raise ValueError("Timestamp generale CEDOC assente")
        site_time = parse_time(m.group(1))
        state["source_time"] = site_time.isoformat().replace("+00:00", "Z")
        state["local_day"] = site_time.astimezone(TZ).date().isoformat()
        age = NOW - site_time
        if age < -dt.timedelta(minutes=6) or age > MAX_AGE:
            state["status"] = "stale"
            state["warning"] = "Fonte CEDOC non aggiornata da oltre 3 ore: dati giornalieri nascosti."
        elif state["local_day"] != NOW.astimezone(TZ).date().isoformat():
            state["status"] = "stale"
            state["warning"] = "Data CEDOC non coincidente con il giorno locale corrente."
        else:
            catalog = sensor_catalog(session)
            state["catalog_count"] = len(catalog)
            # Interrogazione moderata: max 3 richieste simultanee.
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                stations = list(filter(None, pool.map(one_station, catalog)))
            if len(stations) < 15:
                state["warning"] = "Lettura delle schede giornaliere incompleta: " + str(len(stations)) + " sensori."
            else:
                for station in stations:
                    # È l'ora di aggiornamento della rete, NON l'ultimo evento di pioggia.
                    station["last"] = state["source_time"]
                    station["observed_day"] = state["local_day"]
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
