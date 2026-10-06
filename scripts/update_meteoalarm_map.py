#!/usr/bin/env python3
"""Genera il layer cartografico MeteoAlarm attivo sul Mediterraneo dai vector tile ufficiali."""
from __future__ import annotations

import gzip
import json
import math
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import mapbox_vector_tile
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

OUT = Path("data/meteoalarm_map.geojson")
BASE = "https://visservice.meteoalarm.org"
UTC = timezone.utc

# Inquadratura operativa del Mediterraneo usata dalla web app.
WEST, SOUTH, EAST, NORTH = -10.5, 29.0, 38.0, 48.5
ZOOM = 5
PRESET = "now"

# Paesi affacciati sul Mediterraneo coperti o potenzialmente coperti dal servizio visuale.
# La Spagna e la Francia vengono considerate sull'intero territorio nazionale; il Portogallo
# è escluso perché non è un paese mediterraneo, anche se ricade nel riquadro cartografico.
MEDITERRANEAN_COUNTRY_CODES = {
    "ES", "FR", "MC", "IT", "SI", "HR", "BA", "ME", "AL",
    "GR", "TR", "CY", "MT", "IL",
}

LEVEL_NAMES = {1: "rosso", 2: "arancione", 3: "giallo"}
LEVEL_COLORS = {1: "#d7191c", 2: "#f28e2b", 3: "#ffd54f"}

UA = {
    "User-Agent": "SardegnaMeteoMonitor/1.0 (+https://github.com/matteotidili/sardegna-meteolive-data)",
    "Accept": "application/json, application/vnd.mapbox-vector-tile, */*",
    "Accept-Encoding": "gzip",
}


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def get_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return raw


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def tile_coord_to_lonlat(x: float, y: float, tx: int, ty: int, z: int, extent: int) -> tuple[float, float]:
    # Il decoder viene istruito a mantenere l'asse Y MVT (verso il basso).
    n = 2**z
    gx = tx + (x / extent)
    gy = ty + (y / extent)
    lon = gx / n * 360.0 - 180.0
    merc = math.pi * (1.0 - 2.0 * gy / n)
    lat = math.degrees(math.atan(math.sinh(merc)))
    return lon, lat


def convert_coords(value, tx: int, ty: int, z: int, extent: int):
    if not isinstance(value, list):
        return value
    if value and isinstance(value[0], (int, float)):
        lon, lat = tile_coord_to_lonlat(float(value[0]), float(value[1]), tx, ty, z, extent)
        return [lon, lat]
    return [convert_coords(v, tx, ty, z, extent) for v in value]


def fetch_frames(reference_date: str) -> list[dict]:
    qs = urllib.parse.urlencode({"referenceDate": reference_date, "preset": PRESET})
    url = f"{BASE}/api/v1/stream-buffers/live/frames?{qs}"
    payload = json.loads(get_bytes(url).decode("utf-8"))
    return payload.get("frames", [])


def tile_range():
    x0, y0 = lonlat_to_tile(WEST, NORTH, ZOOM)
    x1, y1 = lonlat_to_tile(EAST, SOUTH, ZOOM)
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            yield x, y


def fetch_tile(reference_date: str, x: int, y: int) -> dict:
    qs = urllib.parse.urlencode({"referenceDate": reference_date, "preset": PRESET})
    url = f"{BASE}/stream-buffers/live/tiles/{ZOOM}/{x}/{y}.mvt?{qs}"
    raw = get_bytes(url)
    return mapbox_vector_tile.decode(raw, default_options={"y_coord_down": True})


def build() -> dict:
    reference_date = iso_now()
    frames = fetch_frames(reference_date)

    severity_by_feature: dict[str, int] = {}
    types_by_feature: dict[str, set[int]] = defaultdict(set)
    fids_by_feature: dict[str, int] = {}

    for row in frames:
        feature_id = str(row.get("featureId") or "")
        level_id = row.get("levelId")
        if not feature_id or level_id not in (1, 2, 3):
            continue
        level_id = int(level_id)
        current = severity_by_feature.get(feature_id)
        if current is None or level_id < current:
            severity_by_feature[feature_id] = level_id
        type_id = row.get("typeId")
        if isinstance(type_id, int):
            types_by_feature[feature_id].add(type_id)
        fid = row.get("fid")
        if isinstance(fid, int):
            fids_by_feature[feature_id] = fid

    pieces: dict[str, list] = defaultdict(list)
    country_by_feature: dict[str, str] = {}
    clip = box(WEST, SOUTH, EAST, NORTH)
    tile_count = 0

    for x, y in tile_range():
        decoded = fetch_tile(reference_date, x, y)
        tile_count += 1
        area_layer = decoded.get("areas") or {}
        extent = int(area_layer.get("extent") or 4096)
        for feat in area_layer.get("features", []):
            props = feat.get("properties") or {}
            feature_id = str(props.get("uuid") or "")
            country_code = str(props.get("region_code") or "").upper()
            if country_code not in MEDITERRANEAN_COUNTRY_CODES:
                continue
            if feature_id not in severity_by_feature:
                continue
            geom = feat.get("geometry")
            if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
                continue
            converted = {
                "type": geom["type"],
                "coordinates": convert_coords(geom.get("coordinates"), x, y, ZOOM, extent),
            }
            try:
                part = shape(converted)
                if not part.is_valid:
                    part = part.buffer(0)
                if part.is_empty:
                    continue
                pieces[feature_id].append(part)
                country_by_feature[feature_id] = str(props.get("region_code") or "")
            except Exception:
                continue

    features = []
    for feature_id, parts in pieces.items():
        try:
            geom = unary_union(parts)
            if not geom.is_valid:
                geom = geom.buffer(0)
            geom = geom.intersection(clip)
            if geom.is_empty:
                continue
            level = severity_by_feature[feature_id]
            features.append(
                {
                    "type": "Feature",
                    "id": feature_id,
                    "properties": {
                        "feature_id": feature_id,
                        "fid": fids_by_feature.get(feature_id),
                        "country_code": country_by_feature.get(feature_id, ""),
                        "level": level,
                        "level_name": LEVEL_NAMES[level],
                        "color": LEVEL_COLORS[level],
                        "type_ids": sorted(types_by_feature.get(feature_id, set())),
                        "source": "MeteoAlarm",
                        "preset": PRESET,
                    },
                    "geometry": mapping(geom),
                }
            )
        except Exception:
            continue

    features.sort(key=lambda f: (f["properties"]["level"], f["properties"]["country_code"], f["id"]))
    return {
        "type": "FeatureCollection",
        "name": "MeteoAlarm active warnings Mediterranean",
        "source": "MeteoAlarm / EUMETNET",
        "source_url": "https://meteoalarm.org/en/live/",
        "generated_at": iso_now(),
        "reference_date": reference_date,
        "preset": PRESET,
        "zoom": ZOOM,
        "bounds": [WEST, SOUTH, EAST, NORTH],
        "tile_count": tile_count,
        "feature_count": len(features),
        "features": features,
    }


def main() -> None:
    try:
        payload = build()
        if not payload["features"]:
            raise RuntimeError("nessuna area attiva restituita dal servizio visuale")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"MeteoAlarm mappa Mediterraneo: {payload['feature_count']} aree attive da {payload['tile_count']} tile")
    except Exception as exc:
        if OUT.exists():
            print(f"MeteoAlarm mappa: aggiornamento non riuscito ({type(exc).__name__}: {exc}); mantengo l'ultimo layer valido")
            return
        raise


if __name__ == "__main__":
    main()
