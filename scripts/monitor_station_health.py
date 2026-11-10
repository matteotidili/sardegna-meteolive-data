#!/usr/bin/env python3
"""Monitor automatico: verifica i timestamp reali di DPCN, AM, Ecowitt, CEDOC;
chiede il recupero dei collector inattivi, senza ripubblicare dati vecchi.
Weather Underground rimane disattivata."""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TITLE = "[MeteoLive] Rete stazioni non aggiornata"
# WU temporaneamente spenta su richiesta: NON e' un guasto da riparare
# e NON deve tenere aperta una segnalazione quando DPCN e AM sono sane.
WU_ACTIVE = False
SOURCES = {
    "DPCN": ("data/stations.json", 30, "temp"),
    "Aeronautica Militare": ("data/am.json", 30, "temp"),
    "Ecowitt": ("data/ecowitt.json", 25, "temp"),
    # Le misure CEDOC sono pluviometriche: controllare la pioggia,
    # non la temperatura che in questo dataset non e' prevista.
    "CEDOC": ("data/cedoc.json", 180, "rain_today"),
    "Weather Underground": ("data/wunderground.json", 95, "temp"),
}

def parse_time(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def evaluate(now):
    health = read("data/wunderground_app_api_health.json")
    report = {}
    for name, (path, limit, measure) in SOURCES.items():
        data = read(path)
        rows = data.get("stations") or []
        usable = [
            parse_time(row.get("last"))
            for row in rows if isinstance(row, dict)
            and isinstance(row.get(measure), (int, float))
            and (name != "CEDOC" or data.get("status") == "ok")
        ]
        usable = [dt for dt in usable if dt and dt <= now + timedelta(minutes=5)]
        latest = max(usable, default=None)
        count = sum((now - dt).total_seconds() <= 60*limit for dt in usable)
        info = {
            "status": "ok" if count else "stale",
            "stations": len(rows), "fresh": count, "limit_min": limit,
            "latest": latest.isoformat().replace("+00:00", "Z") if latest else None,
        }
        if name in ("DPCN", "Aeronautica Militare"):
            checked = parse_time(data.get("checked_at"))
            age = (now-checked).total_seconds()/60 if checked and checked <= now else None
            info["checked_at"] = data.get("checked_at")
            info["acquisition_age_min"] = age
            # Recupero preventivo prima del limite richiesto di 30 minuti.
            info["recovery_due"] = age is None or age >= 20
            if age is None or age > 30:
                info["status"] = "acquisition_stale"
            elif not count:
                info["status"] = "source_delayed"
        if name == "Weather Underground" and not WU_ACTIVE:
            info["status"] = "paused"
            info["fresh"] = 0
        elif name == "Weather Underground" and health.get("status") == "cooldown":
            info["status"] = "api_throttled"
            info["blocked_until"] = health.get("blocked_until")
            info["reason"] = str(health.get("last_error") or "API throttled")[:100]
        report[name] = info
    return report

def github(method, path, payload=None):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ["GITHUB_REPOSITORY"]
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.github.com/repos/"+repo+path, method=method, data=data,
        headers={"Accept":"application/vnd.github+json",
                 "Authorization":"Bearer "+token,
                 "Content-Type":"application/json",
                 "X-GitHub-Api-Version":"2022-11-28",
                 "User-Agent":"MeteoLive-health-monitor"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            b=r.read()
            return json.loads(b) if b else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError("GitHub "+method+" "+path+" HTTP "+str(e.code)) from None

def description(report, now):
    lines = [
        "Controllo automatico Sardegna MeteoLive. Nessun dato e' simulato o cancellato.",
        "", "Controllo UTC: "+now.isoformat(timespec="minutes"), "",
        "| Rete | Stato | Ultima osservazione (UTC) | Stazioni con misura recente |",
        "|---|---|---|---:|",
    ]
    for name, v in report.items():
        lines.append("| "+name+" | "+v["status"]+" | "+str(v["latest"] or "—")
                     +" | "+str(v["fresh"])+"/"+str(v["stations"])+" |")
    wu=report["Weather Underground"]
    if wu["status"]=="paused":
        lines += ["", "WU sospesa volontariamente. Nessuna interrogazione API o recupero automatico."]
    if wu["status"]=="api_throttled":
        lines += ["", "WU: limitazione applicata dal fornitore ("+
                  wu.get("reason","quota")+"). Prossima prova non prima di "+
                  str(wu.get("blocked_until"))+
                  ". Questo monitor NON forza interrogazioni e NON modifica la chiave."]
    lines += ["", "Il buon esito di una GitHub Action non garantisce la consegna di nuove osservazioni."]
    return "\n".join(lines)

def sync_issue(report, now):
    rows=github("GET","/issues?state=all&per_page=100")
    issue=next((x for x in rows if x.get("title")==TITLE and "pull_request" not in x),None)
    degraded=any(v["status"] not in ("ok","paused") for v in report.values())
    if not degraded:
        if issue and issue.get("state")=="open":
            github("PATCH","/issues/"+str(issue["number"]),
                   {"state":"closed","state_reason":"completed",
                    "body":description(report,now)+"\n\nReti di nuovo aggiornate."})
        return
    body=description(report,now)
    if issue:
        if issue["state"]=="closed":
            github("PATCH","/issues/"+str(issue["number"]),{"state":"open","body":body})
        else:
            # Evita modifiche e notifiche ogni 15 minuti se la diagnosi
            # e' invariata. Aggiorna solo sui cambi di stato/blocco.
            previous=issue.get("body") or ""
            same_states=all(
                "| "+name+" | "+value["status"]+" |" in previous
                for name,value in report.items()
            )
            same_block=(report["Weather Underground"].get("blocked_until") or "") in previous
            if not same_states or not same_block:
                github("PATCH","/issues/"+str(issue["number"]),{"body":body})
    else:
        created=github("POST","/issues",{"title":TITLE,"body":body})
        print("MONITOR: segnalazione aperta",created.get("html_url"),flush=True)

def self_repair(report, now):
    # Un collettore non disponibile non deve bloccare le altre reti:
    # recuperi indipendenti, senza riattivare Weather Underground.
    targets = [
        (("DPCN", "Aeronautica Militare"), "update-observations.yml"),
        (("Ecowitt",), "update-ecowitt.yml"),
        (("CEDOC",), "update-cedoc.yml"),
    ]
    for networks, workflow in targets:
        stale = [n for n in networks if report[n].get("recovery_due",
                  report[n]["status"] == "stale")]
        if not stale:
            continue
        runs=github("GET","/actions/workflows/"+workflow+"/runs?per_page=12").get("workflow_runs",[])
        if any(t and (now-t).total_seconds()<20*60 for t in
               [parse_time(x.get("created_at")) for x in runs]):
            print("MONITOR: tentativo recente per "+workflow+", nessun doppio avvio",flush=True)
            continue
        github("POST","/actions/workflows/"+workflow+"/dispatches",{"ref":"main"})
        print("MONITOR: ripristino automatico richiesto per",", ".join(stale),flush=True)

def main():
    now=datetime.now(timezone.utc)
    report=evaluate(now)
    for name,v in report.items():
        print("MONITOR",name,v["status"],str(v["fresh"])+"/"+str(v["stations"]),
              v["latest"],flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a",encoding="utf-8") as f:
            f.write(description(report,now)+"\n")
    if not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        return
    # Su richiesta dell'utente, nessuna issue automatica: creazioni e cambi
    # di stato possono generare email GitHub. Diagnosi e orari restano nel
    # log e nel riepilogo Actions; il recupero automatico rimane attivo.
    self_repair(report,now)  # un'automazione rotta deve risultare fallita

if __name__=="__main__":
    main()

