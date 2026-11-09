#!/usr/bin/env python3
"""Prova non distruttiva di una sola estrazione MeteoHub contenente DPCN + MNW."""
import collections
import gzip
import io
import json
import sys
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone
from update_meteohub import api, login, get_id, wait_for_request

NOW = datetime.now(timezone.utc)

def inspect_payload(name, blob):
    if blob.startswith(b"\x1f\x8b"):
        blob = gzip.decompress(blob)
    for line in blob.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, list):
            for obj in record:
                if isinstance(obj, dict):
                    yield obj
        elif isinstance(record, dict):
            yield record

def main():
    token = login()
    hourly = api("GET", "/api/hourly", token)
    if isinstance(hourly, dict) and hourly.get("remaining") is not None:
        remaining = int(hourly["remaining"])
        print("Quota MeteoHub residua:", remaining)
        if remaining < 2:
            raise RuntimeError("Quota oraria insufficiente per prova combinata; niente interrogazione")
    start = (NOW - timedelta(hours=3)).isoformat(timespec="seconds").replace("+00:00","Z")
    end = NOW.isoformat(timespec="seconds").replace("+00:00","Z")
    request = {
        "request_name": "sardegna-meteolive-probe-dpcn-mnw",
        "reftime": {"from":start, "to":end},
        "dataset_names": ["dpcn-sardegna","mnw"],
        "filters": {"product": [
            {"code":c,"active":True}
            for c in ["B11001","B11002","B11041","B12101","B13003","B13011"]
        ]},
        "only_reliable":True,
        "output_format":"json"
    }
    request_id=None
    try:
        response=api("POST","/api/data",token,request)
        request_id=get_id(response)
        if request_id is None:
            raise RuntimeError("MeteoHub: request_id mancante")
        filename=wait_for_request(token,request_id)
        raw=api("GET","/api/data/"+urllib.parse.quote(str(filename),safe=""),token,timeout=240,parse_json=False)
        if not isinstance(raw,(bytes,bytearray)):
            raw=json.dumps(raw).encode("utf-8")
        if bytes(raw).startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                blobs=[(n,z.read(n)) for n in z.namelist() if n.lower().endswith((".json",".jsonl",".txt",".gz"))]
                print("File in archivio:",[n for n,_ in blobs])
        else:
            blobs=[("single",raw)]
        counts=collections.Counter()
        latest={}
        sample={}
        for name,blob in blobs:
            for obj in inspect_payload(name,blob):
                network=str(obj.get("network") or "<assente>")
                counts[network]+=1
                sample.setdefault(network,{"keys":list(obj.keys()),"metadata_vars":list(((obj.get("data") or [{}])[0].get("vars") or {}).keys())[:10]})
                stamp=str(obj.get("date") or "")
                if stamp and stamp>latest.get(network,""):
                    latest[network]=stamp
        print("Reti nella singola estrazione:",dict(counts))
        print("Ultime misure per rete:",latest)
        print("Struttura minima:",sample)
        if not any("dpcn" in n.lower() for n in counts) or not any("mnw" in n.lower() or "meteonetwork" in n.lower() for n in counts):
            raise RuntimeError("Estrazione combinata non contiene ENTRAMBE le reti: non modificare produzione")
    finally:
        if request_id is not None:
            try:
                api("DELETE","/api/requests/"+urllib.parse.quote(str(request_id),safe=""),token)
            except Exception as exc:
                print("Avviso: impossibile eliminare la richiesta temporanea:",type(exc).__name__,file=sys.stderr)

if __name__=="__main__":
    main()
