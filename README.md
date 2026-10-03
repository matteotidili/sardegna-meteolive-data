# Sardegna MeteoLive Data

Repository pubblico dei dati osservativi usati da **Sardegna MeteoLive**.

Contiene esclusivamente dati meteorologici pubblici elaborati dalla rete **MeteoHub / DPCN Sardegna**. Le credenziali di accesso a MeteoHub non sono presenti nel codice e devono essere salvate esclusivamente nei **GitHub Actions Secrets** del repository.

File principale:

- `data/stations.json` — osservazioni aggregate per stazione.

Il workflow `Aggiorna dati MeteoHub` è predisposto per l'aggiornamento automatico circa ogni 30 minuti.

Il sito pubblico legge questo repository come sorgente dati, così gli aggiornamenti meteorologici non generano nuovi deploy Netlify.
