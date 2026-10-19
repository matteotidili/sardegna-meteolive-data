#!/usr/bin/env python3
"""Archivio minimo delle osservazioni di pioggia e stima mobile delle ultime 48 ore.

Per DPCN, WU, WeatherLink non si inventa lo storico dal cumulato giornaliero:
si inizia a calcolare SOLO dopo 48 ore di osservazioni realmente archiviate.
Le stime sono campionate (~15 min) e non equivalgono al dato ufficiale CEDOC.
L'Aeronautica Militare attualmente non pubblica rain_today in questa app.
"""
from __future__ import annotations
import datetime as dt
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

UTC=dt.timezone.utc
ROME=ZoneInfo("Europe/Rome")
ROOT=Path("data")
HISTORY=ROOT/"rain_history.json"
OUTPUT=ROOT/"rain_48h.json"
INPUTS=(("dpcn-sardegna","stations.json"),("wunderground","wunderground.json"),("weatherlink","weatherlink.json"))
NOW=dt.datetime.now(UTC)
WINDOW=dt.timedelta(hours=48)
KEEP=dt.timedelta(hours=75)
MAX_SOURCE_AGE=dt.timedelta(minutes=95)
MAX_GAP=dt.timedelta(minutes=80)
BOUNDARY_TOLERANCE=dt.timedelta(minutes=35)
MAX_RAIN_MM=1800
MAX_PERSISTED_POINTS=400

def from_iso(value):
    if not value:return None
    try:
        v=dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return v.astimezone(UTC) if v.tzinfo else None
    except (ValueError,TypeError,OverflowError):
        return None

def as_iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00","Z")

def load_json(path):
    if not path.exists():return {}
    try:
        x=json.loads(path.read_text(encoding="utf-8"))
        return x if isinstance(x,dict) else {}
    except (OSError,ValueError):
        return {}

def day_of(epoch_min):
    return dt.datetime.fromtimestamp(epoch_min*60,UTC).astimezone(ROME).date()

def valid_amount(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and 0<=value<=MAX_RAIN_MM

def safe_station_key(net,s):
    ident=str(s.get("station_id") or s.get("name") or "").strip()
    if not ident or len(ident)>200:return None
    return net+"|"+ident

def collect_samples():
    inputs=[]
    today=NOW.astimezone(ROME).date()
    for net,filename in INPUTS:
        obj=load_json(ROOT/filename)
        if obj.get("local_day")!=str(today):continue
        for s in obj.get("stations",[]):
            if not isinstance(s,dict):continue
            k=safe_station_key(net,s)
            v=s.get("rain_today")
            t=from_iso(s.get("last"))
            if not k or not valid_amount(v) or t is None:continue
            if t>NOW+dt.timedelta(minutes=5) or NOW-t>MAX_SOURCE_AGE:continue
            if t.astimezone(ROME).date()!=today:continue
            minute=int(t.timestamp()//60)
            inputs.append((k,minute,int(round(v*10)),net,str(s.get("station_id") or s.get("name"))))
    return inputs

def update_history(history,samples):
    limit=int((NOW-KEEP).timestamp()//60)
    for k,v in list(history.items()):
        if not isinstance(v,list):
            del history[k];continue
        # Progettato per restare piccolo anche se alcuni aggiornamenti falliscono.
        points={}
        for pair in v:
            if isinstance(pair,list) and len(pair)==2 and all(isinstance(x,int) for x in pair):
                if pair[0]>=limit and 0<=pair[1]<=18000:
                    points[pair[0]]=pair[1]
        history[k]=[[minute,points[minute]] for minute in sorted(points)[-MAX_PERSISTED_POINTS:]]
        if not history[k]:del history[k]
    for key,minute,rain_tenth,_,_ in samples:
        arr=history.setdefault(key,[])
        for p in arr:
            if p[0]==minute:
                p[1]=rain_tenth
                break
        else:
            if not arr or minute>arr[-1][0]:arr.append([minute,rain_tenth])
        history[key]=arr[-MAX_PERSISTED_POINTS:]
    return history

def rolling_48h(points,reference):
    if not points:return None
    cutoff=reference-int(WINDOW.total_seconds()//60)
    # Campione di partenza PRIMA dell'inizio delle 48 h; senza di esso
    # manca la base del contatore e il totale potrebbe essere ingannevole.
    starts=[i for i,(t,_) in enumerate(points) if t<=cutoff]
    if not starts:return None
    i=starts[-1]
    t0=points[i][0]
    if cutoff-t0>int(BOUNDARY_TOLERANCE.total_seconds()//60):return None
    window_points=[p for p in points[i:] if p[0]<=reference]
    if len(window_points)<20:return None
    total=0
    prev_t,prev_val=window_points[0]
    span_days=set()
    for t,val in window_points[1:]:
        gap=t-prev_t
        if gap<=0 or gap>int(MAX_GAP.total_seconds()//60):
            return None
        d0,d1=day_of(prev_t),day_of(t)
        span_days.update((d0,d1))
        if d1==d0:
            change=val-prev_val
            if change < 0:return None  # reset anomalo, dato non qualificabile
            total+=change
        else:
            # Reset giornaliero a mezzanotte: primo campione del nuovo
            # giorno comprende gli scatti dalle 00:00 al suo timestamp.
            if d1-d0!=dt.timedelta(days=1):return None
            total+=val
        prev_t,prev_val=t,val
    if (window_points[-1][0]-window_points[0][0])<48*60-40:return None
    if len(span_days)<3:return None
    if total<0 or total>MAX_RAIN_MM*10:return None
    return round(total/10,1)

def main():
    HISTORY.parent.mkdir(parents=True,exist_ok=True)
    old=load_json(HISTORY)
    series=old.get("series") if isinstance(old.get("series"),dict) else {}
    samples=collect_samples()
    series=update_history(series,samples)
    output=[]
    for key,epoch,_,net,ident in samples:
        point=series.get(key,[])
        # Un campione ripetuto (orario invariato) non aggiorna la copertura.
        v=rolling_48h(point,epoch)
        if v is None:continue
        obs=dt.datetime.fromtimestamp(epoch*60,UTC)
        output.append({
            "network":net,"station_id":ident,"rain_48h":v,
            "last":as_iso(obs),"method":"estimated_from_daily_counters",
            "sample_minutes":15
        })
    output.sort(key=lambda x:(x["network"],x["station_id"]))
    HISTORY.write_text(json.dumps({
        "generated_at":as_iso(NOW),
        "format":"daily_counter_timeseries_v1",
        "retention_hours":75,"series":series
    },ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
    OUTPUT.write_text(json.dumps({
        "generated_at":as_iso(NOW),"window_hours":48,
        "source":"MeteoLive – stime su osservazioni archiviate dalle singole reti",
        "description":"Stima mobile da cumulati giornalieri campionati; non dato ufficiale a risoluzione temporale continua",
        "station_count":len(output),
        "coverage_note":"Le stime compaiono solo con >=48 h di campioni sufficientemente continui; prima nessun valore.",
        "stations":output
    },ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
    print("Stazioni campionate:",len(samples),"con stima 48h:",len(output),
          "serie conservate:",len(series))

if __name__=="__main__":
    main()
