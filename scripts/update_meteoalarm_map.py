#!/usr/bin/env python3
"""Genera i layer cartografici MeteoAlarm attivi e prossimi sul Mediterraneo."""
from __future__ import annotations

import gzip
import json
import math
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mapbox_vector_tile
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

OUT = Path("data/meteoalarm_map.geojson")
CACHE = Path("data/meteoalarm_geometry_cache.json")
WARNINGS = Path("data/meteoalarm.json")
BASE = "https://visservice.meteoalarm.org"
UTC = timezone.utc

WEST, SOUTH, EAST, NORTH = -10.5, 29.0, 38.0, 48.5
ZOOM = 5
PRESET = "now"
MAX_FUTURE_HOURS = 48

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


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_now() -> str:
    return iso(datetime.now(UTC))


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


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


def fetch_frames(reference_date: datetime) -> list[dict]:
    qs = urllib.parse.urlencode({"referenceDate": iso(reference_date), "preset": PRESET})
    url = f"{BASE}/api/v1/stream-buffers/live/frames?{qs}"
    payload = json.loads(get_bytes(url).decode("utf-8"))
    return payload.get("frames", [])


def tile_range():
    x0, y0 = lonlat_to_tile(WEST, NORTH, ZOOM)
    x1, y1 = lonlat_to_tile(EAST, SOUTH, ZOOM)
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            yield x, y


def fetch_tile(reference_date: datetime, x: int, y: int) -> dict:
    qs = urllib.parse.urlencode({"referenceDate": iso(reference_date), "preset": PRESET})
    url = f"{BASE}/stream-buffers/live/tiles/{ZOOM}/{x}/{y}.mvt?{qs}"
    raw = get_bytes(url)
    return mapbox_vector_tile.decode(raw, default_options={"y_coord_down": True})


def frame_profile(frames: list[dict]) -> dict[str, dict]:
    profile: dict[str, dict] = {}
    for row in frames:
        feature_id = str(row.get("featureId") or "")
        level = row.get("levelId")
        if not feature_id or level not in (1, 2, 3):
            continue
        level = int(level)
        item = profile.setdefault(feature_id, {"level": level, "types": set(), "fid": None})
        item["level"] = min(item["level"], level)
        type_id = row.get("typeId")
        if isinstance(type_id, int):
            item["types"].add(type_id)
        fid = row.get("fid")
        if isinstance(fid, int):
            item["fid"] = fid
    return profile


def future_reference_dates(now: datetime) -> list[datetime]:
    if not WARNINGS.exists():
        return []
    try:
        payload = json.loads(WARNINGS.read_text(encoding="utf-8"))
    except Exception:
        return []
    limit = now + timedelta(hours=MAX_FUTURE_HOURS)
    refs = set()
    for warning in payload.get("warnings", []):
        if warning.get("status") != "upcoming":
            continue
        start = parse_dt(warning.get("onset") or warning.get("effective"))
        end = parse_dt(warning.get("expires"))
        if not start or start <= now or start > limit:
            continue
        probe = start + timedelta(minutes=1)
        if end and probe >= end:
            probe = start + (end - start) / 2
        refs.add(probe)
    return sorted(refs)


def load_geometry_cache() -> tuple[dict[str, dict], set[str]]:
    geometries: dict[str, dict] = {}
    excluded: set[str] = set()
    if CACHE.exists():
        try:
            payload = json.loads(CACHE.read_text(encoding="utf-8"))
            geometries = payload.get("features", {}) if isinstance(payload.get("features"), dict) else {}
            excluded = set(payload.get("excluded_ids", []))
            return geometries, excluded
        except Exception:
            pass

    # Prima esecuzione: riusa le geometrie già pubblicate del layer attivo.
    if OUT.exists():
        try:
            payload = json.loads(OUT.read_text(encoding="utf-8"))
            for feature in payload.get("features", []):
                props = feature.get("properties") or {}
                feature_id = str(props.get("feature_id") or feature.get("id") or "")
                geometry = feature.get("geometry")
                country = str(props.get("country_code") or "").upper()
                if feature_id and geometry and country in MEDITERRANEAN_COUNTRY_CODES:
                    geometries[feature_id] = {
                        "geometry": geometry,
                        "country_code": country,
                        "fid": props.get("fid"),
                    }
        except Exception:
            pass
    return geometries, excluded


def collect_geometries(reference_date: datetime, target_ids: set[str]) -> tuple[dict[str, dict], set[str], int]:
    pieces: dict[str, list] = defaultdict(list)
    meta: dict[str, dict] = {}
    tile_count = 0

    for x, y in tile_range():
        decoded = fetch_tile(reference_date, x, y)
        tile_count += 1
        area_layer = decoded.get("areas") or {}
        extent = int(area_layer.get("extent") or 4096)
        for feat in area_layer.get("features", []):
            props = feat.get("properties") or {}
            feature_id = str(props.get("uuid") or "")
            if feature_id not in target_ids:
                continue
            country = str(props.get("region_code") or "").upper()
            if country not in MEDITERRANEAN_COUNTRY_CODES:
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
                meta[feature_id] = {
                    "country_code": country,
                    "fid": props.get("fidd"),
                }
            except Exception:
                continue

    clip = box(WEST, SOUTH, EAST, NORTH)
    found: dict[str, dict] = {}
    for feature_id, parts in pieces.items():
        try:
            geom = unary_union(parts)
            if not geom.is_valid:
                geom = geom.buffer(0)
            geom = geom.intersection(clip)
            if geom.is_empty:
                continue
            found[feature_id] = {
                "geometry": mapping(geom),
                "country_code": meta[feature_id]["country_code"],
                "fid": meta[feature_id].get("fid"),
            }
        except Exception:
            continue
    return found, target_ids.difference(found), tile_count


def feature_from_cache(feature_id: str, profile: dict, cache: dict[str, dict], period: str, refs: list[str] | None = None):
    cached = cache.get(feature_id)
    if not cached:
        return None
    level = int(profile["level"])
    return {
        "type": "Feature",
        "id": f"{period}:{feature_id}",
        "properties": {
            "feature_id": feature_id,
            "fid": profile.get("fid") or cached.get("fid"),
            "country_code": cached.get("country_code", ""),
            "level": level,
            "level_name": LEVEL_NAMES[level],
            "color": LEVEL_COLORS[level],
            "type_ids": sorted(profile.get("types", set())),
            "period": period,
            "reference_dates": refs or [],
            "source": "MeteoAlarm",
            "preset": PRESET,
        },
        "geometry": cached["geometry"],
    }


def build() -> tuple[dict, dict]:
    now = datetime.now(UTC)
    active_frames = fetch_frames(now)
    active_profile = frame_profile(active_frames)

    snapshots: list[tuple[datetime, dict[str, dict]]] = [(now, active_profile)]
    future_profiles: list[tuple[datetime, dict[str, dict]]] = []
    for ref in future_reference_dates(now):
        profile = frame_profile(fetch_frames(ref))
        future_profiles.append((ref, profile))
        snapshots.append((ref, profile))

    # "Prossime": nuove aree, nuovi fenomeni o cambi di livello rispetto al frame attuale.
    active_signatures = {
        (fid, type_id, level)
        for fid, item in active_profile.items()
        for type_id in item["types"]
        for level in [item["level"]]
    }
    upcoming_profile: dict[str, dict] = {}
    upcoming_refs: dict[str, set[str]] = defaultdict(set)
    for ref, profile in future_profiles:
        for feature_id, item in profile.items():
            changed_types = {
                type_id
                for type_id in item["types"]
                if (feature_id, type_id, item["level"]) not in active_signatures
            }
            if feature_id in active_profile and not changed_types and item["level"] == active_profile[feature_id]["level"]:
                continue
            current = upcoming_profile.get(feature_id)
            if current is None:
                upcoming_profile[feature_id] = {
                    "level": item["level"],
                    "types": set(changed_types or item["types"]),
                    "fid": item.get("fid"),
                }
            else:
                current["level"] = min(current["level"], item["level"])
                current["types"].update(changed_types or item["types"])
                current["fid"] = current.get("fid") or item.get("fid")
            upcoming_refs[feature_id].add(iso(ref))

    cache, excluded = load_geometry_cache()
    desired_ids = set(active_profile) | set(upcoming_profile)
    missing = desired_ids.difference(cache).difference(excluded)
    tile_count = 0

    # Greedy set cover: scarica solo i frame che coprono il maggior numero di geometrie mancanti.
    remaining_snapshots = list(snapshots)
    while missing and remaining_snapshots:
        best_index = -1
        best_ids: set[str] = set()
        for i, (_, profile) in enumerate(remaining_snapshots):
            ids = missing.intersection(profile)
            if len(ids) > len(best_ids):
                best_index = i
                best_ids = ids
        if best_index < 0 or not best_ids:
            break
        ref, _ = remaining_snapshots.pop(best_index)
        found, not_found, used_tiles = collect_geometries(ref, best_ids)
        tile_count += used_tiles
        cache.update(found)
        excluded.update(not_found)
        missing = desired_ids.difference(cache).difference(excluded)

    active_features = []
    for feature_id, item in active_profile.items():
        feature = feature_from_cache(feature_id, item, cache, "active")
        if feature:
            active_features.append(feature)

    upcoming_features = []
    for feature_id, item in upcoming_profile.items():
        feature = feature_from_cache(
            feature_id,
            item,
            cache,
            "upcoming",
            sorted(upcoming_refs.get(feature_id, set())),
        )
        if feature:
            upcoming_features.append(feature)

    features = active_features + upcoming_features
    features.sort(
        key=lambda f: (
            0 if f["properties"]["period"] == "active" else 1,
            f["properties"]["level"],
            f["properties"]["country_code"],
            f["properties"]["feature_id"],
        )
    )

    payload = {
        "type": "FeatureCollection",
        "name": "MeteoAlarm warnings Mediterranean",
        "source": "MeteoAlarm / EUMETNET",
        "source_url": "https://meteoalarm.org/en/live/",
        "generated_at": iso_now(),
        "reference_date": iso(now),
        "preset": PRESET,
        "zoom": ZOOM,
        "bounds": [WEST, SOUTH, EAST, NORTH],
        "future_horizon_hours": MAX_FUTURE_HOURS,
        "future_reference_count": len(future_profiles),
        "tile_count": tile_count,
        "active_feature_count": len(active_features),
        "upcoming_feature_count": len(upcoming_features),
        "feature_count": len(features),
        "features": features,
    }

    cache_payload = {
        "version": 1,
        "updated_at": iso_now(),
        "feature_count": len(cache),
        "excluded_count": len(excluded),
        "features": cache,
        "excluded_ids": sorted(excluded),
    }
    return payload, cache_payload


def main() -> None:
    try:
        payload, cache = build()
        if not payload["features"]:
            raise RuntimeError("nessuna area restituita dal servizio visuale")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(
            "MeteoAlarm mappa Mediterraneo: "
            f"{payload['active_feature_count']} aree attive, "
            f"{payload['upcoming_feature_count']} aree prossime, "
            f"{payload['future_reference_count']} frame futuri, "
            f"{payload['tile_count']} tile scaricate"
        )
    except Exception as exc:
        if OUT.exists():
            print(f"MeteoAlarm mappa: aggiornamento non riuscito ({type(exc).__name__}: {exc}); mantengo l'ultimo layer valido")
            return
        raise


if __name__ == "__main__":
    main()
