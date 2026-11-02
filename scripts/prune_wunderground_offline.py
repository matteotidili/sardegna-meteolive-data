#!/usr/bin/env python3
"""Rimuove dal catalogo WU le PWS offline da almeno 14 giorni.

Guardrail:
- richiede una scansione completa, con larga maggioranza di risposte valide;
- non scambia gli errori quota/autenticazione per stazioni disconnesse;
- esamina sia l'ultima osservazione sia l'ultimo updateTimeUtc di discovery;
- conserva un registro separato e consente la riattivazione dalla discovery.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATALOG = Path("data/wunderground_stations.json")
OBSERVATIONS = Path("data/wunderground.json")
RETIRED = Path("data/wunderground_offline.json")
HEALTH = Path("data/wunderground_app_api_health.json")
UTC = timezone.utc
OFFLINE_DAYS = 14


def stamp(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(value):
    try:
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def file_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n",
                    encoding="utf-8")


def prune(now=None):
    now = now or datetime.now(UTC)
    catalog = file_json(CATALOG)
    observations = file_json(OBSERVATIONS)
    health = file_json(HEALTH)
    if health.get("status") != "active" or health.get("blocked_until"):
        print("WU: nessuna pulizia durante blocco API")
        return 0

    items = catalog.get("stations") or []
    count = len(items)
    checked = parse(observations.get("checked_at"))
    requested = int(observations.get("requested_stations") or 0)
    batch = int(observations.get("batch_requested") or 0)
    updated = int(observations.get("batch_updated") or 0)
    if (count < 100 or not checked or now - checked > timedelta(hours=2)
            or checked - now > timedelta(minutes=5)
            or requested != batch or batch < count
            or updated < max(50, int(requested * .65))):
        print(f"WU: pulizia sospesa: ciclo non completo/affidabile "
              f"(catalogo={count}, interrogate={batch}, risposte={updated})")
        return 0

    previous = file_json(RETIRED) if RETIRED.exists() else {"stations": []}
    retired = {str(x["station_id"]): x for x in previous.get("stations", [])
               if isinstance(x, dict) and x.get("station_id")}
    obs_rows = {str(x["station_id"]): x for x in observations.get("stations", [])
                if isinstance(x, dict) and x.get("station_id")}
    cutoff = now - timedelta(days=OFFLINE_DAYS)
    candidates = []
    for site in items:
        sid = str(site.get("station_id") or "")
        if not sid:
            continue
        reading = obs_rows.get(sid) or {}
        last = parse(reading.get("last"))
        timestamp = site.get("update_time_utc")
        try:
            discovery_time = datetime.fromtimestamp(float(timestamp), UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            discovery_time = None
        # Non eliminare se manca la conferma temporale della discovery.
        if not discovery_time:
            continue
        if discovery_time < cutoff and (last is None or last < cutoff):
            candidates.append((sid, site, reading, discovery_time))

    if not candidates:
        print(f"WU: nessuna stazione offline >= {OFFLINE_DAYS} giorni da eliminare")
        return 0
    if len(candidates) > int(count * .30):
        print(f"WU: pulizia sospesa, troppe stazioni candidate ({len(candidates)}/{count})")
        return 0

    ids = {sid for sid, *_ in candidates}
    for sid, site, reading, seen in candidates:
        retired[sid] = {
            "station_id": sid, "name": site.get("name") or sid,
            "lat": site.get("lat"), "lon": site.get("lon"),
            "last_observation": reading.get("last"),
            "last_discovery_update": stamp(seen),
            "retired_at": stamp(now),
            "reason": f"Assenza di osservazioni e ultimo aggiornamento WU > {OFFLINE_DAYS} giorni",
        }

    active = [x for x in items if str(x.get("station_id")) not in ids]
    kept = [x for x in observations.get("stations", [])
            if str(x.get("station_id")) not in ids]
    catalog["stations"] = active
    catalog["station_count"] = len(active)
    catalog["offline_removed_at"] = stamp(now)
    catalog["offline_removed_count"] = len(ids)
    observations["stations"] = kept
    observations["station_count"] = len(kept)
    observations["offline_removed_count"] = len(ids)
    observations["offline_removed_at"] = stamp(now)
    if isinstance(observations.get("variable_counts"), dict):
        fields = {"temperature": "temp", "humidity": "rh", "wind": "wind",
                  "gust": "gust", "rain": "rain"}
        observations["variable_counts"] = {
            k: sum(1 for x in kept if (x.get("available") or {}).get(field))
            for k, field in fields.items()
        }
    save(CATALOG, catalog)
    save(OBSERVATIONS, observations)
    save(RETIRED, {"generated_at": stamp(now), "offline_days": OFFLINE_DAYS,
                   "station_count": len(retired),
                   "stations": sorted(retired.values(), key=lambda x: x["station_id"])})
    print(f"WU: escluse {len(ids)} PWS offline da >= {OFFLINE_DAYS} giorni; "
          f"rimangono {len(active)} nel catalogo. Registro ripristinabile.")
    return len(ids)


if __name__ == "__main__":
    prune()
