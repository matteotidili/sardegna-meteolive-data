#!/usr/bin/env python3
"""Legge il timestamp più recente del layer EUMETSAT MTG GeoColour.

Non scarica l'immagine satellitare: pubblica solo un piccolo JSON con il
timestamp più recente disponibile e l'URL GetMap per una singola immagine
ritagliata sulla Sardegna. Il browser carica poi direttamente il PNG da
EUMETView.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CAPS = "https://view.eumetsat.int/geoserver/wms?service=WMS&request=GetCapabilities"
WMS = "https://view.eumetsat.int/geoserver/wms"
LAYER = "mtg_fd:rgb_geocolour"
OUT = Path("data/satellite_eumetsat.json")

# Area leggermente più ampia della Sardegna per evitare bordi durante piccoli pan.
BBOX = (7.65, 38.65, 10.25, 41.45)  # lon_min, lat_min, lon_max, lat_max
WIDTH = 1000
HEIGHT = 1080

ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z")

def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

def get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sardegna-meteolive/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def find_layer(root):
    for node in root.iter():
        if localname(node.tag) != "Layer":
            continue
        name = None
        for child in node:
            if localname(child.tag) == "Name":
                name = (child.text or "").strip()
                break
        if name == LAYER:
            return node
    return None

def latest_time(layer) -> str:
    candidates = []
    for node in layer.iter():
        if localname(node.tag) not in {"Dimension", "Extent"}:
            continue
        if (node.attrib.get("name") or "").lower() != "time":
            continue
        default = node.attrib.get("default")
        text = (node.text or "").strip()
        blob = " ".join(x for x in (default, text) if x)
        candidates.extend(ISO_RE.findall(blob))

    if not candidates:
        raise RuntimeError("Nessun timestamp TIME trovato per " + LAYER)

    def dt(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    return max(candidates, key=dt)

def main():
    raw = get_bytes(CAPS)
    root = ET.fromstring(raw)
    layer = find_layer(root)
    if layer is None:
        raise RuntimeError("Layer non trovato nel GetCapabilities: " + LAYER)

    ts = latest_time(layer)

    params = {
        "service": "WMS",
        "request": "GetMap",
        "version": "1.1.1",
        "layers": LAYER,
        "styles": "",
        "format": "image/png",
        "transparent": "true",
        "srs": "EPSG:4326",
        "bbox": ",".join(str(x) for x in BBOX),
        "width": str(WIDTH),
        "height": str(HEIGHT),
        "time": ts,
    }
    image_url = WMS + "?" + urllib.parse.urlencode(params)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": "EUMETSAT EUMETView",
        "product": "MTG GeoColour RGB",
        "layer": LAYER,
        "time": ts,
        "bounds": [[BBOX[1], BBOX[0]], [BBOX[3], BBOX[2]]],
        "width": WIDTH,
        "height": HEIGHT,
        "image_url": image_url,
        "attribution": "© EUMETSAT / NASA",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n"

    old = OUT.read_text(encoding="utf-8") if OUT.exists() else None
    if old == text:
        print("Timestamp satellitare invariato:", ts)
        return

    OUT.write_text(text, encoding="utf-8")
    print("Ultimo frame EUMETSAT:", ts)

if __name__ == "__main__":
    main()
