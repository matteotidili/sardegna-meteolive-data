#!/usr/bin/env python3
"""Validate and publish one consistent AROME forecast and 10 m wind bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import struct
import subprocess
import time

LEADS = list(range(1, 52))
FORECAST = "data/arome/forecast.json"
VECTORS = "data/arome/wind_vectors.json"
GRID = {
    "west": -12.0, "east": 16.0, "north": 47.5, "south": 37.5,
    "step_degrees": 0.1, "width": 281, "height": 101,
    "origin": "northwest", "order": "row-major",
    "components": ["eastward_10m_wind", "northward_10m_wind"],
    "encoding": "interleaved_signed_int16_little_endian",
    "scale": "0.01 m/s per unit", "missing": -32768,
}


def run_time(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("AROME: orario senza fuso")
    return stamp.astimezone(timezone.utc)


def frames(document, label):
    rows = document.get("frames")
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or [row.get("lead_hours") for row in rows] != LEADS):
        raise ValueError(f"AROME: {label} deve contenere una volta ciascuna scadenza H+01..51")
    return rows


def validate_bundle(root=Path(".")):
    """Check identity, forecast times and every U/V binary before publication."""
    root = Path(root)
    forecast = json.loads((root / FORECAST).read_text(encoding="utf-8"))
    vectors = json.loads((root / VECTORS).read_text(encoding="utf-8"))
    if not isinstance(forecast, dict) or not isinstance(vectors, dict):
        raise ValueError("AROME: il manifest deve essere un oggetto JSON")
    cycle = forecast.get("run_time")
    if not cycle or vectors.get("run_time") != cycle:
        raise ValueError("AROME: corsa dei vettori diversa dalla previsione raster")
    start = run_time(cycle)
    if forecast.get("model") != "AROME-France HD" or vectors.get("model") != "AROME-France HD":
        raise ValueError("AROME: modello inatteso")
    if forecast.get("forecast_horizon_hours") != 51:
        raise ValueError("AROME: orizzonte diverso da H+51")
    grid = vectors.get("grid") or {}
    if not isinstance(grid, dict) or any(grid.get(key) != value for key, value in GRID.items()):
        raise ValueError("AROME: geometria o codifica dei vettori inattesa")
    raster_frames = frames(forecast, "raster")
    vector_frames = frames(vectors, "vettori")
    cells = GRID["width"] * GRID["height"]
    for raster, vector in zip(raster_frames, vector_frames):
        lead = raster["lead_hours"]
        valid = start + timedelta(hours=lead)
        if run_time(raster.get("valid_time")) != valid or run_time(vector.get("valid_time")) != valid:
            raise ValueError(f"AROME: validità non coerente per H+{lead:02d}")
        expected_file = f"data/arome/wind_vectors/h{lead:02d}.bin"
        if vector.get("file") != expected_file:
            raise ValueError(f"AROME: percorso vettori inatteso per H+{lead:02d}")
        raw = (root / expected_file).read_bytes()
        if len(raw) != cells * 4:
            raise ValueError(f"AROME: dimensione binaria errata per H+{lead:02d}")
        valid_cells = 0
        for u, v in struct.iter_unpack("<hh", raw):
            if (u == GRID["missing"]) != (v == GRID["missing"]):
                raise ValueError(f"AROME: coppia U/V incompleta per H+{lead:02d}")
            if u != GRID["missing"]:
                if abs(u) > 30000 or abs(v) > 30000:
                    raise ValueError(f"AROME: valore U/V fuori scala per H+{lead:02d}")
                valid_cells += 1
        if valid_cells / cells < 0.05:
            raise ValueError(f"AROME: dati vettoriali insufficienti per H+{lead:02d}")
    return cycle


def candidate_is_current(candidate, remote_forecast, mode):
    """Never replace a newer remote run; vector repair must match its raster."""
    if mode == "vectors":
        return run_time(candidate) == run_time(remote_forecast)
    if mode == "full":
        return run_time(candidate) >= run_time(remote_forecast)
    raise ValueError("Modalità pubblicazione AROME non valida")


def git(root, *args, check=True, capture_output=False):
    return subprocess.run(["git", *args], cwd=root, text=True, check=check,
                          capture_output=capture_output)


def publish(mode="full", root=Path(".")):
    """One commit, with a remote freshness check on every non-forced push retry."""
    root = Path(root)
    candidate = validate_bundle(root)
    files = [VECTORS, "data/arome/wind_vectors/"]
    if mode == "full":
        files += [FORECAST, "data/arome/products/"]
        files += [f"data/arome/precip_h{lead:02d}.png" for lead in LEADS]
    elif mode != "vectors":
        raise ValueError("Modalità pubblicazione AROME non valida")
    git(root, "config", "user.name", "sardegna-meteolive-data-bot")
    git(root, "config", "user.email", "actions@users.noreply.github.com")
    git(root, "add", "--", *files)
    changes = git(root, "diff", "--cached", "--quiet", check=False)
    if changes.returncode == 0:
        print("AROME: corsa invariata, nessuna pubblicazione", flush=True)
        return False
    if changes.returncode != 1:
        changes.check_returncode()
    message = ("Aggiorna AROME HD H+51 e vettori 10 m della stessa corsa"
               if mode == "full" else "Ripristina vettori AROME 10 m della corsa pubblicata")
    git(root, "commit", "-m", message)
    for attempt in range(1, 6):
        git(root, "fetch", "origin", "main")
        remote = json.loads(git(root, "show", "origin/main:" + FORECAST,
                                capture_output=True).stdout)
        if not candidate_is_current(candidate, remote.get("run_time"), mode):
            print("AROME: previsione remota cambiata, non pubblico la candidata", candidate,
                  "sulla corsa", remote.get("run_time"), flush=True)
            return False
        rebased = git(root, "rebase", "origin/main", check=False)
        if rebased.returncode:
            git(root, "rebase", "--abort", check=False)
            raise RuntimeError("AROME: conflitto al rebase, nessun dato pubblicato")
        if validate_bundle(root) != candidate:
            raise ValueError("AROME: corsa candidata cambiata durante la pubblicazione")
        if git(root, "push", "origin", "HEAD:main", check=False).returncode == 0:
            print("AROME: raster e vettori coerenti pubblicati per", candidate, flush=True)
            return True
        if attempt < 5:
            time.sleep(attempt)
    raise RuntimeError("AROME: pubblicazione non riuscita dopo cinque tentativi")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "publish"))
    parser.add_argument("--mode", choices=("full", "vectors"), default="full")
    args = parser.parse_args()
    if args.command == "publish":
        publish(args.mode)
    else:
        print("AROME: raster e 51 campi U/V convalidati per", validate_bundle(), flush=True)


if __name__ == "__main__":
    main()
