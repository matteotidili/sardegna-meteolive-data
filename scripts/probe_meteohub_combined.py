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
from update_meteohub import api, login, get_id, wait_for_request, aggregate as aggregate_dpcn, SARDINIA_BOUNDING_BOX
from update_meteonetwork_hub import aggregate as aggregate_mnw

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
        # Sondaggio COMPLETO: nessun filtro su variabili, livelli o periodi.
        # Mantiene il ritaglio Sardegna per rispettare quote e dimensioni.
        "filters": {"bounding_box": SARDINIA_BOUNDING_BOX.copy()},
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
        observed_codes=collections.defaultdict(collections.Counter)
        temperature_extremes=collections.defaultdict(list)
        recent_stations=collections.defaultdict(set)
        for name,blob in blobs:
            for obj in inspect_payload(name,blob):
                network=str(obj.get("network") or "<assente>")
                counts[network]+=1
                sample.setdefault(network,{"keys":list(obj.keys()),"metadata_vars":list(((obj.get("data") or [{}])[0].get("vars") or {}).keys())[:10]})
                station_id=obj.get("ident")
                if station_id is not None:
                    recent_stations[network].add(str(station_id))
                for entry in (obj.get("data") or [])[1:]:
                    if not isinstance(entry,dict):
                        continue
                    for code,value in (entry.get("vars") or {}).items():
                        observed_codes[network][code]+=1
                        if code in {"B12011","B12012","B12021","B12022","B12111","B12112","B12116","B12117","B12118","B12119"}:
                            if len(temperature_extremes[network])<20:
                                temperature_extremes[network].append({
                                    "code":code,
                                    "v": value.get("v") if isinstance(value,dict) else value,
                                    "date":obj.get("date"),
                                    "timerange":entry.get("timerange"),
                                    "level":entry.get("level")
                                })
                stamp=str(obj.get("date") or "")
                if stamp and stamp>latest.get(network,""):
                    latest[network]=stamp
        print("Reti nella singola estrazione:",dict(counts))
        print("Ultime misure per rete:",latest)
        print("Struttura minima:",sample)
        print("Stazioni uniche per rete:",{k:len(v) for k,v in recent_stations.items()})
        print("Codici BUFR presenti per rete:",{k:dict(v.most_common()) for k,v in observed_codes.items()})
        print("Estremi originali BUFR (solo esempi non nominativi):",dict(temperature_extremes))
        if not any("dpcn" in n.lower() for n in counts) or not any("mnw" in n.lower() or "meteonetwork" in n.lower() for n in counts):
            raise RuntimeError("Estrazione combinata non contiene ENTRAMBE le reti: non modificare produzione")
        dpcn = aggregate_dpcn(bytes(raw))
        mnw = aggregate_mnw(bytes(raw))
        print("Parser DPCN:",dpcn["station_count"],"stazioni")
        print("Parser MNW:",mnw["station_count"],"stazioni")
        print("Esempio codice MNW:",mnw["stations"][0]["station_id"])
        assert dpcn["station_count"] >= 70, "DPCN incompleta"
        assert 8 <= mnw["station_count"] <= 50, "MeteoNetwork: conteggio anomalo (dataset mescolati?)"
    finally:
        if request_id is not None:
            try:
                api("DELETE","/api/requests/"+urllib.parse.quote(str(request_id),safe=""),token)
            except Exception as exc:
                print("Avviso: impossibile eliminare la richiesta temporanea:",type(exc).__name__,file=sys.stderr)

if __name__=="__main__":
    main()
