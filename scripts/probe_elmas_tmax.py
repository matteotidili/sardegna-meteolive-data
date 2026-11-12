#!/usr/bin/env python3
"""Diagnostica non distruttiva: sorgente dei candidati Tmax Cagliari-Elmas."""
import csv,io
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from update_am import fetch_synops,fetch_metars,decode_report,decode_metar,parse_obs_time

rome=ZoneInfo("Europe/Rome")
today=datetime.now(timezone.utc).astimezone(rome).date()
print("DATA LOCALE:",today)
candidates=[]
try:
  synops=fetch_synops()
  for row in csv.reader(io.StringIO(synops)):
    if len(row)<7 or row[0].strip()!="16560":continue
    try:
      dt=datetime(int(row[1]),int(row[2]),int(row[3]),int(row[4]),int(row[5]),tzinfo=timezone.utc)
    except (ValueError,TypeError):continue
    if dt.astimezone(rome).date()!=today:continue
    raw=",".join(row[6:]).strip()
    dec=decode_report(raw,"16560")
    if dec.get("temp") is not None:
      candidates.append((dt,"SYNOP",dec["temp"],raw))
    if dec.get("reported_tmax") is not None:
      print("SYNOP EXTREMO ORIGINALE:",dt.isoformat(),dec["reported_tmax"],raw)
except Exception as e:
  print("SYNOP non raggiungibile:",type(e).__name__,str(e)[:150])
try:
  for row in fetch_metars():
    if str(row.get("icaoId") or "").upper()!="LIEE":continue
    dt=parse_obs_time(row.get("obsTime") or row.get("reportTime"))
    if dt is None or dt.astimezone(rome).date()!=today:continue
    dec=decode_metar(row)
    if dec.get("temp") is not None:
      candidates.append((dt,"METAR",dec["temp"],row.get("rawOb") or ""))
except Exception as e:
  print("METAR non raggiungibile:",type(e).__name__,str(e)[:150])

for dt,source,temp,raw in sorted(candidates,key=lambda x:(x[0],x[1])):
  print("OSSERVAZIONE",dt.astimezone(rome).strftime("%Y-%m-%d %H:%M %Z"),source,f"{temp:.1f}°C",raw[:190])
if candidates:
  m=max(c[2] for c in candidates)
  print("MASSIMA CAMPIONATA",m)
  print("SORGENTI MASSIMA",[(x[0].isoformat(),x[1],x[3][:150]) for x in candidates if x[2]==m])
else:
  print("NESSUNA OSSERVAZIONE DISPONIBILE")
