#!/usr/bin/env python3
"""AROME HD 0,01°: raster GeoPNG di precipitazione oraria sul Mediterraneo occidentale.

Fonte ufficiale Météo-France: pacchetti aperti GRIB2, SP2.
I tre campi tirf, tsnowp e tgrp sono accumuli dall'inizio del run (0..lead).
L'accumulo orario è quindi: (tirf+tsnowp+tgrp)[h] - (...)[h-1],
NON il valore diretto della singola scadenza. Unità kg m-2 = mm equivalenti.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
from eccodes import codes_get, codes_get_values, codes_grib_new_from_file, codes_release

BASE = "https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net"
OUT = Path("data/arome")
META = OUT / "forecast.json"
SOURCE = "https://www.data.gouv.fr/datasets/paquets-arome-resolution-0-01deg"
# Porzione centro-meridionale del dominio ufficiale EURW1S100.
# 37,5°N è il limite SUD ufficiale: non estendere l'immagine su Algeria/Tunisia.
# Longitudine -12/+16: intercetta l'arrivo delle perturbazioni atlantiche.
BOUNDING = {"west":-12.0, "east":16.0, "south":37.5, "north":47.5}
# Dataset originale EURW1S100; si verifichino sempre i valori effettivi del GRIB.
GRID_LON_0, GRID_LAT_0, GRID_RES = -12.0, 55.4, 0.01
LEADS = list(range(1, 52))  # 51 scadenze orarie, massimo ufficiale H+51
FIELDS = ("tirf", "tsnowp", "tgrp")
PALETTE = [
    (0.00,(0,0,0)), (0.10,(30,190,245)), (0.50,(31,130,245)),
    (1.00,(0,190,210)), (2.00,(0,185,100)), (5.00,(240,220,25)),
    (10.0,(252,148,35)), (20.0,(240,49,43)), (40.0,(178,25,166)),
    (80.0,(255,110,244))
]

def iso(date):
    return date.strftime("%Y-%m-%dT%H:%M:%SZ")

def url_for(run, lead):
    stamp=iso(run)
    return (f"{BASE}/pnt/{stamp}/arome/001/SP2/"
            f"arome__001__SP2__{lead:02d}H__{stamp}.grib2")

def head_ok(url):
    try:
        req=urllib.request.Request(url,method="HEAD",headers={"User-Agent":"SardegnaMeteoLive/1.0"})
        with urllib.request.urlopen(req,timeout=18) as res:
            size=int(res.headers.get("Content-Length","0"))
            return res.status==200 and 8_000_000<size<65_000_000
    except (urllib.error.HTTPError,TimeoutError,OSError,ValueError) as exc:
        print("Non pronto:",type(exc).__name__,url.rsplit("/",1)[-1],flush=True)
        return False

def latest_cycle():
    now=dt.datetime.now(dt.timezone.utc)
    start=now.replace(hour=(now.hour//3)*3,minute=0,second=0,microsecond=0)
    for offset in range(0,9):
        candidate=start-dt.timedelta(hours=offset*3)
        # Entrambe le scadenze devono esistere per produrre una sequenza completa.
        if head_ok(url_for(candidate,1)) and head_ok(url_for(candidate,51)):
            return candidate
    raise RuntimeError("Nessun run recente AROME HD completamente pubblicato")

def get_bytes(url):
    for attempt in range(3):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"SardegnaMeteoLive/1.0"})
            with urllib.request.urlopen(req,timeout=90) as r:
                if r.status!=200:raise RuntimeError(f"HTTP {r.status}")
                blob=r.read(65_000_001)
            if len(blob)>65_000_000 or not blob.startswith(b"GRIB"):
                raise RuntimeError("Fichier GRIB2 non valido")
            return blob
        except Exception as exc:
            print(f"Tentativo {attempt+1} fallito: {exc}",flush=True)
            if attempt==2:raise
            time.sleep(2+attempt*3)

def crop_geometry():
    # Extent raster dei bordi esterni dei pixel, non dei centri cella.
    c0=int(round((BOUNDING["west"]-GRID_LON_0)/GRID_RES))
    c1=int(round((BOUNDING["east"]-GRID_LON_0)/GRID_RES))+1
    r0=int(round((GRID_LAT_0-BOUNDING["north"])/GRID_RES))
    r1=int(round((GRID_LAT_0-BOUNDING["south"])/GRID_RES))+1
    return (r0,r1,c0,c1),[
        [round(GRID_LAT_0-(r1-.5)*GRID_RES,5),round(GRID_LON_0+(c0-.5)*GRID_RES,5)],
        [round(GRID_LAT_0-(r0-.5)*GRID_RES,5),round(GRID_LON_0+(c1-.5)*GRID_RES,5)]
    ]

def decode_sp2(blob, lead, window):
    fields={}
    r0,r1,c0,c1=window
    with tempfile.TemporaryFile(mode="w+b") as handle:
        handle.write(blob);handle.seek(0)
        while (gid:=codes_grib_new_from_file(handle)) is not None:
            try:
                name=str(codes_get(gid,"shortName"))
                if name not in FIELDS:continue
                if name in fields:raise RuntimeError(f"GRIB duplicato {name}")
                unit=str(codes_get(gid,"units"))
                step_type=str(codes_get(gid,"stepType"))
                start,end=int(codes_get(gid,"startStep")),int(codes_get(gid,"endStep"))
                nx,ny=int(codes_get(gid,"Ni")),int(codes_get(gid,"Nj"))
                lon0=float(codes_get(gid,"longitudeOfFirstGridPointInDegrees"))
                lat0=float(codes_get(gid,"latitudeOfFirstGridPointInDegrees"))
                lon0=lon0-360 if lon0>180 else lon0
                dx=float(codes_get(gid,"iDirectionIncrementInDegrees"))
                dy=float(codes_get(gid,"jDirectionIncrementInDegrees"))
                if not (abs(lon0-GRID_LON_0)<.001 and abs(lat0-GRID_LAT_0)<.001
                        and abs(dx-GRID_RES)<.001 and abs(dy-GRID_RES)<.001
                        and nx==2801 and ny==1791
                        and not bool(codes_get(gid,"iScansNegatively"))
                        and not bool(codes_get(gid,"jScansPositively"))
                        and not bool(codes_get(gid,"jPointsAreConsecutive"))):
                    raise RuntimeError(f"Geometria AROME inattesa: {name} {nx}x{ny} {lon0}/{lat0}")
                if step_type!="accum" or start!=0 or end!=lead or unit!="kg m**-2":
                    raise RuntimeError(f"Accumulazione GRIB non valida: {name} {unit} {start}/{end} {step_type}")
                v=np.asarray(codes_get_values(gid),dtype=np.float32).reshape(ny,nx)
                grid=v[r0:r1,c0:c1].copy()
                # ecCodes nel rettangolo esterno restituisce il sentinella 9999, non NaN.
                grid[(~np.isfinite(grid)) | (grid>=9998) | (grid<-0.01)]=np.nan
                # L'area realmente simulata è trapezoidale dentro il GRIB rettangolare.
                # Mantenere le celle non definite trasparenti senza inventare dati.
                valid_fraction=float(np.isfinite(grid).mean())
                if valid_fraction < .05:
                    raise RuntimeError(f"AROME: dominio quasi interamente privo di dati {name}: {valid_fraction:.1%}")
                if name=="tirf":
                    print(f"Copertura valida {name} H+{lead}: {valid_fraction:.1%}",flush=True)
                fields[name]=grid
            finally:
                codes_release(gid)
    if set(fields)!=set(FIELDS):
        raise RuntimeError(f"GRIB incompleto: trovati {sorted(fields)}")
    arr=sum(fields.values())
    # I GRIB originali sono cumulati dal ciclo: non presentare come mm/h.
    return arr

def rgba_from_mm(data):
    valid=np.isfinite(data)&(data>=.10)
    v=np.nan_to_num(data,nan=0,posinf=0,neginf=0)
    rgb=np.stack([np.interp(v,[x for x,_ in PALETTE],[y[i] for _,y in PALETTE])
                  for i in range(3)],axis=-1).astype(np.uint8)
    alpha=np.where(valid,np.interp(v,[0.1,1,5,10,25,80],[110,155,185,210,225,235]),0).astype(np.uint8)
    return np.dstack((rgb,alpha))

def run():
    cycle=latest_cycle()
    print("Run disponibile:",iso(cycle),flush=True)
    if META.exists():
        try:
            existing=json.loads(META.read_text(encoding="utf-8"))
            if (existing.get("run_time")==iso(cycle)
                and len(existing.get("frames",[]))==len(LEADS)
                and existing.get("domain")=="EURW1S100-WesternMediterranean-H51"):
                print("Ciclo invariato: nessuna modifica",flush=True)
                return
        except (ValueError,OSError):pass
    window,bounds=crop_geometry()
    rows,cols=window[1]-window[0],window[3]-window[2]
    print("Raster:",cols,"x",rows,"bounds",bounds,flush=True)
    # Ogni scadenza contiene precipitazioni cumulate da H0.
    # L'analisi H0 di SP2 non ha precipitazioni: cumulato H0=0.
    previous=np.zeros((rows,cols),dtype=np.float32)
    frames=[]
    OUT.mkdir(parents=True,exist_ok=True)
    for lead in LEADS:
        blob=get_bytes(url_for(cycle,lead))
        accumulated=decode_sp2(blob,lead,window)
        difference=accumulated-previous
        good=np.isfinite(accumulated)&np.isfinite(previous)
        negatives=int(np.count_nonzero((difference<-.1)&good))
        if negatives>int(difference.size*.01):
            raise RuntimeError(f"Accumulazioni non monotone al lead {lead}: {negatives} celle")
        hourly=np.where(good,np.maximum(difference,0),np.nan)
        filename=f"precip_h{lead:02d}.png"
        Image.fromarray(rgba_from_mm(hourly),"RGBA").save(OUT/filename,optimize=True)
        maximum=float(np.nanmax(hourly))
        frames.append({
            "lead_hours":lead,
            "valid_time":iso(cycle+dt.timedelta(hours=lead)),
            "accumulation_hours":1,
            "image":f"data/arome/{filename}",
            "max_mm":round(maximum,1)
        })
        previous=accumulated
        print("Frame",lead,filename,"max",round(maximum,1),"size",(OUT/filename).stat().st_size,flush=True)
    meta={
        "model":"AROME-France HD",
        "domain":"EURW1S100-WesternMediterranean-H51",
        "forecast_horizon_hours":LEADS[-1],
        "domain_note":"Copertura effettiva AROME variabile entro la griglia EURW1S100; sud del Mediterraneo non coperto sotto 37,5°N. Celle mancanti trasparenti.",
        "producer":"Météo-France",
        "source":SOURCE,
        "license":"Licence Ouverte / Open Licence 2.0",
        "resolution_degrees":0.01,
        "product":"precipitation_1h",
        "description":"Precipitazioni totali previste (pioggia + neve + graupel), equivalenti in acqua; accumulo dell'ora precedente.",
        "run_time":iso(cycle),
        "generated_at":iso(dt.datetime.now(dt.timezone.utc)),
        "bounds":bounds,
        "frames":frames,
    }
    META.write_text(json.dumps(meta,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
    print("JSON salvato:",META,len(frames),"frame",flush=True)

if __name__=="__main__":
    run()
