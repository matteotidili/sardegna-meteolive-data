#!/usr/bin/env python3
"""Scarica le osservazioni DPCN Sardegna da MeteoHub e genera data/stations.json."""
from __future__ import annotations

import gzip, io, json, os, sys, time, urllib.parse, urllib.request, zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://meteohub.agenziaitaliameteo.it"
ROME = ZoneInfo("Europe/Rome")
UTC = timezone.utc
OUT = Path("data/stations.json")
# Margine per la durata del job: il cron resta ogni 5 minuti.
MIN_RUN_INTERVAL_MIN = 4

V_TEMP="B12101"; V_RH="B13003"; V_WDIR="B11001"; V_WSPD="B11002"; V_GUST="B11041"; V_RAIN="B13011"
DESIRED={V_TEMP,V_RH,V_WDIR,V_WSPD,V_GUST,V_RAIN}

def api(method, path, token=None, body=None, timeout=120, parse_json=True):
    data=None
    headers={"User-Agent":"sardegna-meteolive/1.0"}
    if body is not None:
        data=json.dumps(body).encode("utf-8")
        headers["Content-Type"]="application/json"
    if token:
        headers["Authorization"]=f"Bearer {token}"
    req=urllib.request.Request(BASE+path,data=data,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        raw=resp.read()
        ctype=resp.headers.get("Content-Type","")
        if parse_json and ("json" in ctype or raw[:1] in (b"{",b"[")):
            return json.loads(raw.decode("utf-8"))
        return raw

def login():
    username=os.environ.get("METEOHUB_USERNAME")
    password=os.environ.get("METEOHUB_PASSWORD")
    if not username or not password:
        raise RuntimeError("Mancano METEOHUB_USERNAME/METEOHUB_PASSWORD nei GitHub Secrets")
    res = api("POST", "/auth/login", body={"username": username, "password": password})

    if isinstance(res, str):
        token = res.strip().strip('"')
    elif isinstance(res, dict):
        token = res.get("access_token") or res.get("token") or res.get("access")
    else:
        token = None

    if not token:
        raise RuntimeError("Token MeteoHub non trovato nella risposta di login")

    return token
def request_body():
    now_local=datetime.now(ROME)
    start_local=now_local.replace(hour=0,minute=0,second=0,microsecond=0)
    iso=lambda d:d.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00","Z")
    return {
        "request_name":"sardegna-meteolive-auto",
        "filters":{
            "level":[
                {"code":"1,0,0,0","active":True},
                {"code":"103,2000,0,0","active":True},
                {"code":"103,10000,0,0","active":True}
            ],
            "product":[
                {"code":"B11001","active":True},
                {"code":"B11002","active":True},
                {"code":"B11041","active":True},
                {"code":"B12101","active":True},
                {"code":"B13003","active":True},
                {"code":"B13011","active":True}
            ],
            "timerange":[
                {"code":"1,0,60","active":True},
                {"code":"2,0,3600","active":True},
                {"code":"254,0,0","active":True}
            ]
        },
        "reftime":{"from":iso(start_local),"to":iso(now_local)},
        "only_reliable":True,
        "output_format":"json",
        "dataset_names":["dpcn-sardegna"]
    }

def normalize_requests(obj):
    if isinstance(obj,list): return obj
    if isinstance(obj,dict):
        for key in ("requests","items","results","data"):
            if isinstance(obj.get(key),list): return obj[key]
    return []

def get_id(obj):
    if not isinstance(obj,dict): return None
    return obj.get("request_id") or obj.get("id") or obj.get("task_id") or obj.get("taskid")

def submit_and_wait(token):
    res=api("POST","/api/data",token,request_body())
    rid=get_id(res)
    if not rid: raise RuntimeError(f"MeteoHub non ha restituito request_id: {res}")
    print("Richiesta MeteoHub:",rid)
    deadline=time.time()+12*60
    while time.time()<deadline:
        items=normalize_requests(api("GET","/api/requests",token))
        row=next((x for x in items if str(get_id(x))==str(rid)),None)
        if row:
            status=str(row.get("status","")).upper()
            print("Stato:",status)
            if status=="SUCCESS":
                filename=row.get("fileoutput") or row.get("file_output") or row.get("filename")
                if not filename: raise RuntimeError("SUCCESS senza fileoutput")
                return rid,filename
            if status in {"FAILED","ERROR","CANCELLED"}:
                raise RuntimeError(f"Richiesta MeteoHub fallita: {row}")
        time.sleep(20)
    raise TimeoutError("Richiesta MeteoHub ancora PENDING dopo 12 minuti")

def unpack(raw):
    if raw[:2]==b"\x1f\x8b": return gzip.decompress(raw)
    if raw[:4]==b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names=[n for n in z.namelist() if n.lower().endswith((".json",".jsonl",".txt"))] or z.namelist()
            if not names: raise RuntimeError("Archivio MeteoHub vuoto")
            return z.read(names[0])
    return raw

def parse_dt(s): return datetime.fromisoformat(s.replace("Z","+00:00"))

def mval(meta,code,default=None):
    try:return meta.get(code,{}).get("v",default)
    except Exception:return default

def latest(series):
    if not series:return None,None
    dt,v,_,_=max(series,key=lambda x:x[0])
    return v,dt

def aggregate(raw):
    text=unpack(raw).decode("utf-8",errors="replace")
    rows=[]; max_dt=None
    for line in text.splitlines():
        line=line.strip()
        if not line: continue
        try: rec=json.loads(line)
        except json.JSONDecodeError: continue
        if rec.get("network")!="dpcn-sardegna": continue
        try: dt=parse_dt(rec["date"])
        except Exception: continue
        max_dt=dt if max_dt is None or dt>max_dt else max_dt
        rows.append((dt,rec))
    if not rows or max_dt is None: raise RuntimeError("Nessuna osservazione dpcn-sardegna")
    target_day=max_dt.astimezone(ROME).date()
    stations={}
    for dt,rec in rows:
        data=rec.get("data") or []
        if not data: continue
        meta=data[0].get("vars",{})
        name=mval(meta,"B01019"); lat=mval(meta,"B05001"); lon=mval(meta,"B06001"); elev=mval(meta,"B07030")
        if not name or lat is None or lon is None: continue
        key=(str(name),float(lat),float(lon))
        st=stations.setdefault(key,{"name":str(name),"network":"dpcn-sardegna","lat":float(lat),"lon":float(lon),
                                    "elev":float(elev) if elev is not None else None,"series":defaultdict(list)})
        for item in data[1:]:
            for code,obj in (item.get("vars") or {}).items():
                if code not in DESIRED: continue
                try:v=float(obj.get("v"))
                except (TypeError,ValueError):continue
                st["series"][code].append((dt,v,item.get("timerange"),item.get("level")))
    output=[]
    for st in stations.values():
        s=st["series"]
        if not any(s.get(c) for c in DESIRED): continue
        rec={k:st[k] for k in ("name","network","lat","lon","elev")}
        ts=[x for x in s.get(V_TEMP,[]) if x[3] and x[3][0]==103 and x[3][1]==2000] or s.get(V_TEMP,[])
        tk,_=latest(ts)
        rec["temp"]=round(tk-273.15,1) if tk is not None else None
        td=[v-273.15 for dt,v,_,_ in ts if dt.astimezone(ROME).date()==target_day]
        rec["tmin"]=round(min(td),1) if td else None; rec["tmax"]=round(max(td),1) if td else None
        rh,_=latest(s.get(V_RH,[])); rec["rh"]=round(rh) if rh is not None else None
        ws,_=latest(s.get(V_WSPD,[])); rec["wind_ms"]=round(ws,1) if ws is not None else None; rec["wind_kmh"]=round(ws*3.6,1) if ws is not None else None
        wd,_=latest(s.get(V_WDIR,[])); rec["wind_dir"]=round(wd) if wd is not None else None
        gd=[v for dt,v,_,_ in s.get(V_GUST,[]) if dt.astimezone(ROME).date()==target_day]
        rec["gust_ms"]=round(max(gd),1) if gd else None; rec["gust_kmh"]=round(max(gd)*3.6,1) if gd else None
        rv,_=latest(s.get(V_RAIN,[])); rec["rain_1m"]=round(rv,2) if rv is not None else None; rec["rain_rate"]=round(rv*60,1) if rv is not None else None
        rd=[v for dt,v,_,_ in s.get(V_RAIN,[]) if dt.astimezone(ROME).date()==target_day]
        rec["rain_today"]=round(sum(rd),1) if rd else None
        times=[dt for code in DESIRED for dt,*_ in s.get(code,[])]
        last=max(times) if times else None
        rec["last"]=last.isoformat().replace("+00:00","Z") if last else None
        rec["stale_min"]=round((max_dt-last).total_seconds()/60) if last else None
        rec["available"]={"temp":bool(ts),"rh":bool(s.get(V_RH)),"wind":bool(s.get(V_WSPD)),
                          "wind_dir":bool(s.get(V_WDIR)),"gust":bool(s.get(V_GUST)),"rain":bool(s.get(V_RAIN))}
        output.append(rec)
    output.sort(key=lambda x:x["name"].casefold())
    return {
        "generated_at":max_dt.isoformat().replace("+00:00","Z"),
        "local_day":str(target_day),
        "source":"MeteoHub / dpcn-sardegna",
        "station_count":len(output),
        "variable_counts":{
            "temperature":sum(1 for x in output if x["available"]["temp"]),
            "humidity":sum(1 for x in output if x["available"]["rh"]),
            "wind":sum(1 for x in output if x["available"]["wind"]),
            "gust":sum(1 for x in output if x["available"]["gust"]),
            "rain":sum(1 for x in output if x["available"]["rain"])
        },
        "stations":output
    }

def should_skip_recent_run():
    if str(os.environ.get("FORCE_UPDATE","")).lower() in {"1","true","yes"}:
        return False
    if not OUT.exists():
        return False
    try:
        prev=json.loads(OUT.read_text(encoding="utf-8"))
        ts=prev.get("checked_at")
        if not ts:
            return False
        dt=parse_dt(ts)
        age=(datetime.now(UTC)-dt.astimezone(UTC)).total_seconds()/60
        if age < MIN_RUN_INTERVAL_MIN:
            print(f"Salto aggiornamento: ultimo controllo {age:.1f} min fa")
            return True
    except Exception:
        pass
    return False

def main():
    if should_skip_recent_run():
        return
    token=login(); rid=None
    try:
        rid,filename=submit_and_wait(token)
        print("Download:",filename)
        raw=api("GET","/api/data/"+urllib.parse.quote(str(filename),safe=""),token,timeout=240,parse_json=False)
        if not isinstance(raw,(bytes,bytearray)): raw=json.dumps(raw).encode("utf-8")
        result=aggregate(bytes(raw))
        result["checked_at"]=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00","Z")
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps(result,ensure_ascii=False,separators=(",",":"))+"\n",encoding="utf-8")
        print(f"Scritte {result['station_count']} stazioni; timestamp {result['generated_at']}")
    finally:
        if rid is not None:
            try:
                api("DELETE",f"/api/requests/{urllib.parse.quote(str(rid),safe='')}",token)
                print("Richiesta temporanea eliminata da MeteoHub")
            except Exception as exc:
                print("ATTENZIONE: impossibile eliminare la richiesta temporanea:",exc,file=sys.stderr)

if __name__=="__main__":
    main()
