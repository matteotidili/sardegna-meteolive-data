# UniCA radar X-band — integrazione georeferenziata

Sardegna MeteoLive supporta un layer **Leaflet L.imageOverlay** (non un iframe).
Il pulsante **Radar UniCA · Cagliari** resta volutamente disabilitato fino alla
disponibilità di immagini di precipitazione georeferenziate e trasparenti.

## Perché non basta il link PHP

La pagina pubblica https://meteoradar.unica.it/R_xB_DP_Dop_Unica.php
contiene un visualizzatore immagine/video. Non è stato possibile verificare
un endpoint WMS, WMTS, XYZ, GeoTIFF oppure PNG trasparente georeferenziato
con un bbox scientificamente affidabile. Sovrapporre il video o il PNG
con un'estensione supposta sposterebbe gli echi rispetto alla topografia.

## Manifest pronto

`data/unica_radar.json` contiene:
- `enabled` boolean: abilitazione solo dopo validazione della sorgente;
- `image_url`: URL HTTPS di PNG/WebP trasparente, georeferenziato,
  con esatta estensione geografica e senza cartografia di sfondo;
- `bounds`: `[[south,west],[north,east]]` in gradi WGS84 (EPSG:4326).
  Per immagini in altra proiezione occorre prima riproiettarle;
- `transparent`: deve essere `true`, verificato su raster prodotto;
- `observed_at`: timestamp di osservazione UTC ISO-8601;
- `updated_at`: timestamp di pubblicazione UTC ISO-8601;
- `product`: tipo di variabile (es. VMI).
  
Il frontend accetta l'immagine soltanto se fresca (non oltre 45 minuti),
geograficamente coerente con il settore sardo e caricabile; mantiene
disabilitato il pulsante in assenza di dati verificati.

Per implementare il collector occorre ottenere da UniCA/DICAAR **autorizzazione
alla redistribuzione**, un feed del prodotto radar senza basemap e le
specifiche geometriche (CRS, bbox, dimensioni pixel, prodotto e orario).
L'accesso pubblico al sito non implica l'esistenza o la licenza di un feed
raster riutilizzabile.

L'integrazione è predisposta nella pagina:
`matteotidili/sardegna-meteolive-web/index.html`.
