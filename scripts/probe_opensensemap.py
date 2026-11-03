#!/usr/bin/env python3
"""Verifica una tantum della copertura sarda openSenseMap (dati PDDL).

Solo un accesso pubblico documentato; non pubblica alcuna stazione fittizia.
"""
import collections
import datetime
import json
import urllib.parse
import urllib.request
import urllib.error

now=datetime.datetime.now(datetime.timezone.utc)
params={"bbox":"7.7,38.65,10.6,41.32","exposure":"outdoor",
        "full":"true","limit":250}
url="https://api.opensensemap.org/boxes?"+urllib.parse.urlencode(params)
req=urllib.request.Request(url,headers={"User-Agent":"SardegnaMeteoLive/1.0 (weather station research)",
                                         "Accept":"application/json"})
try:
    with urllib.request.urlopen(req,timeout=35) as response:
        boxes=json.load(response)
except urllib.error.HTTPError as exc:
    print("openSenseMap: HTTP",exc.code,exc.read(1000).decode("utf-8",errors="replace")[:700])
    raise
if not isinstance(boxes,list):
    raise ValueError("API returned non-list")
counts=collections.Counter()
fresh=[]
for box in boxes:
    sensors=box.get("sensors") or []
    obs=[]
    for sensor in sensors:
        title=str(sensor.get("title") or "").lower()
        measurement=sensor.get("lastMeasurement") or {}
        stamp=measurement.get("createdAt")
        try:
            dt=datetime.datetime.fromisoformat(stamp.replace("Z","+00:00"))
            age=(now-dt).total_seconds()/60
        except (AttributeError,ValueError):
            continue
        if age<0 or age>120:
            continue
        obs.append(title)
        if any(x in title for x in ("temperatur","temperature")):counts["temperature"]+=1
        if any(x in title for x in ("rain","precipitation","niederschlag")):counts["rain"]+=1
        if any(x in title for x in ("wind","geschwindigkeit")):counts["wind"]+=1
        if any(x in title for x in ("humidity","feuchte","umidit")):counts["humidity"]+=1
    if obs:
        fresh.append({"name":str(box.get("name",""))[:65],
                      "obs_count":len(obs),"has_temperature":any("temperatur" in o for o in obs)})
print("Sardegna openSenseMap: boxes",len(boxes),"recent boxes",len(fresh))
print("Fresh parameter counts",dict(counts))
print("Sample of fresh stations",json.dumps(fresh[:25],ensure_ascii=False))
