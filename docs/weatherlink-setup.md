# Collegamento WeatherLink (Davis) a Sardegna MeteoLive

La mappa pubblica WeatherLink non è un endpoint API aperto: **essere visibile
sulla mappa non concede il diritto di leggere i dati di tutte le stazioni**.

## Autorizzazioni necessarie

- Una chiave `WeatherLink v2 API Key` e il relativo `API Secret`, generati
  dall'account WeatherLink in https://www.weatherlink.com/account.
- Permessi di lettura effettivi per ciascuna stazione. Secondo Davis sono
  autorizzate le stazioni proprie, quelle condivise con l'account (la condivisione
  Basic deve essere fatta dal proprietario), e quelle pubbliche a cui è stato
  applicato un accesso Pro o Pro+ appropriato.
- Il collector evita di pubblicare stazioni marcate come `private`, anche se
  visibili nel proprio account. Non copia dati di proprietari senza il necessario
  permesso.

## Attivazione

1. Aprire il repository `matteotidili/sardegna-meteolive-data`.
2. `Settings > Secrets and variables > Actions > New repository secret`.
3. Aggiungere i due secret `WEATHERLINK_API_KEY` e `WEATHERLINK_API_SECRET`.
   **Non inserirli nel codice, nei commit o nel frontend pubblico.**
4. In `Actions`, avviare manualmente `Aggiorna rete WeatherLink (accesso autorizzato)`.
5. Verificare `data/weatherlink.json`: le stazioni accessibili della Sardegna
   compaiono automaticamente nel filtro WL della webapp. Il processo è schedulato
   ai minuti 07 e 37 di ogni ora.

## Logica della rete

- Considera soltanto la Sardegna e le isole circostanti.
- Usa le letture attuali ISS con timestamp valido, non oltre 90 minuti.
- Esclude possibili duplicati in altre reti *solo* se hanno esattamente lo
  stesso nome normalizzato e coordinate entro 40 metri. Stazioni differenti
  anche vicine non sono unificate automaticamente.
- Converte la temperatura da Fahrenheit a Celsius e il vento da mph a km/h.
- Il dato WeatherLink `/current` non fornisce uniformemente estremi giornalieri
  affidabili: `tmin`, `tmax` e raffica giornaliera sono lasciati nulli, per
  evitare classifiche con valori di intervalli temporali differenti.
- Per Tmin/Tmax reali della rete WL occorrerà accesso allo storico e una
  aggregazione per giorno locale `Europe/Rome`, non una stima dal singolo
  campione istantaneo.
- `data/weatherlink.json` resta inizialmente vuoto e il filtro WL non compare
  finché non arrivano osservazioni realmente autorizzate.

Documentazione Davis: https://weatherlink.github.io/v2-api/
