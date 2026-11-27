#!/usr/bin/env python3
"""Scarica ICON-2I e pubblica prodotti raster regolari per la Sardegna."""
from __future__ import annotations
import datetime as dt, json, math, os, re, tempfile, time, urllib.error, urllib.request
from html.parser import HTMLParser
from pathlib import Path
import numpy as np
from PIL import Image
from eccodes import codes_get, codes_get_values, codes_grib_new_from_file, codes_release
from icon2i_parameters import PARAMETERS, BOUNDS, STEP

BASE="https://meteohub.agenziaitaliameteo.it/nwp/ICON-2I_SURFACE_PRESSURE_LEVELS"
OUT=Path("data/icon2i"); META=OUT/"forecast.json"
W,E,S,N=(BOUNDS[k] for k in ("west","east","south","north"))
WIDTH=round((E-W)/STEP)+1; HEIGHT=round((N-S)/STEP)+1
LON0,LAT0=3.0,33.7; SOURCE_NX=SOURCE_NY=761; SOURCE_STEP=0.05

class Links(HTMLParser):
    def __init__(self): super().__init__(); self.href=[]
    def handle_starttag(self,tag,attrs):
        if tag=="a":
            h=dict(attrs).get("href")
            if h: self.href.append(h)

def get(url,timeout=60):
    req=urllib.request.Request(url,headers={"User-Agent":"SardegnaMeteoLive/1.0"})
    with urllib.request.urlopen(req,timeout=timeout) as r: return r.read()

def latest_run():
    parser=Links(); parser.feed(get(BASE+"/",25).decode("utf8","replace"))
    runs=sorted({x.strip("/") for x in parser.href if re.fullmatch(r"20\d{8}",x.strip("/"))},reverse=True)
    now=dt.datetime.now(dt.timezone.utc)
    for stamp in runs:
        run=dt.datetime.strptime(stamp,"%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
        if run>now+dt.timedelta(hours=1) or now-run>dt.timedelta(hours=30): continue
        # Latest may still be publishing. Verify all target variable listings.
        try:
            for p in PARAMETERS.values(): find_file(stamp,p["source"])
            return run
        except Exception as e: print("Corsa incompleta",stamp,e,flush=True)
    raise RuntimeError("Nessuna corsa ICON-2I completa nelle ultime 30 ore")

def find_file(stamp,variable):
    parser=Links(); parser.feed(get(f"{BASE}/{stamp}/{variable}/",25).decode("utf8","replace"))
    candidates=[x for x in parser.href if x.endswith(".grib")]
    if variable=="TOT_PREC": candidates=[x for x in candidates if x.endswith("surface-0.grib")]
    elif variable=="CLCT": candidates=[x for x in candidates if x.endswith("surface-0.grib")]
    if len(candidates)!=1: raise RuntimeError(f"File ambiguo/non disponibile per {variable}: {candidates}")
    return f"{BASE}/{stamp}/{variable}/{candidates[0]}"

def index_window():
    # Source rows go south→north; columns west→east. ICON-2I's documented
    # regular_ll extent is 3.0E..41.0E, 33.7N..71.7N at 0.05 degrees.
    c0=round((W-LON0)/SOURCE_STEP); c1=round((E-LON0)/SOURCE_STEP)+1
    r0=round((S-LAT0)/SOURCE_STEP); r1=round((N-LAT0)/SOURCE_STEP)+1
    return r0,r1,c0,c1

def download(url):
    for n in range(3):
        try: return get(url,180)
        except Exception:
            if n==2: raise
            time.sleep(2+n*3)

def read_grib(blob, expected, accumulated=False):
    out=np.full((73,HEIGHT,WIDTH),np.nan,dtype=np.float32)
    previous=None
    base=None
    with tempfile.TemporaryFile() as f:
        f.write(blob); f.seek(0); i=0
        while (gid:=codes_grib_new_from_file(f)) is not None:
            try:
                short=str(codes_get(gid,"shortName"))
                end=codes_get(gid,"endStep")
                lead=int(end) if isinstance(end,int) or str(end).isdigit() else (int(str(end).removesuffix("m")) if accumulated and int(codes_get(gid,"lengthOfTimeRange")) else (0 if int(codes_get(gid,"forecastTime"))==0 else int(codes_get(gid,"forecastTime"))//60))
                if short!=expected: raise RuntimeError(f"ShortName {short}, atteso {expected}")
                if int(codes_get(gid,"Ni"))!=SOURCE_NX or int(codes_get(gid,"Nj"))!=SOURCE_NY: raise RuntimeError("Griglia ICON-2I cambiata")
                if lead<0 or lead>72: continue
                arr=np.asarray(codes_get_values(gid),dtype=np.float32).reshape(SOURCE_NY,SOURCE_NX)
                # Crop first, then sample onto a stable 0.01° application grid.
                r0,r1,c0,c1=index_window(); crop=arr[r0:r1,c0:c1]
                ys=np.rint(np.linspace(0,crop.shape[0]-1,HEIGHT)).astype(int)
                xs=np.rint(np.linspace(0,crop.shape[1]-1,WIDTH)).astype(int)
                field=crop[np.ix_(ys,xs)]
                if accumulated:
                    current=field
                    if lead==0: base=current.copy(); previous=current.copy(); continue
                    if previous is not None: field=np.maximum(0,current-previous)
                    previous=current.copy()
                    slot=lead
                else: slot=lead
                if slot<0 or slot>=73: continue
                if np.isfinite(out[slot]).any(): raise RuntimeError(f"Scadenza duplicata {lead}")
                out[slot]=field; i+=1
            finally: codes_release(gid)
    required=out[1:73] if accumulated else out
    if not np.isfinite(required).any(axis=(1,2)).all():
        missing=np.where(~np.isfinite(required).any(axis=(1,2)))[0].tolist()
        raise RuntimeError(f"Scadenze mancanti per {expected}: {missing}")
    return out

def colorize(data,param):
    stops=param["stops"]; vals=data*param["factor"]+param["offset"]
    rgba=np.zeros((*vals.shape,4),dtype=np.uint8); valid=np.isfinite(vals)
    if param["scale"]=="precip": valid &= vals>=0.05
    rgba[:,:,3]=np.where(valid,230,0).astype(np.uint8)
    for i in range(len(stops)-1):
        v0,c0=stops[i]; v1,c1=stops[i+1]
        mask=valid&(vals>=v0)&(vals<=v1)
        t=np.clip((vals-v0)/max(1e-9,v1-v0),0,1)
        for ch in range(3): rgba[:,:,ch][mask]=(c0[ch]+(c1[ch]-c0[ch])*t[mask]).astype(np.uint8)
    below=valid&(vals<stops[0][0]); above=valid&(vals>stops[-1][0])
    rgba[below,:3]=stops[0][1]; rgba[above,:3]=stops[-1][1]
    return Image.fromarray(rgba,"RGBA")

def iso(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")

def main():
    run=latest_run(); stamp=run.strftime("%Y%m%d%H"); print("ICON-2I",stamp,flush=True)
    OUT.mkdir(parents=True,exist_ok=True); (OUT/"products").mkdir(exist_ok=True)
    all_data={}
    for key,p in PARAMETERS.items():
        print("Download",p["source"],flush=True)
        raw=download(find_file(stamp,p["source"]))
        all_data[key]=read_grib(raw,p["short"],p.get("accumulated",False))
    # Physical grid values make map clicks report the model cell exactly.
    params={}
    for key,p in PARAMETERS.items():
        params[key]={k:p[k] for k in ("label","unit","group","scale","stops")}
        folder=OUT/"products"/key; folder.mkdir(parents=True,exist_ok=True)
        for lead in range(1,73):
            colorize(all_data[key][lead],p).save(folder/f"h{lead:02d}.webp",format="WEBP",lossless=True,method=4)
    frames=[{"lead_hours":h,"valid_time":iso(run+dt.timedelta(hours=h))} for h in range(1,73)]
    manifest={"model":"ICON-2I","producer":"ItaliaMeteo / ARPAE / CINECA","source":BASE,
      "run_time":iso(run),"generated_at":iso(dt.datetime.now(dt.timezone.utc)),"forecast_horizon_hours":72,
      "bounds":[[S,W],[N,E]],"grid":{"step_degrees":STEP,"width":WIDTH,"height":HEIGHT,"encoding":"float32-le"},
      "product_path_template":"data/icon2i/products/{parameter}/h{lead:02d}.webp",
      "parameters":params,"frames":frames}
    tmp=META.with_suffix(".json.tmp"); tmp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); os.replace(tmp,META)
    print("Published manifest",len(frames),"frames",len(params),"parameters",flush=True)

if __name__=="__main__": main()
