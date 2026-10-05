#!/usr/bin/env python3
"""Scarica le allerte MeteoAlarm per l'Italia e pubblica quelle riferite alla Sardegna."""
from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("data/meteoalarm.json")
FEED_URL = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-italy"
ATOM = "http://www.w3.org/2005/Atom"
CAP = "urn:oasis:names:tc:emergency:cap:1.2"
UTC = timezone.utc

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


def parse_alert(xml_bytes: bytes, cap_url: str) -> dict | None:
    root = ET.fromstring(xml_bytes)
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
    if expires and expires < now:
        return None

    headline = text(info, "headline")
    description = text(info, "description")
    event = text(info, "event")
    aw_level = parameter(info, "awareness_level")
    aw_type = parameter(info, "awareness_type")
    level = leading_code(aw_level)
    type_code = leading_code(aw_type)

    matched = []
    for area in areas:
        area_desc = text(area, "areaDesc") if area is not None else ""
        if is_sardinia(area_desc, headline, description):
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
                "type_name": TYPE_NAMES.get(type_code, event or "Allerta meteo"),
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
    feed = ET.fromstring(http_get(FEED_URL))
    feed_updated_el = feed.find(f"{{{ATOM}}}updated")
    feed_updated = (feed_updated_el.text or "").strip() if feed_updated_el is not None else None

    warnings = []
    errors = []
    seen = set()

    for entry in feed.findall(f"{{{ATOM}}}entry"):
        link = entry.find(f"{{{ATOM}}}link[@type='application/cap+xml']")
        if link is None:
            continue
        href = link.get("href")
        if not href:
            continue
        try:
            records = parse_alert(http_get(href), href)
            for record in records or []:
                key = (record["id"], record["area_desc"], record["onset"], record["expires"])
                if key in seen:
                    continue
                seen.add(key)
                warnings.append(record)
        except Exception as exc:
            errors.append(f"{href}: {type(exc).__name__}")

    warnings.sort(
        key=lambda w: (
            -(w.get("level") or 0),
            w.get("onset") or w.get("effective") or "",
            w.get("area_desc") or "",
        )
    )

    max_level = max((w.get("level") or 0 for w in warnings), default=0)
    payload = {
        "source": "MeteoAlarm",
        "provider": "EUMETNET members",
        "country": "Italy",
        "region": "Sardegna",
        "feed_url": FEED_URL,
        "generated_at": iso(now),
        "feed_updated": feed_updated,
        "warning_count": len(warnings),
        "max_level": max_level,
        "warnings": warnings,
        "errors": errors[:10],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"MeteoAlarm Sardegna: {len(warnings)} allerte, livello max {max_level or 0}")


if __name__ == "__main__":
    main()
