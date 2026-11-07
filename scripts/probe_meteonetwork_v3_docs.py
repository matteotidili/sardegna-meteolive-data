#!/usr/bin/env python3
"""Prova senza credenziali: leggi solo la documentazione Swagger MeteoNetwork v3."""
import io, json, urllib.request
from urllib.error import HTTPError
import yaml
url="https://api.meteonetwork.it/swagger.yaml"
req=urllib.request.Request(url,headers={"User-Agent":"SardegnaMeteoLive/1.0 (technical integration research)","Accept":"text/yaml,application/yaml,*/*"})
try:
    with urllib.request.urlopen(req,timeout=22) as resp: raw=resp.read()
    data=yaml.safe_load(raw)
    print("SWAGGER",str(data.get("info",{}))[:400])
    for path,methods in (data.get("paths") or {}).items():
        if not any(term in path for term in ("station","data","login","network","observation")):continue
        for method,definition in methods.items():
            if not isinstance(definition,dict) or method not in ("get","post"):continue
            fields=[p.get("name") for p in definition.get("parameters",[]) if isinstance(p,dict)]
            body=definition.get("requestBody") or {}
            print("ENDPOINT",method.upper(),path,"summary",str(definition.get("summary",""))[:160],
                "tags",definition.get("tags"),"params",fields,"security",definition.get("security"),
                "body",str(body)[:550])
    print("SECURITY",json.dumps(data.get("securityDefinitions") or data.get("components",{}).get("securitySchemes") or {}))
    comp=data.get("components") or {}
    schemas=comp.get("schemas") or {}
    for name,val in schemas.items():
        if any(t in name.lower() for t in ("credential","station","realtime","daily","data","measure","token","login")):
            print("SCHEMA",name,json.dumps(val,ensure_ascii=False)[:2400])
    for target in ["/login","/stations","/data-realtime","/data-daily"]:
        d=(data.get("paths") or {}).get(target) or {}
        for method,spec in d.items():
            if method not in ("get","post"):continue
            print("DETAIL",method.upper(),target,json.dumps(spec,ensure_ascii=False)[:5500])
except HTTPError as exc:
    print("Swagger fetch HTTP",exc.code,"contenttype",exc.headers.get("Content-Type"))
except Exception as exc:
    print("Swagger fetch error",type(exc).__name__)
