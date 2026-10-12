#!/usr/bin/env python3
"""Campi vettoriali U/V a 10 m dal GRIB2 SP1 AROME-France HD.

Campionamento regolare 0,1° dal GRIB nativo 0,01°. U positivo verso est,
V positivo verso nord (m/s). Binario little endian, coppie int16 (centimetri/s)
intercalate, -32768 = cella mancante. Nessuna direzione ricavata dalla sola velocità.
"""
from __future__ import annotations
import datetime as dt
import json
import tempfile
import urllib.request
from pathlib import Path
import numpy as np
from eccodes import codes_get, codes_get_values, codes_grib_new_from_file, codes_release

BASE="https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net"
ROOT=Path("data/arome")
FORECAST=ROOT/"forecast.json"
TARGET=ROOT/"wind_vectors"
MANIFEST=ROOT/"wind_vectors.json"
MISSING=-32768

def download(url):
    request=urllib.request.Request(url,headers={"User-Agent":"SardegnaMeteoLive/1.0 (meteorological model data)"})
    with urllib.request.urlopen(request,timeout=100) as r:
        body=r.read(60_000_001)
    if len(body)>60_000_000 or not body.startswith(b"GRIB"):
        raise RuntimeError(f"SP1 non valido: {url} ({len(body)} byte)")
    return body

def decode(blob,lead):
    fields={}
    with tempfile.TemporaryFile(mode="w+b") as f:
        f.write(blob)
        f.seek(0)
        while (gid:=codes_grib_new_from_file(f)) is not None:
            try:
                short=str(codes_get(gid,"shortName"))
                if short not in {"10u","10v"}:continue
                if short in fields:raise RuntimeError(f"Duplicato {short} H+{lead}")
                nx,ny=int(codes_get(gid,"Ni")),int(codes_get(gid,"Nj"))
                lon=float(codes_get(gid,"longitudeOfFirstGridPointInDegrees"))
                lat=float(codes_get(gid,"latitudeOfFirstGridPointInDegrees"))
                lon=lon-360 if lon>180 else lon
                if (nx,ny)!=(2801,1791) or abs(lon+12)>.001 or abs(lat-55.4)>.001:
                    raise RuntimeError(f"Geometria imprevista {short} {nx}x{ny} da {lon},{lat}")
                if int(codes_get(gid,"endStep"))!=lead or str(codes_get(gid,"units"))!="m s**-1":
                    raise RuntimeError(f"Step/unità non validi: {short} H+{lead}")
                if bool(codes_get(gid,"iScansNegatively")) or bool(codes_get(gid,"jScansPositively")) or bool(codes_get(gid,"jPointsAreConsecutive")):
                    raise RuntimeError(f"Ordine griglia imprevisto: {short}")
                raw=np.asarray(codes_get_values(gid),dtype=np.float32).reshape(ny,nx)
                # Ritaglio -12..16 E e 47,5..37,5 N. Step 10 pixel = 0,1°.
                subset=raw[790:1791:10,0:2801:10].copy()
                subset[~np.isfinite(subset) | (np.abs(subset)>=300) | (subset>=9998)]=np.nan
                if subset.shape!=(101,281):raise RuntimeError("Dimensioni campionamento errate")
                fields[short]=subset
            finally:codes_release(gid)
    if set(fields)!={"10u","10v"}:raise RuntimeError(f"SP1 vettori incompleti: {fields.keys()}")
    return fields

def encode(u,v):
    mask=np.isfinite(u)&np.isfinite(v)
    if mask.mean()<.05:raise RuntimeError("Mancanza dati vettoriali nel dominio")
    arr=np.full((*u.shape,2),MISSING,dtype="<i2")
    for component,field in enumerate((u,v)):
        scaled=np.rint(np.nan_to_num(field,nan=0)*100).clip(-30000,30000).astype("<i2")
        arr[:,:,component]=np.where(mask,scaled,MISSING)
    return arr.tobytes(),float(mask.mean())

def run():
    data=json.loads(FORECAST.read_text("utf-8"))
    cycle=str(data["run_time"])
    leads=[int(item["lead_hours"]) for item in data["frames"]]
    if leads!=list(range(1,52)):raise RuntimeError("Previsione AROME H+51 incompleta")
    stamp=cycle.replace("Z","+00:00")
    run=dt.datetime.fromisoformat(stamp)
    expected=cycle
    if MANIFEST.exists():
        old=json.loads(MANIFEST.read_text("utf-8"))
        if (old.get("run_time")==expected and len(old.get("frames",[]))==51 and
            all((TARGET/f"h{lead:02d}.bin").is_file() for lead in leads)):
            print("Vettori U/V già pubblicati per questa corsa",expected,flush=True)
            return
    TARGET.mkdir(parents=True,exist_ok=True)
    frames=[]
    total=0
    for lead in leads:
        url=(f"{BASE}/pnt/{cycle}/arome/001/SP1/"
             f"arome__001__SP1__{lead:02d}H__{cycle}.grib2")
        uv=decode(download(url),lead)
        blob,coverage=encode(uv["10u"],uv["10v"])
        file=TARGET/f"h{lead:02d}.bin"
        file.write_bytes(blob)
        total+=len(blob)
        frames.append({"lead_hours":lead,"file":f"data/arome/wind_vectors/h{lead:02d}.bin",
           "valid_time":(run+dt.timedelta(hours=lead)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        print(f"Wind H+{lead:02d}: {len(blob)} byte, coverage {coverage:.1%}",flush=True)
    metadata={
        "model":"AROME-France HD",
        "producer":"Météo-France",
        "source":"https://www.data.gouv.fr/datasets/paquets-arome-resolution-0-01deg",
        "run_time":expected,
        "grid":{"west":-12.0,"east":16.0,"north":47.5,"south":37.5,
                "step_degrees":0.1,"width":281,"height":101,
                "origin":"northwest","order":"row-major",
                "components":["eastward_10m_wind","northward_10m_wind"],
                "encoding":"interleaved_signed_int16_little_endian",
                "scale":"0.01 m/s per unit","missing":MISSING},
        "frames":frames,
        "note":"Campi del vento a 10 m previsti dal modello, campionati a 0,1° dai GRIB nativi 0,01°. Linee di flusso rappresentative, non traiettorie osservate."
    }
    MANIFEST.write_text(json.dumps(metadata,ensure_ascii=False,separators=(",",":"))+"\n","utf-8")
    print("DONE",expected,len(frames),f"{total/1048576:.1f} MiB",flush=True)

if __name__=="__main__":
    run()
