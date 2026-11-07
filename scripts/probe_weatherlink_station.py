#!/usr/bin/env python3
"""Test autorizzazione WeatherLink v2, senza esportare dati privati o chiavi."""
import json, os, urllib.parse, urllib.request, urllib.error
UUID = "b9af613f-888a-4597-9055-c481da9903a2"
key=os.environ.get("WEATHERLINK_API_KEY","").strip()
secret=os.environ.get("WEATHERLINK_API_SECRET","").strip()
if not key or not secret:
    print("WeatherLink: credenziali assenti nei secret del repository")
    raise SystemExit(0)
base="https://api.weatherlink.com/v2"
headers={"X-Api-Secret":secret,"Accept":"application/json","User-Agent":"SardegnaMeteoLive/1.0"}
def request(path):
    url=base+path+"?"+urllib.parse.urlencode({"api-key":key})
    req=urllib.request.Request(url,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=25) as r:
            obj=json.load(r)
            return r.status,obj
    except urllib.error.HTTPError as e:
        return e.code,{}
    except (urllib.error.URLError,TimeoutError,ValueError):
        return "error",{}
status,payload=request("/stations")
station_rows=payload.get("stations",[]) if isinstance(payload,dict) else []
match=[s for s in station_rows if str(s.get("station_id_uuid","")).lower()==UUID]
print("WeatherLink /stations:",status,"totale",len(station_rows),
      "stazione UUID nella lista autorizzata:",bool(match))
if match:
    s=match[0]
    print("Stazione UUID: flag private:",s.get("private"),
          "attiva:",s.get("active"))
status,data=request("/current/"+UUID)
print("WeatherLink /current/UUID: HTTP",status,
      "sensori restituiti:",len(data.get("sensors",[])) if status==200 and isinstance(data,dict) else 0)
if isinstance(data,dict):
    # Struttura della risposta, nessuna osservazione / chiave privata nel log.
    print("Current API response fields:", sorted(str(k) for k in data.keys())[:30])
    for k in ("code","status","error","error_code","message"):
        if k in data:
            value=data[k]
            print("Current status field",k,str(value)[:140])
