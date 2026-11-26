"""Acquire verified precipitation observations from Sardegna CEDOC."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import random
import re
from threading import Lock, local
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://www.sardegnacedoc.it/idrografico"
BULK = "/webgis/ajax/pluviometria"
OUT = Path("data/cedoc.json")
LAST_VALID = Path("data/cedoc_last_valid.json")
SITES = Path("data/cedoc_sites.json")
CATALOG = Path("data/cedoc_sensors.json")
TZ = ZoneInfo("Europe/Rome")
UTC = dt.timezone.utc
REFRESH_MINUTES = 30
FRESH = dt.timedelta(minutes=90)
STALE = dt.timedelta(hours=3)
FUTURE_TOLERANCE = dt.timedelta(minutes=6)
SOURCE = "Sardegna CEDOC / ADIS – Regione Autonoma della Sardegna"
VARIABLES = ("rain_48h", "rain_today")
READ_STATES = {"ok", "partial", "unreachable", "unavailable", "invalid"}
SENSOR_RE = re.compile(r"/(?:idrografico/)?sensore/(\d+)/(\d+)/(20207|20406)(?!\d)")
STATION_RE = re.compile(r"/idrografico/stazione/(\d+)/?$")
STAMP_RE = r"(\d{1,2}[-/]\d{1,2}[-/]\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?)"
NUMBER_RE = r"([+-]?\d+(?:[.,]\d+)?)"
ROLLING_RE = re.compile(
    STAMP_RE + r"\s*[:–]?\s*" + NUMBER_RE + r"\s*mm\s*(?:nelle\s+)?ultime\s+48\s*(?:h|ore)\b", re.I
)
DAILY_RE = re.compile(r"^Pioggia\s+cumulata\s+oggi\s*:?\s*" + NUMBER_RE + r"\s*mm$", re.I)


def utcnow():
    return dt.datetime.now(UTC)


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_iso(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (AttributeError, ValueError):
        return None


def parse_time(value, fetched_at):
    day, clock = value.split()
    naive = dt.datetime.strptime(day.replace("/", "-") + " " + clock,
                                 "%d-%m-%Y %H:%M:%S" if clock.count(":") == 2 else "%d-%m-%Y %H:%M")
    possible = {naive.replace(tzinfo=TZ, fold=fold).astimezone(UTC) for fold in (0, 1)}
    possible = [candidate for candidate in possible if candidate <= fetched_at + FUTURE_TOLERANCE
                and candidate.astimezone(TZ).replace(tzinfo=None) == naive]
    if len(possible) != 1:
        raise ValueError("Orario CEDOC ambiguo, inesistente o nel futuro")
    return possible[0]


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(str(value).replace(",", "."))
    except (ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and low <= parsed <= high else None


def coordinates(lat, lon):
    return number(lat, 38.6, 41.5), number(lon, 7.5, 10.4)


def data_file(path):
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("stations"), list):
        raise ValueError(f"Dataset CEDOC non valido: {path}")
    return payload


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n",
                       encoding="utf-8")
    pending.replace(path)


def retry_after(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class Client:
    """Share request spacing and rate-limit pauses across the worker pool."""

    def __init__(self):
        self.workers = max(1, min(8, int(os.getenv("CEDOC_WORKERS", "4"))))
        self.interval = max(0.25, float(os.getenv("CEDOC_REQUEST_INTERVAL", "0.75")))
        self.timeout = max(2, float(os.getenv("CEDOC_TIMEOUT", "12")))
        self.attempts = max(1, min(4, int(os.getenv("CEDOC_ATTEMPTS", "2"))))
        self.sessions = local()
        self.lock = Lock()
        self.next_request = 0.0
        self.deadline = time.monotonic() + max(10, int(os.getenv("CEDOC_BUDGET_SECONDS", "800")))

    def wait_for_slot(self):
        while True:
            with self.lock:
                now = time.monotonic()
                if max(now, self.next_request) >= self.deadline:
                    raise requests.Timeout("Tempo disponibile per la scansione CEDOC esaurito")
                delay = self.next_request - now
                if delay <= 0:
                    self.next_request = now + self.interval
                    return
            time.sleep(delay)

    def get(self, path, *, json_response=False):
        if not hasattr(self.sessions, "session"):
            self.sessions.session = requests.Session()
        for attempt in range(self.attempts):
            self.wait_for_slot()
            delay = 2 ** attempt + random.uniform(0, 0.75)
            try:
                response = self.sessions.session.get(
                    BASE + path, timeout=(min(6, self.timeout), self.timeout),
                    headers={"User-Agent": "SardegnaMeteoLive/1.0 (public rain-data monitor)",
                             "Accept": "application/json,*/*" if json_response else "text/html,*/*",
                             "Cache-Control": "no-cache", "Pragma": "no-cache"},
                )
                if response.status_code == 429 or response.headers.get("Retry-After"):
                    pause = max(delay, retry_after(response.headers.get("Retry-After")))
                    with self.lock:
                        self.next_request = max(self.next_request, time.monotonic() + pause)
                response.raise_for_status()
                if not response.content.strip():
                    raise ValueError("Risposta CEDOC vuota")
                if json_response:
                    return response.json(), utcnow()
                return BeautifulSoup(response.content, "html.parser"), utcnow()
            except requests.HTTPError as exc:
                if exc.response.status_code not in {408, 425, 429, 500, 502, 503, 504} or attempt + 1 == self.attempts:
                    raise
            except (requests.ConnectionError, requests.Timeout, ValueError):
                if attempt + 1 == self.attempts:
                    raise
            time.sleep(delay)


def sensor_path(path):
    match = SENSOR_RE.fullmatch(path) if isinstance(path, str) else None
    return match.groups() if match else None


def station_code(value):
    text = str(value)
    return text if re.fullmatch(r"\d+", text) else None


def bulk_sites(payload):
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise ValueError("GeoJSON CEDOC non valido")
    sites = {}
    for feature in payload["features"]:
        if not isinstance(feature, dict):
            continue
        props, geo = feature.get("properties"), feature.get("geometry")
        if not isinstance(props, dict) or not isinstance(geo, dict) or geo.get("type") != "Point":
            continue
        code = station_code(props.get("StatNum"))
        coords = geo.get("coordinates")
        if not code or not isinstance(coords, list) or len(coords) < 2:
            continue
        lat, lon = coordinates(coords[1], coords[0])
        url = props.get("url")
        url_match = STATION_RE.fullmatch(urlsplit(url).path) if isinstance(url, str) else None
        if lat is None or lon is None or not url_match or url_match.group(1) != code:
            continue
        name = props.get("StatName") or props.get("Municipality") or code
        entry = {"station_id": code, "name": str(name).strip(), "lat": lat, "lon": lon,
                 "source_url": BASE + "/stazione/" + code}
        # The map may contain several values for the same StatNum and no per-feature
        # timestamp. Use only its geographic metadata; never pick a rain value here.
        sites.setdefault(code, entry)
    if not sites:
        raise ValueError("GeoJSON CEDOC senza stazioni verificabili")
    return sites


def table_fields(soup):
    fields = {}
    table = soup.find("table")
    if table:
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            for label, value in zip(cells[::2], cells[1::2]):
                key = re.sub(r"[^a-z0-9]", "", label.get_text(" ", strip=True).lower())
                fields[key] = value.get_text(" ", strip=True)
    return fields


def read_station(soup, code, fetched_at):
    fields = table_fields(soup)
    heading = soup.find("h2")
    if fields.get("codicestazione", fields.get("codicestatione")) != code or not heading:
        raise ValueError("Scheda stazione CEDOC mancante o con codice inatteso")
    lat, lon = coordinates(fields.get("latitudinewgs84"), fields.get("longitudinewgs84"))
    site = {"station_id": code, "name": heading.get_text(" ", strip=True), "lat": lat, "lon": lon,
            "source_url": BASE + "/stazione/" + code}
    links = sorted({"/sensore/" + "/".join(match.groups()) for match in SENSOR_RE.finditer(str(soup).replace("\\/", "/"))
                    if match.group(1) == code})
    text = soup.get_text(" ", strip=True)
    section = re.split(r"Rilevamento\s+(?:delle\s+)?precipitazioni\s*:?", text, flags=re.I, maxsplit=1)
    match = ROLLING_RE.search(section[1][:220]) if len(section) == 2 else None
    if not match:
        return site, links, None
    value = number(match.group(2), 0, 1800)
    if value is None:
        return site, links, None
    try:
        observed = parse_time(match.group(1), fetched_at)
    except ValueError:
        return site, links, None
    return site, links, measure(value, observed, fetched_at, site["source_url"])


def read_daily(soup, code, sensor):
    fields = table_fields(soup)
    if (fields.get("codicestazione", fields.get("codicestatione")) != code
            or fields.get("codicesensore") != sensor
            or not re.fullmatch(r"pluviometro|pioggia\s+cumulata", fields.get("tipo", ""), re.I)):
        raise ValueError("Scheda pluviometro CEDOC mancante o con codice inatteso")
    heading = soup.find(re.compile(r"^h[2-5]$"),
                        string=re.compile(r"Precipitazioni\s+cumulate\s+giornaliere", re.I))
    if heading:
        for sibling in heading.next_siblings:
            tag = getattr(sibling, "name", None)
            if tag in {"h2", "h3", "h4", "h5"}:
                break
            if tag:
                rows = sibling.find_all("tr") or [sibling]
                for row in rows:
                    match = DAILY_RE.fullmatch(row.get_text(" ", strip=True))
                    if match:
                        return number(match.group(1), 0, 1800)
    return None


def measure(value, observed, fetched, source_url, **extra):
    return {"value": value, "unit": "mm", "observed_at": stamp(observed),
            "fetched_at": stamp(fetched) if fetched else None, "source_url": source_url, **extra}


def key_for(station_id, sensor_id):
    return str(station_id), str(sensor_id) if sensor_id is not None else None


def saved_measurements(payloads, known_sensors, checked):
    result = {}
    for payload in payloads:
        for row in payload.get("stations", []):
            code = station_code(row.get("station_id"))
            if not code:
                continue
            path = sensor_path(urlsplit(row.get("source_url") or "").path.removeprefix("/idrografico"))
            sensor = station_code(row.get("sensor_id")) or (path[1] if path and path[0] == code else None)
            if sensor is None and len(known_sensors.get(code, [])) == 1:
                sensor = known_sensors[code][0]
            key = key_for(code, sensor)
            for variable in VARIABLES:
                observed = parse_iso(row.get("last"))
                saved = row.get("variables")
                original_path = BASE + "/stazione/" + code if variable == "rain_48h" else row.get("source_url")
                current = (saved.get(variable) or {}) if isinstance(saved, dict) else {
                    "value": row.get(variable), "observed_at": row.get("last"), "source_url": original_path,
                    "local_day": row.get("observed_day") or (observed.astimezone(TZ).date().isoformat() if observed else None),
                }
                value = number(current.get("value"), 0, 1800)
                observed = parse_iso(current.get("observed_at"))
                source_url = current.get("source_url")
                if (value is None or not observed or observed > checked + FUTURE_TOLERANCE
                        or not isinstance(source_url, str) or not source_url.startswith(BASE + "/")):
                    continue
                extra = {"local_day": observed.astimezone(TZ).date().isoformat()} if variable == "rain_today" else {}
                if variable == "rain_today" and current.get("observed_at_basis"):
                    extra["observed_at_basis"] = current["observed_at_basis"]
                prior = result.setdefault(key, {}).get(variable)
                if not prior or observed >= parse_iso(prior["observed_at"]):
                    result[key][variable] = measure(value, observed, parse_iso(current.get("fetched_at")), source_url, **extra)
    return result


def read_network(client, seeds, previous):
    """Read one station page per code and a sensor page only for a new daily reading."""
    def read(code):
        paths = set(seeds[code])
        try:
            page, fetched = client.get("/stazione/" + code)
            site, discovered, rolling = read_station(page, code, fetched)
            paths.update(discovered)
        except requests.RequestException as exc:
            error = f"HTTP {exc.response.status_code}" if exc.response is not None else type(exc).__name__
            return code, None, paths, {}, {}, "unreachable", error
        except ValueError as exc:
            return code, None, paths, {}, {}, "invalid", str(exc)
        daily, daily_states = {}, {}
        if rolling:
            observed = parse_iso(rolling["observed_at"])
            observed_day = observed.astimezone(TZ).date()
            if len(paths) == 1 and observed_day == fetched.astimezone(TZ).date():
                for path in sorted(paths):
                    _, sensor, _ = sensor_path(path)
                    old = previous.get((code, sensor), {}).get("rain_today")
                    # If the timestamp is unchanged, reuse an already verified pair.
                    if old and old["observed_at"] == rolling["observed_at"] and old.get("fetched_at"):
                        daily[sensor] = old
                        continue
                    try:
                        sensor_page, daily_fetched = client.get(path)
                        amount = read_daily(sensor_page, code, sensor)
                        if amount is not None and daily_fetched.astimezone(TZ).date() == observed_day:
                            daily[sensor] = measure(amount, observed, daily_fetched, BASE + path,
                                                    local_day=observed_day.isoformat(),
                                                    observed_at_basis="station_precipitation_report")
                        else:
                            daily_states[sensor] = "unavailable"
                    except requests.RequestException:
                        daily_states[sensor] = "unreachable"
                    except ValueError:
                        daily_states[sensor] = "invalid"
        # Station detail has one station-level rolling total without a sensor ID.
        # Attach it to a sensor only when the station has a unique pluviometer.
        return code, site, paths, daily, daily_states, "ok" if rolling else "unavailable", rolling

    with ThreadPoolExecutor(max_workers=client.workers) as pool:
        return list(pool.map(read, sorted(seeds)))


def freshness(observed, now, retained, variable):
    age = now - observed
    if age > STALE or variable == "rain_today" and observed.astimezone(TZ).date() != now.astimezone(TZ).date():
        return "historic"
    return "stale" if retained or age > FRESH else "fresh"


def variables_with_status(values, now, failed_status="unavailable"):
    return {
        key: (
            {**values[key], "retained": bool(values[key].get("retained")),
             "status": freshness(parse_iso(values[key]["observed_at"]), now, values[key].get("retained"), key)}
            if key in values else {"value": None, "unit": "mm", "observed_at": None, "fetched_at": None,
                                   "source_url": None,
                                   "status": "invalid" if failed_status == "invalid" else "unavailable"}
        ) for key in VARIABLES
    }


def make_row(code, sensor, site, readings, reading_status, now, source_url, detail=None):
    variables = variables_with_status(readings, now, reading_status)
    stamps = [entry["observed_at"] for entry in variables.values() if entry["observed_at"]]
    fetched = [entry["fetched_at"] for entry in variables.values() if entry["fetched_at"]]
    daily = variables["rain_today"]
    return {**site, "station_id": code, "sensor_id": sensor, "source_url": source_url,
            "reading_status": reading_status, "reading_error": detail, "variables": variables,
            "observed_at": max(stamps, default=None), "fetched_at": max(fetched, default=None),
            # Compatibility with the static classic viewer and other feed consumers.
            "last": max(stamps, default=None), "rain_48h": variables["rain_48h"]["value"],
            "rain_48h_source": "cedoc_direct" if "rain_48h" in readings else None,
            "rain_today": daily["value"] if daily.get("local_day") == now.astimezone(TZ).date().isoformat() else None,
            "observed_day": daily.get("local_day")}


def validate(payload):
    rows = payload["stations"]
    observed, seen = [], set()
    for row in rows:
        code, sensor = key_for(row["station_id"], row["sensor_id"])
        if not station_code(code) or sensor is not None and not station_code(sensor) or (code, sensor) in seen:
            raise ValueError("Identificativo CEDOC non valido o duplicato")
        seen.add((code, sensor))
        lat, lon = coordinates(row.get("lat"), row.get("lon"))
        if ((lat is None or lon is None) and (row.get("lat") is not None or row.get("lon") is not None)
                or row["reading_status"] not in READ_STATES):
            raise ValueError(f"Coordinate o stato CEDOC non validi: {code}")
        for variable in VARIABLES:
            entry = row["variables"][variable]
            if entry["unit"] != "mm":
                raise ValueError(f"Unità CEDOC inattesa: {code}")
            if entry["value"] is None:
                if entry["observed_at"] is not None or entry["status"] not in {"invalid", "unavailable"}:
                    raise ValueError(f"Misura CEDOC mancante non coerente: {code}")
                continue
            when = parse_iso(entry["observed_at"])
            fetched = parse_iso(entry["fetched_at"])
            if (number(entry["value"], 0, 1800) is None or not when
                    or when > parse_iso(payload["generated_at"]) + FUTURE_TOLERANCE
                    or entry["fetched_at"] is not None and (not fetched or when > fetched + FUTURE_TOLERANCE)
                    or not entry["source_url"].startswith(BASE + "/")
                    or entry["status"] not in {"fresh", "stale", "historic"}
                    or variable == "rain_today" and entry["local_day"] != when.astimezone(TZ).date().isoformat()):
                raise ValueError(f"Misura CEDOC non valida: {code}, {variable}")
            observed.append(entry["observed_at"])
    if any(row["last"] for row in rows) != bool(observed) or payload["station_count"] != len(rows):
        raise ValueError("Riepilogo CEDOC incoerente")
    if payload["source_time"] != max(observed, default=None) or payload["oldest_observation"] != min(observed, default=None):
        raise ValueError("Intervallo osservazioni CEDOC incoerente")
    return bool(observed)


def grouped_coverage(rows, *, sensor_only=False):
    groups = {}
    for row in rows:
        key = key_for(row["station_id"], row["sensor_id"]) if sensor_only else row["station_id"]
        if not sensor_only or row["sensor_id"] is not None:
            groups.setdefault(key, []).append(row)
    with_value = sum(any(row["last"] for row in group) for group in groups.values())
    fresh = sum(any(measurement["status"] == "fresh" for row in group for measurement in row["variables"].values())
                for group in groups.values())
    unreachable = sum(all(row["reading_status"] == "unreachable" or row["reading_error"] == "unreachable"
                          for row in group) for group in groups.values())
    return {"censused": len(groups), "with_value": with_value, "without_value": len(groups) - with_value,
            "fresh": fresh, "older": with_value - fresh, "unreachable": unreachable}


def payload_for(rows, now, **extra):
    observed = [entry["observed_at"] for row in rows for entry in row["variables"].values() if entry["observed_at"]]
    return {"schema_version": 2, "generated_at": stamp(now), "source": SOURCE,
            "source_url": BASE + "/temporeale", "refresh_minutes": REFRESH_MINUTES,
            "station_sweep_minutes": REFRESH_MINUTES, "local_day": now.astimezone(TZ).date().isoformat(),
            "stations": rows, "station_count": len(rows), "source_time": max(observed, default=None),
            "oldest_observation": min(observed, default=None), **extra}


def acquire(client):
    started = utcnow()
    catalog = data_file(CATALOG)
    live = data_file(OUT)
    snapshot = data_file(LAST_VALID)
    old_geo = data_file(SITES)
    sites = {str(row["station_id"]): row for row in old_geo.get("stations", []) if station_code(row.get("station_id"))}
    seeds = {code: set() for code in sites}
    for row in catalog.get("stations", []):
        path = row.get("sensor_path")
        match = sensor_path(path)
        if match and match[0] == str(row.get("station_id")):
            seeds.setdefault(match[0], set()).add(path)
    known_sensors = {code: sorted({sensor_path(path)[1] for path in paths}) for code, paths in seeds.items()}
    previous = saved_measurements([snapshot, live], known_sensors, started)
    for code, _ in previous:
        seeds.setdefault(code, set())
    bulk_error = None
    try:
        bulk, _ = client.get(BULK, json_response=True)
        geography = bulk_sites(bulk)
        for code, site in geography.items():
            seeds.setdefault(code, set())
            sites.setdefault(code, site)
    except (requests.RequestException, ValueError) as exc:
        bulk_error = f"GeoJSON CEDOC non disponibile ({type(exc).__name__})."
    if not seeds:
        raise RuntimeError("Nessuna anagrafica CEDOC utilizzabile")
    results = read_network(client, seeds, previous)
    now = utcnow()
    rows, snapshot_rows, catalog_rows = [], [], []
    updated_count = 0
    prior_sensors = {}
    for code, sensor in previous:
        prior_sensors.setdefault(code, set()).add(sensor)
    for code, site, paths, daily, daily_states, outcome, result in results:
        if site and site["name"] and site["lat"] is not None and site["lon"] is not None:
            sites[code] = site
        geography = sites.get(code, {"name": site["name"] if site else code, "lat": None, "lon": None})
        sensor_urls = {sensor_path(path)[1]: BASE + path for path in sorted(paths)}
        catalog_rows.extend({"station_id": code, "sensor_id": sensor, "name": geography["name"],
                             "sensor_path": url.removeprefix(BASE)} for sensor, url in sensor_urls.items())
        sensors = set(sensor_urls) | (prior_sensors.get(code, set()) - {None})
        station_url = BASE + "/stazione/" + code
        # Keep a separate station observation if a published 48 h total cannot be
        # attributed unambiguously to one of several sensors.
        if not sensors or len(sensors) > 1 and (outcome == "ok" or None in prior_sensors.get(code, ())):
            sensors.add(None)
        for sensor in sorted(sensors, key=lambda value: value or ""):
            prior = previous.get((code, sensor), {})
            if len(sensors) == 1 and sensor is not None:
                prior = {**previous.get((code, None), {}), **prior}
            selected = {key: {**value, "retained": True} for key, value in prior.items()}
            incoming = {}
            if outcome == "ok" and (len(sensors) == 1 or sensor is None):
                incoming["rain_48h"] = result
            if sensor in daily:
                incoming["rain_today"] = daily[sensor]
            advanced = False
            for key, value in incoming.items():
                existing = selected.get(key)
                when = parse_iso(value["observed_at"])
                if not existing or when >= parse_iso(existing["observed_at"]):
                    advanced |= not existing or when > parse_iso(existing["observed_at"]) or value["value"] != existing["value"]
                    selected[key] = value
            updated_count += advanced
            reading = outcome
            if outcome == "ok":
                reading = "ok" if sensor is None or sensor not in daily_states and (daily.get(sensor) or
                            parse_iso(result["observed_at"]).astimezone(TZ).date() != now.astimezone(TZ).date()) else "partial"
                if not incoming or all(selected[key].get("retained") for key in incoming):
                    reading = "partial"
            reading_error = daily_states.get(sensor) if outcome == "ok" else result
            source_url = sensor_urls.get(sensor, station_url)
            rows.append(make_row(code, sensor, geography, selected, reading, now, source_url, reading_error))
            if selected:
                snapshot_rows.append(make_row(code, sensor, geography,
                                              {key: {**value, "retained": True} for key, value in selected.items()},
                                              "ok", now, source_url))

    succeeded = sum(row["reading_status"] in {"ok", "partial"} and
                    any(value["value"] is not None and not value["retained"] for value in row["variables"].values())
                    for row in rows)
    partial = sum(row["reading_status"] == "partial" for row in rows)
    unreachable = sum(row["reading_status"] == "unreachable" or row["reading_error"] == "unreachable" for row in rows)
    with_value = sum(bool(row["last"]) for row in rows)
    fresh = sum(any(value["status"] == "fresh" for value in row["variables"].values()) for row in rows)
    state = "ok" if succeeded == len(rows) and fresh and not partial and not bulk_error else "degraded" if with_value else "unavailable"
    coverage = {"censused": len(rows), "succeeded": succeeded, "failed": len(rows) - succeeded,
                "partial": partial, "updated": updated_count, "unreachable": unreachable, "with_value": with_value,
                "without_value": len(rows) - with_value, "fresh": fresh, "older": with_value - fresh}
    station_coverage = grouped_coverage(rows)
    sensor_coverage = grouped_coverage(rows, sensor_only=True)
    warnings = [bulk_error] if bulk_error else []
    if state != "ok":
        warnings.append(f"CEDOC: {station_coverage['with_value']} stazioni con misure su {station_coverage['censused']}, "
                        f"di cui {station_coverage['fresh']} con misure recenti e {station_coverage['older']} "
                        "con l’ultima misura conservata o in ritardo.")
    report = payload_for(rows, now, checked_at=stamp(now), scan_started_at=stamp(started), status=state,
                         coverage=coverage, station_coverage=station_coverage, sensor_coverage=sensor_coverage,
                         warning=" ".join(warnings) or None)
    candidate_snapshot = payload_for(snapshot_rows, now, status="snapshot")

    has_values = validate(report)
    if validate(candidate_snapshot):
        retained_by_key = {(row["station_id"], row["sensor_id"]): row["variables"] for row in snapshot_rows}
        for (code, sensor), measurements in previous.items():
            candidates = retained_by_key.get((code, sensor))
            if candidates is None and sensor is None:
                candidates = next((values for (station, _), values in retained_by_key.items() if station == code), None)
            if not candidates or any(
                not candidates[key]["observed_at"] or
                parse_iso(candidates[key]["observed_at"]) < parse_iso(measurement["observed_at"])
                for key, measurement in measurements.items()
            ):
                raise RuntimeError(f"La nuova acquisizione perderebbe misure CEDOC già verificate: {code}")
        # A total outage refreshes feed health while preserving the last valid snapshot.
        existing_keys = {(r.get("station_id"), r.get("sensor_id")) for r in snapshot.get("stations", [])}
        if (updated_count or snapshot.get("schema_version") != 2
                or any((r["station_id"], r["sensor_id"]) not in existing_keys for r in snapshot_rows)):
            write_json(LAST_VALID, candidate_snapshot)
    elif not has_values and any(row.get("last") for row in live.get("stations", []) + snapshot.get("stations", [])):
        raise RuntimeError("Acquisizione vuota: mantenute le misure CEDOC già pubblicate")

    geographic_rows = [sites[code] for code in sorted(sites)
                       if all(value is not None for value in coordinates(sites[code].get("lat"), sites[code].get("lon")))]
    if geographic_rows != old_geo.get("stations", []):
        write_json(SITES, {"generated_at": stamp(now), "source": SOURCE,
                           "station_count": len(geographic_rows), "stations": geographic_rows})
    if catalog_rows != catalog.get("stations", []):
        write_json(CATALOG, {"generated_at": stamp(now), "source_url": BASE + "/stazioni", "stations": catalog_rows})
    write_json(OUT, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", help="Valida i dataset locali senza accedere alla fonte")
    args = parser.parse_args()
    if args.validate:
        report = data_file(OUT)
        has_values = validate(report)
        if LAST_VALID.exists() and not validate(data_file(LAST_VALID)):
            raise ValueError("Snapshot CEDOC senza misure")
        coverage = report["coverage"]
        if (coverage["censused"] != report["station_count"]
                or coverage["succeeded"] + coverage["failed"] != coverage["censused"]
                or bool(coverage["with_value"]) != has_values
                or coverage["with_value"] + coverage["without_value"] != coverage["censused"]
                or coverage["fresh"] + coverage["older"] != coverage["with_value"]
                or report["status"] not in {"ok", "degraded", "unavailable"}):
            raise ValueError("Copertura CEDOC incoerente")
        for key, sensor_only in (("station_coverage", False), ("sensor_coverage", True)):
            if report[key] != grouped_coverage(report["stations"], sensor_only=sensor_only):
                raise ValueError(f"Copertura CEDOC incoerente: {key}")
        return
    report = acquire(Client())
    print(json.dumps({key: value for key, value in report.items() if key != "stations"}, ensure_ascii=False))
    if report["status"] != "ok" and os.getenv("STRICT_CEDOC") == "1":
        raise RuntimeError(report["warning"])


if __name__ == "__main__":
    main()
