#!/usr/bin/env python3
"""Monitor pubblico CEDOC: pluviometria 48h georeferenziata.

Usa esclusivamente le pagine pubbliche del sito della Regione Sardegna.
Nessun valore viene presentato come attuale senza timestamp verificato.
Non interpreta l'accumulo 48h come pioggia giornaliera.
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
TZ = ZoneInfo("Europe/Rome")
NOW = dt.datetime.now(dt.timezone.utc)
MAX_AGE = dt.timedelta(hours=4)
TIME_RE = r"(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})"
RAIN_RE = re.compile(
    r"Rilevamento\s+precipitazioni\s*:?\s*"
    + TIME_RE + r"\s+([\d.,]+)\s*mm\s+nelle\s+ultime\s+48\s*h",
    re.I,
)
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

def one_station(item):
    code, fallback = item
    session = requests.Session()
    try:
        soup = fetch(session, "/stazione/" + code)
        heading = soup.find("h2")
        name = heading.get_text(" ", strip=True) if heading else fallback
        body = plain(soup)
        match = RAIN_RE.search(body)
        lat = LAT_RE.search(body)
        lon = LON_RE.search(body)
        if not (match and lat and lon):
            return None
        when = parse_time(match.group(1))
        age = NOW - when
        if age < -dt.timedelta(minutes=6) or age > MAX_AGE:
            return None
        amount = float(match.group(2).replace(",", "."))
        y, x = float(lat.group(1)), float(lon.group(1))
        if not (38.6 <= y <= 41.5 and 7.5 <= x <= 10.4 and 0 <= amount <= 1800):
            return None
        return {
            "station_id": code, "name": name[:110], "lat":y, "lon":x,
            "rain_48h": round(amount, 1), "last": when.isoformat().replace("+00:00", "Z"),
            "source_url": BASE + "/stazione/" + code
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
        "parameter": "rain_48h",
        "unit": "mm",
        "accumulation_hours": 48,
        "max_observation_age_hours": 4,
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
        age = NOW - site_time
        if age < -dt.timedelta(minutes=6) or age > MAX_AGE:
            state["status"] = "stale"
            state["warning"] = "Fonte CEDOC non aggiornata da oltre 4 ore: misure temporaneamente nascoste."
        else:
            catalog = get_catalog(session)
            state["catalog_count"] = len(catalog)
            # Interrogazione moderata: max 3 richieste simultanee.
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                stations = list(filter(None, pool.map(one_station, catalog.items())))
            state["stations"] = sorted(stations, key=lambda s: s["station_id"])
            state["station_count"] = len(stations)
            state["status"] = "ok" if stations else "unavailable"
            if not stations:
                state["warning"] = "Nessun pluviometro con dato 48h recente verificato."
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
