#!/usr/bin/env python3
"""Colleziona esclusivamente i link ai 36 frame ARPAS di Monte Rasu, prodotto DPSRI.

Le immagini restano sul server ARPAS: non vengono copiate né ripubblicate.
Fonte: https://www.sar.sardegna.it/servizi/meteo/imgradar_it.asp?prod=4
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

SOURCE = "https://www.sar.sardegna.it/servizi/meteo/imgradar_it.asp?prod=4"
OUT = Path("data/arpas_radar.json")
RADAR_PATH = "/servizi_live/meteo/radar/dBR.dpsri/"
# L'HTML pubblica assegnazioni JavaScript IMG[n].src, non un servizio GIS.
FRAME_RX = re.compile(r"IMG\[\s*\d+\s*\]\.src\s*=\s*['\"]([^'\"]+\.png)['\"]", re.I)
NAME_RX = re.compile(r"^(\d{12})\d{2,4}dBR\.dpsri\.png$", re.I)


def collect():
    request = urllib.request.Request(
        SOURCE,
        headers={"User-Agent": "SardegnaMeteoLive/1.0 (+GitHub; metadata only)", "Accept": "text/html"},
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        if response.status != 200:
            raise RuntimeError(f"ARPAS HTTP {response.status}")
        html = response.read(400_000).decode("windows-1252", "replace")
    matched = FRAME_RX.findall(html)
    found = {}
    for src in matched:
        url = urllib.parse.urljoin(SOURCE, src)
        p = urllib.parse.urlsplit(url)
        if p.scheme != "https" or p.hostname not in {"www.sar.sardegna.it", "sar.sardegna.it"}:
            continue
        if not p.path.startswith(RADAR_PATH):
            continue
        match = NAME_RX.fullmatch(p.path.rsplit("/", 1)[-1])
        if not match:
            continue
        stamp = dt.datetime.strptime(match.group(1), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
        found[stamp] = url

    frames = [
        {"time": stamp.isoformat(timespec="seconds").replace("+00:00", "Z"), "url": found[stamp]}
        for stamp in sorted(found)[-36:]
    ]
    if len(frames) < 2:
        raise RuntimeError(f"ARPAS DPSRI: meno di due frame validi ({len(frames)}): non pubblicare dati vuoti")
    timestamps = [dt.datetime.fromisoformat(x["time"].replace("Z", "+00:00")) for x in frames]
    if max(timestamps) - min(timestamps) > dt.timedelta(days=2):
        raise RuntimeError("Archivio inconsistente (> 48 ore)")
    return {
        "source": "ARPAS / Dipartimento Meteoclimatico - Monte Rasu",
        "source_url": SOURCE,
        "product": "DPSRI 1000 m",
        "unit": "mm/h",
        "range_km": 200,
        "latest_time": frames[-1]["time"],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "notice": "Radar in fase di test e calibrazione: immagini illustrative, disponibilita non continua.",
        "frames": frames,
    }


def main():
    data = collect()
    if OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))
        if old.get("frames") == data["frames"]:
            print("Nessun nuovo frame. Ultimo:", data["latest_time"], "(non modifica il JSON)")
            return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print("Nuovi metadati ARPAS:", len(data["frames"]), "frame; ultimo:", data["latest_time"])


if __name__ == "__main__":
    main()
