#!/usr/bin/env python3
import datetime as dt,urllib.request,tempfile,os
from eccodes import codes_grib_new_from_file,codes_get,codes_get_values,codes_release
import numpy as np
now=dt.datetime.now(dt.timezone.utc).replace(minute=0,second=0,microsecond=0)
now=now.replace(hour=(now.hour//3)*3)-dt.timedelta(hours=6)
stamp=now.strftime("%Y-%m-%dT%H:%M:%SZ")
for pack in ("SP1","SP2","SP3"):
 url=f"https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/{stamp}/arome/001/{pack}/arome__001__{pack}__06H__{stamp}.grib2"
 try:
  with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"SardegnaMeteoLive diagnostic/1.0"}),timeout=80) as resp: blob=resp.read(60_000_000)
  print("PACK",pack,"bytes",len(blob),flush=True)
  with tempfile.TemporaryFile(mode="w+b") as handle:
   handle.write(blob);handle.seek(0)
   i=0
   while (gid:=codes_grib_new_from_file(handle)) is not None:
    try:
     meta={}
     for key in ("shortName","name","units","stepType","startStep","endStep","typeOfLevel","level","discipline","parameterCategory","parameterNumber","productDefinitionTemplateNumber","percentileValue","typeOfStatisticalProcessing"):
      try:meta[key]=codes_get(gid,key)
      except Exception:pass
     i+=1
     if (pack=="SP2" and meta.get("parameterCategory")==16) or pack=="SP3":
      v=np.array(codes_get_values(gid))
      v=v[np.isfinite(v) & (v<9998)]
      meta["stats"]={"count":int(v.size),"min":float(v.min()),"p10":float(np.quantile(v,.1)),"p50":float(np.quantile(v,.5)),"p90":float(np.quantile(v,.9)),"max":float(v.max())}
     print("PARAM",pack,i,meta,flush=True)
    finally:codes_release(gid)
 except Exception as e:print("PACKERR",pack,str(e),flush=True)
