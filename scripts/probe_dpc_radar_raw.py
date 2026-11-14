#!/usr/bin/env python3
"""Verifica non distruttiva di disponibilità API e GeoTIFF radar DPC.

Usa Origin legittima della webapp, senza eludere controlli dell'API.
Non scrive in repository e non registra URL presigned.
"""
import json,urllib.request,urllib.error
from datetime import datetime, timezone

BASE="https://radar-api.protezionecivile.it"
ORIGIN="https://matteotidili.github.io"
for prod in ("SRI","VMI"):
  url=f"{BASE}/findLastProductByType?type={prod}&lang=it"
  try:
    req=urllib.request.Request(url,headers={"User-Agent":"SardegnaMeteoLive/1.0","Origin":ORIGIN})
    with urllib.request.urlopen(req,timeout=25) as r:
      x=json.load(r)
      print(prod,"GET",r.status,"CORS",r.headers.get("Access-Control-Allow-Origin"),"metadata",x)
    entries=x.get("lastProducts") or []
    if not entries: continue
    ms=entries[0]["time"]
    payload=json.dumps({"productType":prod,"productDate":ms}).encode()
    req=urllib.request.Request(f"{BASE}/downloadProduct",data=payload,headers={
      "User-Agent":"SardegnaMeteoLive/1.0","Origin":ORIGIN,"Content-Type":"application/json"
    },method="POST")
    with urllib.request.urlopen(req,timeout=30) as r:
      result=json.load(r)
      print(prod,"POST",r.status,"CORS",r.headers.get("Access-Control-Allow-Origin"),
            "key",result.get("key"),"expiresSeconds",result.get("expiresSeconds"),
            "has_url",bool(result.get("url")),"error",result.get("error"))
    signed=result.get("url")
    if not signed: continue
    req=urllib.request.Request(signed,headers={"User-Agent":"SardegnaMeteoLive/1.0","Range":"bytes=0-127"})
    with urllib.request.urlopen(req,timeout=30) as r:
      blob=r.read(256)
      print(prod,"TIFF GET",r.status,"signature",blob[:4].hex(),"bytes",len(blob),"size",r.headers.get("Content-Range") or r.headers.get("Content-Length"),"content-type",r.headers.get("Content-Type"),"CORS",r.headers.get("Access-Control-Allow-Origin"))
  except urllib.error.HTTPError as e:
    print(prod,"HTTP_ERROR",e.code,e.read(260).decode("utf8","replace"))
  except Exception as e: print(prod,"ERROR",type(e).__name__,str(e)[:220])
