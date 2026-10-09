#!/usr/bin/env python3
"""Diagnostica sorgente ufficiale GRIB2 AROME HD 0,01 gradi Météo-France."""
import datetime as dt
import io
import urllib.error
import urllib.request

BASE="https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net"
now=dt.datetime.now(dt.timezone.utc)
for cycle_back in (3, 6, 9, 12):
    nominal=now-dt.timedelta(hours=cycle_back)
    run=nominal.replace(hour=(nominal.hour//3)*3, minute=0, second=0, microsecond=0)
    stamp=run.isoformat(timespec="seconds").replace("+00:00","Z")
    print("CYCLE",stamp,flush=True)
    for lead in [1,6,12]:
        url=f"{BASE}/pnt/{stamp}/arome/001/SP2/arome__001__SP2__{lead:02d}H__{stamp}.grib2"
        try:
            req=urllib.request.Request(url,method="HEAD",headers={"User-Agent":"SardegnaMeteoLive/0.1"})
            with urllib.request.urlopen(req,timeout=20) as r:
                print("HEAD",lead,r.status,"bytes",r.headers.get("Content-Length"),flush=True)
                if lead==6 and cycle_back==6:
                    print("GET_SOURCE",url,flush=True)
                    with urllib.request.urlopen(url,timeout=100) as f:
                        blob=f.read(60_000_000)
                    print("GRIB_BYTES",len(blob),"header",repr(blob[:12]),flush=True)
                    try:
                        from eccodes import codes_grib_new_from_file,codes_get,codes_get_values,codes_release
                        f=io.BytesIO(blob)
                        while (gid:=codes_grib_new_from_file(f)) is not None:
                            try:
                                print("FIELD", {k:codes_get(gid,k) for k in ("shortName","name","units","stepType","startStep","endStep","gridType","Ni","Nj","longitudeOfFirstGridPointInDegrees","latitudeOfFirstGridPointInDegrees","iScansNegatively","jScansPositively","jPointsAreConsecutive")},flush=True)
                            finally:codes_release(gid)
                    except Exception as exc:print("GRIB_DECODER_ERROR",repr(exc),flush=True)
        except urllib.error.HTTPError as e:print("HEAD",lead,"HTTP",e.code,flush=True)
        except Exception as exc:print("HEAD_ERROR",lead,repr(exc),flush=True)
