#!/usr/bin/env python3
"""Prova unica dell'estrazione MeteoNetwork tramite il login MeteoHub esistente.

Non modifica la rete DPCN, non memorizza dati privati, cancella la richiesta
temporanea e mostra soltanto conteggi anonimi dei record e i metadati.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone

import update_meteohub as hub


def main():
    token = hub.login()
    body = hub.request_body()
    body["request_name"] = "sardegna-meteolive-meteonetwork-verifica"
    body["dataset_names"] = ["meteonetwork"]
    now = datetime.now(timezone.utc)
    body["reftime"] = {
        "from": (now - timedelta(minutes=75)).isoformat(timespec="seconds").replace("+00:00","Z"),
        "to": now.isoformat(timespec="seconds").replace("+00:00","Z"),
    }
    # Una prima richiesta limitata temporalmente; solo un thread.
    rid = None
    try:
        response = hub.api("POST", "/api/data", token, body)
        rid = hub.get_id(response)
        if rid is None:
            raise RuntimeError("MeteoHub: richiesta non accettata")
        filename = hub.wait_for_request(token, rid)
        raw = hub.api("GET","/api/data/"+urllib.parse.quote(str(filename),safe=""),
                      token,timeout=240,parse_json=False)
        if not isinstance(raw, (bytes,bytearray)):
            raw=json.dumps(raw).encode("utf-8")
        content = hub.unpack(bytes(raw)).decode("utf-8", errors="replace")
        network_counts = Counter()
        sample = {}
        n = 0
        for line in content.splitlines():
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            n += 1
            network = str(record.get("network") or "")
            network_counts[network] += 1
            if network not in sample:
                data=record.get("data") or []
                sample[network] = {
                    "record_keys":sorted(record.keys()),
                    "top_meta_keys":sorted((data[0].get("vars") if data and isinstance(data[0],dict) else {}) or {})[:40],
                    "element_keys":sorted((data[1].get("vars") if len(data)>1 and isinstance(data[1],dict) else {}) or {})[:40],
                }
        print("MeteoNetwork verifica: record",n,"network_counts",dict(network_counts))
        print("Meta shape (no personal data):",json.dumps(sample))
        if n == 0:
            raise RuntimeError("La richiesta MeteoNetwork non ha restituito osservazioni.")
    finally:
        if rid is not None:
            try:
                hub.api("DELETE",f"/api/requests/{urllib.parse.quote(str(rid),safe='')}",token)
            except Exception:
                print("Avviso: eliminazione richiesta test fallita", file=sys.stderr)


if __name__ == "__main__":
    main()
