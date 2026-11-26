#!/usr/bin/env python3
"""Conserva 24-26 h di osservazioni di stazione GIA' raccolte dai collector.

Nessuna chiamata API: legge i file di MeteoHub, AM, Ecowitt e MeteoNetwork
gia' pubblicati e accoda per ogni stazione il campione con l'orario REALE di osservazione
(campo `last`). Output: data/station_history_24h.json (formato station_history_v1):

  series["<rete>|<station_id|icao|name>"] = [[epoch_min, temp_decimi|null, rain_today_decimi|null], ...]

Regole: niente valori inventati, niente zeri al posto dei mancanti, nessuna interpolazione;
campioni troppo vecchi (> MAX_SOURCE_AGE) o nel futuro scartati; deduplica per minuto.
"""
import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data"
OUT = ROOT / "station_history_24h.json"
INPUTS = (("dpcn-sardegna", "stations.json"), ("aeronautica-militare", "am.json"), ("ecowitt", "ecowitt.json"), ("meteonetwork", "meteonetwork.json"))
RETENTION = dt.timedelta(hours=26)
MAX_SOURCE_AGE = dt.timedelta(minutes=95)
MAX_POINTS = 220
UTC = dt.timezone.utc


def from_iso(v):
    if not v:
        return None
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


def tenths(v, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return None
    return int(round(v * 10))


def load(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def station_key(net, s):
    ident = str(s.get("station_id") or s.get("icao") or s.get("name") or "").strip()
    return f"{net}|{ident}" if ident and len(ident) <= 200 else None


def collect(root, now):
    out = []
    for net, fname in INPUTS:
        stations = load(root / fname).get("stations")
        if not isinstance(stations, list):
            continue
        for s in stations:
            if not isinstance(s, dict):
                continue
            k, t = station_key(net, s), from_iso(s.get("last"))
            if not k or t is None or t > now + dt.timedelta(minutes=5) or now - t > MAX_SOURCE_AGE:
                continue
            avail = s.get("available")
            if not isinstance(avail, dict):
                avail = {}
            temp = None if avail.get("temp") is False else tenths(s.get("temp"), -40, 55)
            rain = None if avail.get("rain") is False else tenths(s.get("rain_today"), 0, 1000)
            if temp is None and rain is None:
                continue
            out.append((k, t, temp, rain))
    return sorted(out, key=lambda sample: sample[1])


def merge(series, samples, now):
    limit = int((now - RETENTION).timestamp() // 60)
    indexed = {
        key: {p[0]: p for p in points if isinstance(p, list) and len(p) == 3 and type(p[0]) is int and p[0] >= limit}
        for key, points in series.items() if isinstance(points, list)
    }
    for key, time, temp, rain in samples:
        minute = int(time.timestamp() // 60)
        points = indexed.setdefault(key, {})
        _, previous_temp, previous_rain = points.get(minute, [minute, None, None])
        points[minute] = [minute, temp if temp is not None else previous_temp, rain if rain is not None else previous_rain]
    return {key: [points[m] for m in sorted(points)[-MAX_POINTS:]] for key, points in indexed.items() if points}


def main(root=ROOT, out=OUT, now=None):
    now = now or dt.datetime.now(UTC)
    old = load(out)
    series = old.get("series") if isinstance(old.get("series"), dict) else {}
    series = merge(series, collect(root, now), now)
    doc = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "format": "station_history_v1",
        "retention_hours": int(RETENTION.total_seconds() // 3600),
        "fields": ["epoch_minute_utc_observation", "temp_c_tenths", "rain_today_mm_tenths"],
        "description": "Campioni osservati reali (orario di osservazione), nessuna interpolazione; null = non misurato.",
        "series": series,
    }
    if (old.get("series") or {}) == series:
        print("station_history: nessun nuovo campione")
        return False
    fd, tmp = tempfile.mkstemp(dir=str(Path(out).parent), suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out)
    print("station_history: serie", len(series))
    return True


if __name__ == "__main__":
    main()
