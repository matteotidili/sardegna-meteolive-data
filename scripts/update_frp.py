#!/usr/bin/env python3
"""Aggiorna le rilevazioni Fire Radiative Power (FRP) di MTG/FCI sulla Sardegna.

Il servizio WFS EUMETView può limitare il numero di feature restituite anche
quando COUNT è più alto. Per non perdere pixel FRP durante eventi estesi,
la query viene eseguita frame per frame e, solo quando necessario, l'area
viene suddivisa ricorsivamente in quadranti. Le feature sono poi deduplicate
tramite l'identificativo WFS.

Il token EUMETSAT deve essere fornito esclusivamente tramite la variabile
d'ambiente EUMETSAT_ACCESS_TOKEN (GitHub Actions Secret).
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

WFS_URL = "https://view.eumetsat.int/geoserver/ows"
LAYER = "mtg_fd:frp"
OUT = Path("data/frp.json")

# Rettangolo operativo che contiene la Sardegna e le isole minori prossime.
LAT_MIN, LAT_MAX = 38.5, 41.5
LON_MIN, LON_MAX = 7.5, 10.5

FRAME_MINUTES = 10
LOOKBACK_MINUTES = 90
MAX_SPLIT_DEPTH = 6
REQUEST_TIMEOUT = 45
RETRIES = 4
USER_AGENT = "sardegna-meteolive/1.0 (+github.com/matteotidili/sardegna-meteolive-data)"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def floor_frame(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt.replace(minute=(dt.minute // FRAME_MINUTES) * FRAME_MINUTES)


def request_json(params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    url = WFS_URL + "?" + query
    last_error: Exception | None = None

    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read()
                content_type = (response.headers.get("Content-Type") or "").lower()

            # GeoServer può restituire un ExceptionReport XML con HTTP 200.
            if "json" not in content_type and not raw.lstrip().startswith((b"{", b"[")):
                text = raw.decode("utf-8", errors="replace")
                raise RuntimeError("EUMETSAT WFS non ha restituito JSON: " + text[:800])

            return json.loads(raw)
        except Exception as exc:  # retry anche sui conflitti temporanei del replica DB
            last_error = exc
            if attempt < RETRIES:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Richiesta EUMETSAT fallita dopo {RETRIES} tentativi: {last_error}")


def make_filter(lat0: float, lat1: float, lon0: float, lon1: float, start: datetime, end: datetime) -> str:
    # Usiamo i campi Lat/Lon espliciti: evita l'ambiguità dell'ordine assi EPSG:4326
    # osservata nel BBOX geometrico del servizio.
    return (
        f"Lat BETWEEN {lat0:.6f} AND {lat1:.6f} AND "
        f"Lon BETWEEN {lon0:.6f} AND {lon1:.6f} AND "
        f"time DURING {iso_z(start)}/{iso_z(end)}"
    )


def query_box(
    token: str,
    start: datetime,
    end: datetime,
    lat0: float,
    lat1: float,
    lon0: float,
    lon1: float,
    depth: int = 0,
) -> list[dict]:
    params = {
        "access_token": token,
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": LAYER,
        "outputFormat": "application/json",
        "count": "100",
        "CQL_FILTER": make_filter(lat0, lat1, lon0, lon1, start, end),
    }
    data = request_json(params)
    features = list(data.get("features") or [])

    matched_raw = data.get("numberMatched", data.get("totalFeatures", len(features)))
    try:
        matched = int(matched_raw)
    except (TypeError, ValueError):
        matched = len(features)

    # Il servizio può dichiarare più feature di quante ne restituisca.
    # In quel caso suddividiamo solo il quadrante interessato.
    if matched > len(features):
        if depth >= MAX_SPLIT_DEPTH:
            raise RuntimeError(
                f"FRP troncato anche alla profondità {depth}: matched={matched}, returned={len(features)}, "
                f"box=({lat0},{lat1},{lon0},{lon1})"
            )
        lat_mid = (lat0 + lat1) / 2
        lon_mid = (lon0 + lon1) / 2
        boxes = [
            (lat0, lat_mid, lon0, lon_mid),
            (lat0, lat_mid, lon_mid, lon1),
            (lat_mid, lat1, lon0, lon_mid),
            (lat_mid, lat1, lon_mid, lon1),
        ]
        merged: dict[str, dict] = {}
        for a, b, c, d in boxes:
            for feature in query_box(token, start, end, a, b, c, d, depth + 1):
                fid = str(feature.get("id") or json.dumps(feature.get("geometry"), sort_keys=True))
                merged[fid] = feature
        return list(merged.values())

    return features


def clean_feature(feature: dict) -> dict | None:
    props = dict(feature.get("properties") or {})
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") if geometry.get("type") == "Point" else None

    try:
        lat = float(props.get("Lat"))
        lon = float(props.get("Lon"))
    except (TypeError, ValueError):
        return None

    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return None

    def number(name: str):
        value = props.get(name)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    confidence = props.get("Confidence")
    try:
        confidence = int(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    clean_props = {
        "Lat": lat,
        "Lon": lon,
        "FRP": number("FRP"),
        "FRPerr": number("FRPerr"),
        "Confidence": confidence,
        "BT_mir_k": number("BT_mir_k"),
        "BT_tir_k": number("BT_tir_k"),
        "SZA": number("SZA"),
        "VZA": number("VZA"),
        "Datetime": props.get("Datetime"),
        "time": props.get("time"),
    }

    # Ricostruiamo sempre la geometria da Lon/Lat, che abbiamo verificato
    # esplicitamente nel WFS EUMETSAT.
    return {
        "type": "Feature",
        "id": feature.get("id"),
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": clean_props,
    }


def main() -> None:
    token = (os.environ.get("EUMETSAT_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Variabile EUMETSAT_ACCESS_TOKEN non configurata")

    now = datetime.now(timezone.utc)
    latest_slot = floor_frame(now)
    first_slot = latest_slot - timedelta(minutes=LOOKBACK_MINUTES)

    slots: list[datetime] = []
    cursor = first_slot
    while cursor <= latest_slot:
        slots.append(cursor)
        cursor += timedelta(minutes=FRAME_MINUTES)

    collected: dict[str, dict] = {}
    frame_stats: list[dict] = []

    for slot in slots:
        # Una finestra non sovrapposta per ciascun timestamp nominale MTG.
        end = slot + timedelta(minutes=FRAME_MINUTES) - timedelta(seconds=1)
        features = query_box(token, slot, end, LAT_MIN, LAT_MAX, LON_MIN, LON_MAX)
        accepted = 0
        for raw in features:
            feature = clean_feature(raw)
            if not feature:
                continue
            fid = str(feature.get("id") or json.dumps(feature["geometry"], sort_keys=True))
            collected[fid] = feature
            accepted += 1
        frame_stats.append({"time": iso_z(slot), "count": accepted})

    def feature_time(feature: dict) -> float:
        value = (feature.get("properties") or {}).get("time")
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    features = sorted(collected.values(), key=feature_time)
    valid_times = [
        (f.get("properties") or {}).get("time")
        for f in features
        if (f.get("properties") or {}).get("time")
    ]
    latest_detection = max(valid_times) if valid_times else None

    out = {
        "type": "FeatureCollection",
        "generated_at": iso_z(now),
        "source": "EUMETSAT EUMETView",
        "product": "MTG FCI Fire Radiative Power",
        "product_id": "EO:EUM:DAT:1156",
        "layer": LAYER,
        "window_minutes": LOOKBACK_MINUTES,
        "frame_minutes": FRAME_MINUTES,
        "bounds": {"lat_min": LAT_MIN, "lat_max": LAT_MAX, "lon_min": LON_MIN, "lon_max": LON_MAX},
        "latest_detection": latest_detection,
        "frame_stats": frame_stats,
        "features": features,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"FRP Sardegna: {len(features)} rilevazioni negli ultimi {LOOKBACK_MINUTES} minuti")


if __name__ == "__main__":
    main()
