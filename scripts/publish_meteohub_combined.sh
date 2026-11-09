#!/usr/bin/env bash
# Pubblica atomicamente i due file ricavati dalla stessa estrazione MeteoHub.
# Non tocca Ecowitt, AM o altre reti.
set -euo pipefail

FILES=(data/stations.json data/meteonetwork.json)
SAVE_DIR="$RUNNER_TEMP/meteohub-combined-candidates"
mkdir -p "$SAVE_DIR"
for file in "${FILES[@]}"; do
  test -s "$file" || { echo "Manca il dataset $file"; exit 1; }
  cp "$file" "$SAVE_DIR/$(basename "$file")"
done

git config user.name "sardegna-meteolive-data-bot"
git config user.email "actions@users.noreply.github.com"

for attempt in 1 2 3 4 5; do
  git fetch origin main
  git reset --hard origin/main
  python - "$SAVE_DIR" <<'PY'
import json,sys
from datetime import datetime,timezone
from pathlib import Path

saved=Path(sys.argv[1])
files=[(Path("data/stations.json"), saved/"stations.json", 60),
       (Path("data/meteonetwork.json"), saved/"meteonetwork.json", 5)]

def time_of(value):
    try:
        stamp=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)

for target,source,min_stations in files:
    new=json.loads(source.read_text(encoding="utf-8"))
    old=json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    if not isinstance(new.get("stations"),list) or len(new["stations"]) < min_stations:
        raise RuntimeError(f"File {target}: meno di {min_stations} stazioni, pubblicazione annullata")
    incoming=(time_of(new.get("generated_at")),time_of(new.get("checked_at")))
    existing=(time_of(old.get("generated_at")),time_of(old.get("checked_at")))
    if incoming>existing:
        target.write_bytes(source.read_bytes())
        print(target,": osservazione",new.get("generated_at"),"acquisizione",new.get("checked_at"))
    else:
        print(target,": dataset uguale o più recente già pubblicato")
PY
  git add "${FILES[@]}"
  if git diff --cached --quiet; then
    echo "Nessuna nuova osservazione da pubblicare"
    exit 0
  fi
  git commit -m "Aggiorna DPCN e MNW con la stessa estrazione MeteoHub"
  if git push origin HEAD:main; then
    echo "Dataset MeteoHub DPCN e MNW pubblicati"
    exit 0
  fi
  echo "Push concorrente, tentativo $attempt/5" >&2
  sleep "$attempt"
done
echo "ERRORE: impossibile pubblicare dataset MeteoHub combinati" >&2
exit 1
