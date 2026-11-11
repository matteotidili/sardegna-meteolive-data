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

I nomi delle stazioni MNW usano comune e località riportati nel
[portale ufficiale MeteoNetwork](https://www.meteonetwork.eu/it/stations-list),
verificati anche nelle schede della LiveMap. L'associazione tra codice e nome è
in `config/meteonetwork_places.json`: si applica a ogni acquisizione e anche alle
osservazioni precedenti conservate. `station_code` e `station_id` mantengono
l'identità della stazione; misure e orari non cambiano quando cambia il nome.
I nuovi codici senza associazione restano visibili con il codice originale.
Le due stazioni `srd175` e `srd176` risultano entrambe a Oristano nelle schede
ufficiali e mantengono identificativi distinti.

## Notifiche e dimensioni delle estrazioni MeteoHub

Prima di avviare estrazioni, il login disattiva e verifica nel profilo MeteoHub
`notify_on_successful_request`. Se non riesce a confermare la disattivazione,
non avvia nuove estrazioni. La preferenza riguarda l'account MeteoHub.
La [guida ufficiale](https://meteohub.agenziaitaliameteo.it/ui/user-guide)
specifica che le email di errore vengono comunque inviate: per escludere anche
quelle dalla casella serve un filtro sul mittente
`noreply@meteohub.hpc.cineca.it`, oppure sospendere le estrazioni.
Il monitor conserva autoripristino e riepiloghi Actions senza creare o aggiornare
issue GitHub automaticamente. Le notifiche native GitHub dipendono dalle
preferenze dell'account e del repository.

Le estrazioni combinate DPCN/MNW, quelle MNW manuali e la prova combinata usano
`filters.bounding_box` per limitare i dati alla Sardegna già sul server
(latitudine 38.75–41.4, longitudine 7.7–10.3), secondo lo
[schema API ufficiale](https://meteohub.agenziaitaliameteo.it/api/specs).
Non viene più scaricata l'intera rete MNW nazionale. La finestra osservativa
giornaliera e i controlli di quota del servizio restano attivi.


## Previsioni AROME HD e animazione del vento

`update-arome-hd.yml` genera i raster e i campi U/V a 10 m della **stessa corsa**
prima di pubblicarli insieme in un unico commit. Sono mantenuti i 51 passi orari,
i 21 prodotti e il dominio Mediterraneo occidentale. Se un campo manca o non
supera i controlli, la previsione precedente resta pubblicata per intero.
`scripts/arome_bundle.py` verifica corse, validità dei frame, geometria e binari;
a ogni tentativo di pubblicazione esclude candidate superate da una corsa remota
più recente e ripete la validazione dopo il rebase.

Il workflow `update-arome-wind-vectors.yml` rimane un ripristino manuale o su
modifiche al proprio codice: condivide il lock con AROME HD e può pubblicare
vettori soltanto per la corsa raster ancora presente su `main`. Il normale
aggiornamento non dipende da un secondo cron né dal trigger `push` di un altro
workflow. Per il recupero immediato si può avviare il ripristino vettori senza
rigenerare i raster. AROME HD riusa comunque i raster della corsa corrente quando
sono già completi e ricostruisce i vettori mancanti o non coerenti.

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
