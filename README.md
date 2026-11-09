# Sardegna MeteoLive Data

Dati pubblici MeteoHub / DPCN Sardegna e MeteoNetwork (MNW), Aeronautica Militare, Ecowitt e altre reti configurate. Weather Underground resta temporaneamente disattivata.
Le credenziali sono conservate esclusivamente nei GitHub Actions Secrets.

Il workflow di acquisizione generale è programmato ogni 10 minuti (pianificazione GitHub best-effort); il watchdog esterno Cloudflare verifica le acquisizioni ogni 5 minuti.
DPCN Sardegna e MNW sono raccolte **insieme, con un'unica estrazione MeteoHub**,
ma pubblicate nei due JSON indipendenti `stations.json` e `meteonetwork.json`.
Aeronautica Militare ed Ecowitt mantengono acquisizioni indipendenti: un servizio
lento o non disponibile non blocca gli altri. I job della stessa rete non si sovrappongono,
anche quando avviati manualmente. Gli updater applicano un margine di 4 minuti
tra i controlli per non saltare il ciclo successivo a causa della durata del job.

GitHub Actions può ritardare gli avvii: questa pianificazione non garantisce
osservazioni nuove ogni 5 minuti. La frequenza delle misure dipende dalle fonti.
I limiti e gli errori di autenticazione WU continuano a interrompere la raccolta,
preservando il dataset precedente. Non sono previste richieste con nuove chiavi
o altri tentativi di aggirare le quote del servizio.

- `data/stations.json`: MeteoHub / DPCN
- `data/meteonetwork.json`: MeteoHub / MeteoNetwork MNW (stessa estrazione)
- `data/wunderground.json`: PWS Weather Underground
- `data/am.json`: SYNOP / METAR Aeronautica Militare

`last` indica l'ora reale dell'osservazione. Il sito legge questi file separatamente
dal proprio deploy e mantiene il colore dell'ultimo valore disponibile.


## Incendi attivi MTG FCI / EUMETSAT

Il workflow `Aggiorna incendi MTG FRP` interroga il servizio WFS EUMETView
del prodotto `EO:EUM:DAT:1156` (layer `mtg_fd:frp`) e pubblica
`data/frp.json`.

Il collector:
- limita le richieste alla Sardegna tramite i campi espliciti `Lat` e `Lon`;
- interroga i frame a passi di 10 minuti su una finestra recente;
- gestisce automaticamente il limite di feature del WFS suddividendo l'area
  solo quando un frame risulta troncato;
- deduplica i pixel tramite l'identificativo WFS;
- conserva FRP, FRPerr, Confidence, temperature di brillanza e timestamp.

L'access token EUMETSAT non deve essere pubblicato nei file del repository.
Va configurato come GitHub Actions Secret con nome
`EUMETSAT_ACCESS_TOKEN`.
