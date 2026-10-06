#!/usr/bin/env python3
"""Pubblica gli avvisi Atom/CAP dei paesi mediterranei coperti da MeteoAlarm."""
from __future__ import annotations

import gzip
import json
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import mapbox_vector_tile
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union

OUT = Path("data/meteoalarm.json")
MAP_OUT = Path("data/meteoalarm-map.geojson")
FEED_URL = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-italy"
ATOM = "http://www.w3.org/2005/Atom"
CAP = "urn:oasis:names:tc:emergency:cap:1.2"
UTC = timezone.utc
COUNTRIES = {"italy":"Italia", "spain":"Spagna", "france":"Francia", "slovenia":"Slovenia", "croatia":"Croazia", "bosnia-herzegovina":"Bosnia ed Erzegovina", "montenegro":"Montenegro", "greece":"Grecia", "malta":"Malta", "cyprus":"Cipro", "israel":"Israele"}
UNCOVERED = ["Marocco", "Algeria", "Tunisia", "Libia", "Egitto", "Palestina", "Libano", "Siria", "Turchia", "Albania", "Monaco"]
MVT_FRAMES_URL = "https://visservice.meteoalarm.org/api/v1/stream-buffers/live/frames"
MVT_TILE_URL = "https://visservice.meteoalarm.org/stream-buffers/live/tiles/{z}/{x}/{y}.mvt"
MVT_ZOOM = 5
MEDITERRANEAN_BOUNDS = (-10.5, 29.0, 38.0, 48.5)  # ovest, sud, est, nord
MEDITERRANEAN_REGION_CODES = {
    "ES", "FR", "MC", "IT", "SI", "HR", "BA", "ME", "AL", "GR", "MT", "CY", "IL"
}

# Il feed italiano usa normalmente una descrizione territoriale esplicita.
# Manteniamo anche i principali riferimenti geografici sardi per non perdere
# avvisi sub-regionali/provinciali.
SARDINIA_TERMS = (
    "sardegna", "sardinia", "cagliari", "sassari", "nuoro", "oristano",
    "sud sardegna", "sulcis", "iglesiente", "campidano", "ogliastra",
    "gallura", "olbia", "tempio", "carbonia", "medio campidano",
)

TYPE_NAMES = {
    1: "Vento",
    2: "Neve / ghiaccio",
    3: "Temporali",
    4: "Nebbia",
    5: "Temperature elevate",
    6: "Temperature basse",
    7: "Evento costiero",
    8: "Incendi boschivi",
    9: "Valanghe",
    10: "Pioggia",
    12: "Allagamenti / piena",
    13: "Pioggia e allagamenti",
    14: "Pericolo marino",
    15: "Siccità",
}

LEVEL_NAMES = {2: "giallo", 3: "arancione", 4: "rosso"}


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SardegnaMeteoMonitor/1.0 (+https://github.com/matteotidili/sardegna-meteolive-data)",
            "Accept": "application/atom+xml, application/cap+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def text(parent: ET.Element | None, name: str, default: str = "") -> str:
    if parent is None:
        return default
    el = parent.find(f"{{{CAP}}}{name}")
    return (el.text or "").strip() if el is not None else default


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def parameter(info: ET.Element, name: str) -> str:
    for p in info.findall(f"{{{CAP}}}parameter"):
        if text(p, "valueName") == name:
            return text(p, "value")
    return ""


def leading_code(value: str) -> int | None:
    m = re.match(r"\s*(\d+)", value or "")
    return int(m.group(1)) if m else None


def choose_info(root: ET.Element) -> ET.Element | None:
    infos = root.findall(f"{{{CAP}}}info")
    if not infos:
        return None
    for info in infos:
        lang = text(info, "language").lower()
        if lang.startswith("it"):
            return info
    return infos[0]


def parse_entry(entry, cap_url):
    """The maintained Atom feed includes CAP fields, without extra CAP requests."""
    root = ET.Element(f"{{{CAP}}}alert")
    info = ET.SubElement(root, f"{{{CAP}}}info")
    area = ET.SubElement(info, f"{{{CAP}}}area")
    for child in entry:
        if not child.tag.startswith(f"{{{CAP}}}"):
            continue
        name = child.tag.split("}")[-1]
        target = root if name in ("identifier", "sent", "status", "msgType", "message_type", "scope") else area if name in ("areaDesc", "geocode", "polygon", "circle") else info
        copied = deepcopy(child)
        for descendant in copied.iter():
            descendant.tag = f"{{{CAP}}}" + descendant.tag.split("}")[-1]
        if name == "message_type": copied.tag = f"{{{CAP}}}msgType"
        target.append(copied)
    return parse_alert(ET.tostring(root), cap_url) or []


def parse_geometry(area: ET.Element | None):
    if area is None:
        return None
    poly = text(area, "polygon")
    if poly:
        ring = []
        for token in poly.split():
            try:
                lat_s, lon_s = token.split(",", 1)
                ring.append([float(lon_s), float(lat_s)])
            except (ValueError, TypeError):
                continue
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            return {"type": "Polygon", "coordinates": [ring]}
    circle = text(area, "circle")
    if circle:
        # CAP circle = "lat,lon radius_km". Non generiamo un falso poligono:
        # conserviamo il centro/raggio separatamente nel record.
        try:
            center, radius = circle.split()
            lat_s, lon_s = center.split(",", 1)
            return {
                "type": "Point",
                "coordinates": [float(lon_s), float(lat_s)],
                "radius_km": float(radius),
            }
        except (ValueError, TypeError):
            return None
    return None


def area_geocode(area: ET.Element | None) -> str:
    if area is None:
        return ""
    for g in area.findall(f"{{{CAP}}}geocode"):
        name = text(g, "valueName").upper()
        value = text(g, "value")
        if name == "EMMA_ID" and value:
            return value
    first = area.find(f"{{{CAP}}}geocode")
    return text(first, "value") if first is not None else ""


def is_sardinia(*values: str) -> bool:
    hay = " ".join(v or "" for v in values).lower()
    return any(term in hay for term in SARDINIA_TERMS)


def http_get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SardegnaMeteoMonitor/1.0 (+https://github.com/matteotidili/sardegna-meteolive-data)",
            "Accept": "application/json, */*;q=0.5",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_mvt(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SardegnaMeteoMonitor/1.0 (+https://github.com/matteotidili/sardegna-meteolive-data)",
            "Accept": "application/vnd.mapbox-vector-tile, */*;q=0.5",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        encoding = (r.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip" or raw[:2] == b"\\x1f\\x8b":
        raw = gzip.decompress(raw)
    return raw


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_point_to_lonlat(px: float, py: float, tx: int, ty: int, zoom: int, extent: int) -> list[float]:
    n = 2 ** zoom
    world_x = (tx + px / extent) / n
    world_y = (ty + py / extent) / n
    lon = world_x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * world_y))))
    return [lon, lat]


def transform_tile_coords(value, tx: int, ty: int, zoom: int, extent: int):
    if (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        return tile_point_to_lonlat(float(value[0]), float(value[1]), tx, ty, zoom, extent)
    return [transform_tile_coords(v, tx, ty, zoom, extent) for v in value]


def polygonal_only(geom):
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        pieces = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        return unary_union(pieces) if pieces else None
    return None


def build_meteoalarm_map(reference_date: str) -> dict:
    query = urllib.parse.urlencode({"preset": "now", "referenceDate": reference_date})
    frames = http_get_json(f"{MVT_FRAMES_URL}?{query}", timeout=45).get("frames", [])

    levels: dict[int, int] = {}
    types: dict[int, set[int]] = {}
    feature_ids: dict[int, str] = {}
    for row in frames:
        if int(row.get("frame") or 0) != 1:
            continue
        try:
            fid = int(row["fid"])
            level = int(row["levelId"])
        except (KeyError, TypeError, ValueError):
            continue
        if level not in (2, 3, 4):
            continue
        levels[fid] = max(levels.get(fid, 0), level)
        try:
            types.setdefault(fid, set()).add(int(row["typeId"]))
        except (KeyError, TypeError, ValueError):
            pass
        if row.get("featureId"):
            feature_ids[fid] = str(row["featureId"])

    west, south, east, north = MEDITERRANEAN_BOUNDS
    x0, y_south = lonlat_to_tile(west, south, MVT_ZOOM)
    x1, y_north = lonlat_to_tile(east, north, MVT_ZOOM)
    x_min, x_max = sorted((x0, x1))
    y_min, y_max = sorted((y_north, y_south))

    pieces: dict[int, list] = {}
    meta: dict[int, dict] = {}
    for tx in range(x_min, x_max + 1):
        for ty in range(y_min, y_max + 1):
            url = MVT_TILE_URL.format(z=MVT_ZOOM, x=tx, y=ty) + "?" + query
            decoded = mapbox_vector_tile.decode(
                http_get_mvt(url, timeout=45),
                default_options={"y_coord_down": True},
            )
            area_layer = decoded.get("areas") or {}
            extent = int(area_layer.get("extent") or 4096)
            for feature in area_layer.get("features") or []:
                props = feature.get("properties") or {}
                region_code = str(props.get("region_code") or "").upper()
                if region_code not in MEDITERRANEAN_REGION_CODES:
                    continue
                try:
                    fid = int(feature.get("id") or props.get("fidd"))
                except (TypeError, ValueError):
                    continue
                level = levels.get(fid)
                if level not in (2, 3, 4):
                    continue
                geometry = feature.get("geometry") or {}
                if geometry.get("type") not in ("Polygon", "MultiPolygon"):
                    continue
                try:
                    geographic = {
                        "type": geometry["type"],
                        "coordinates": transform_tile_coords(
                            geometry["coordinates"], tx, ty, MVT_ZOOM, extent
                        ),
                    }
                    geom = polygonal_only(shape(geographic))
                    if geom is None or geom.is_empty:
                        continue
                    pieces.setdefault(fid, []).append(geom)
                    meta[fid] = {
                        "fid": fid,
                        "feature_id": str(props.get("uuid") or feature_ids.get(fid) or ""),
                        "region_code": region_code,
                        "level": level,
                        "type_ids": sorted(types.get(fid, set())),
                    }
                except Exception:
                    continue

    features = []
    for fid, geoms in pieces.items():
        geom = polygonal_only(unary_union(geoms))
        if geom is None or geom.is_empty:
            continue
        if not geom.is_valid:
            geom = polygonal_only(geom.buffer(0))
        if geom is None or geom.is_empty:
            continue
        features.append({
            "type": "Feature",
            "id": fid,
            "properties": meta[fid],
            "geometry": mapping(geom),
        })

    features.sort(key=lambda f: (int(f["properties"]["level"]), f["properties"]["region_code"], int(f["id"])))
    return {
        "type": "FeatureCollection",
        "metadata": {
            "source": "MeteoAlarm / EUMETNET",
            "reference_date": reference_date,
            "preset": "now",
            "generated_at": iso(datetime.now(UTC)),
            "zoom": MVT_ZOOM,
            "bounds": list(MEDITERRANEAN_BOUNDS),
            "region_codes": sorted(MEDITERRANEAN_REGION_CODES),
            "feature_count": len(features),
        },
        "features": features,
    }


def parse_alert(xml_bytes: bytes, cap_url: str) -> dict | None:
    root = ET.fromstring(xml_bytes)
    if text(root, "status") != "Actual" or text(root, "msgType") == "Cancel":
        return []
    info = choose_info(root)
    if info is None:
        return None

    areas = info.findall(f"{{{CAP}}}area")
    if not areas:
        areas = [None]

    sent = parse_dt(text(root, "sent"))
    effective = parse_dt(text(info, "effective"))
    onset = parse_dt(text(info, "onset"))
    expires = parse_dt(text(info, "expires"))
    now = datetime.now(UTC)
    if not expires or expires <= now:
        return None
    start = onset or effective or sent
    status = "upcoming" if start and start > now else "active"

    headline = text(info, "headline")
    description = text(info, "description")
    event = text(info, "event")
    aw_level = parameter(info, "awareness_level")
    aw_type = parameter(info, "awareness_type")
    level = leading_code(aw_level)
    if level is None:
        level = {"Moderate":2, "Severe":3, "Extreme":4}.get(text(info, "severity"))
    if level == 1:
        return []
    if not start or level not in (2, 3, 4):
        raise ValueError("Validità o livello non riconosciuto")
    type_code = leading_code(aw_type)

    matched = []
    for area in areas:
        area_desc = text(area, "areaDesc") if area is not None else ""
        matched.append(area)

    if not matched:
        return None

    records = []
    for area in matched:
        area_desc = text(area, "areaDesc") if area is not None else ""
        geometry = parse_geometry(area)
        circle_radius = geometry.pop("radius_km", None) if geometry and geometry.get("type") == "Point" else None
        records.append(
            {
                "id": text(root, "identifier"),
                "status": status,
                "sent": iso(sent),
                "effective": iso(effective),
                "onset": iso(onset),
                "expires": iso(expires),
                "event": event,
                "headline": headline,
                "description": description,
                "instruction": text(info, "instruction"),
                "severity": text(info, "severity"),
                "urgency": text(info, "urgency"),
                "certainty": text(info, "certainty"),
                "awareness_level": aw_level,
                "awareness_type": aw_type,
                "level": level,
                "level_name": LEVEL_NAMES.get(level, ""),
                "type_code": type_code,
                "type_name": TYPE_NAMES.get(type_code) or next((label for word, label in [("thunderstorm", "Temporali"), ("rain", "Pioggia"), ("wind", "Vento"), ("snow", "Neve / ghiaccio"), ("fog", "Nebbia")] if word in event.lower()), event or "Allerta meteo"),
                "area_desc": area_desc,
                "emma_id": area_geocode(area),
                "geometry": geometry,
                "radius_km": circle_radius,
                "cap_url": cap_url,
                "source_web": text(info, "web"),
                "sender_name": text(info, "senderName"),
            }
        )
    return records


def main() -> None:
    now = datetime.now(UTC)
    warnings, errors, countries = [], [], []
    def fetch_country(item):
        slug, name = item
        url = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-" + slug
        rows, failures = [], []
        try:
            feed = ET.fromstring(http_get(url))
            if feed.tag != f"{{{ATOM}}}feed":
                raise ValueError("Risposta non Atom")
            for entry in feed.findall(f"{{{ATOM}}}entry"):
                link = entry.find(f"{{{ATOM}}}link[@type='application/cap+xml']")
                href = link.get("href", "") if link is not None else url
                try:
                    records = parse_entry(entry, href)
                    for row in records:
                        row.update(country=name, country_code=slug)
                        rows.append(row)
                except Exception as exc:
                    failures.append(f"{href}: {type(exc).__name__}")
            updated = feed.find(f"{{{ATOM}}}updated")
            return rows, dict(country=name, status="partial" if failures else "ok", checked_at=iso(datetime.now(UTC)), feed_updated=updated.text if updated is not None else None), failures
        except Exception as exc:
            return [], dict(country=name, status="unavailable", checked_at=iso(datetime.now(UTC))), [f"{name}: {type(exc).__name__}"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for rows, country, failures in pool.map(fetch_country, COUNTRIES.items()):
            warnings.extend(rows); countries.append(country); errors.extend(failures)
    warnings = list({(w["country"], w["id"], w["area_desc"], w["onset"], w["expires"]):w for w in warnings}.values())
    feed_updated = max((c["feed_updated"] for c in countries if c.get("feed_updated")), default=None)


    warnings.sort(
        key=lambda w: (
            -(w.get("level") or 0),
            w.get("onset") or w.get("effective") or "",
            w.get("area_desc") or "",
        )
    )

    active = [w for w in warnings if w.get("status") == "active"]
    upcoming = [w for w in warnings if w.get("status") == "upcoming"]
    max_level = max((w.get("level") or 0 for w in warnings), default=0)
    active_max_level = max((w.get("level") or 0 for w in active), default=0)
    upcoming_max_level = max((w.get("level") or 0 for w in upcoming), default=0)

    map_status = "ok"
    map_feature_count = 0
    map_reference_date = iso(datetime.now(UTC))
    try:
        map_payload = build_meteoalarm_map(map_reference_date)
        MAP_OUT.parent.mkdir(parents=True, exist_ok=True)
        MAP_OUT.write_text(json.dumps(map_payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        map_feature_count = len(map_payload.get("features") or [])
    except Exception as exc:
        map_status = f"stale: {type(exc).__name__}"
        try:
            previous_map = json.loads(MAP_OUT.read_text(encoding="utf-8"))
            map_feature_count = len(previous_map.get("features") or [])
        except Exception:
            map_feature_count = 0
        errors.append(f"mappa MeteoAlarm: {type(exc).__name__}")

    payload = {
        "source": "MeteoAlarm",
        "provider": "EUMETNET members",
        "countries": countries,
        "uncovered": UNCOVERED,
        "region": "Mediterraneo",
        "map_file": "meteoalarm-map.geojson",
        "map_status": map_status,
        "map_feature_count": map_feature_count,
        "map_reference_date": map_reference_date,
        "map_region_codes": sorted(MEDITERRANEAN_REGION_CODES),
        "feed_url": FEED_URL,
        # Il controllo e l'emissione della fonte hanno orari distinti.
        "generated_at": iso(datetime.now(UTC)),
        "feed_updated": feed_updated,
        "warning_count": len(warnings),
        "active_count": len(active),
        "upcoming_count": len(upcoming),
        "max_level": active_max_level,
        "active_max_level": active_max_level,
        "upcoming_max_level": upcoming_max_level,
        "warnings": warnings,
        "errors": errors[:10],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"MeteoAlarm Mediterraneo: {len(active)} attive, {len(upcoming)} prossime, "
        f"livello max {max_level or 0}, aree mappa {map_feature_count} ({map_status})"
    )


if __name__ == "__main__":
    main()
