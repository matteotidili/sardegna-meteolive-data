# Sardegna MeteoLive — sorveglianza esterna gratuita

Questo Cloudflare Worker affianca i workflow GitHub Actions di `matteotidili/sardegna-meteolive-data`. Non modifica i dati, i collector o la webapp e non interroga direttamente le API meteorologiche.

## Funzionamento

- Cloudflare esegue il controllo ogni 5 minuti, indipendentemente da GitHub Actions.
- Per MeteoHub/DPCN (`data/stations.json`) e Aeronautica Militare (`data/am.json`) legge `checked_at`; per Ecowitt Villaspeciosa (`data/ecowitt.json`) legge `checked_at` (o `generated_at` sui vecchi dataset) come orario dell'ultimo controllo e `stations[].last` come orario dell'ultima osservazione reale.
- Se l'acquisizione ha almeno 18 minuti e non risultano esecuzioni recenti (12 minuti) del workflow dedicato, richiede su GitHub il recupero con `workflow_dispatch`.
- Se il controllo è recente ma l'osservazione è vecchia, registra `source_delayed`: per Ecowitt non interpreta `generated_at` come orario della misura e non forza interrogazioni ravvicinate se Ecowitt è offline.
- I controlli già presenti su GitHub restano attivi come riserva. Non viene riattivata Weather Underground.

## Attivazione una tantum

Servono un account gratuito Cloudflare Workers e un token GitHub a privilegi minimi. **Mai incollare token o password nel repository, nelle issue o in chat.**

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**. Selezionare come unico repository `matteotidili/sardegna-meteolive-data`; autorizzare **Actions: Read and write** e **Contents: Read-only**.
2. Sul proprio computer, con Node.js installato e il repository scaricato, aprire il terminale nella cartella `cloudflare-watchdog` ed eseguire `npx wrangler login`.
3. Eseguire `npx wrangler deploy`. Il file `wrangler.toml` configura il cron ogni 5 minuti.
4. Eseguire `npx wrangler secret put GITHUB_ACTIONS_TOKEN` e incollare il token esclusivamente nel prompt sicuro. In alternativa: **Workers & Pages → sardegna-meteolive-watchdog → Settings → Variables and Secrets → Add → Secret**, nome `GITHUB_ACTIONS_TOKEN`.
5. Verificare in Cloudflare **Settings → Triggers** il cron `*/5 * * * *` e, nei log delle esecuzioni programmate, gli esiti `fresh`, `source_delayed`, `recent_run_pending`, `recovery_dispatched` o `error`.

**Finché deployment Cloudflare e secret non sono configurati, il controllo esterno NON è attivo**. Il primo deploy privo di secret può inizialmente produrre errori fino alla configurazione del token.

## Test e limiti

Con Node.js: `npm test`. Il test non interroga i servizi esterni.

Un controllo esterno riduce i buchi del cron GitHub, ma l'esecuzione dei workflow resta soggetta ai tempi di GitHub Actions. Non garantisce matematicamente nuovi dati entro 30 minuti e non può accelerare la pubblicazione delle osservazioni all'origine. Collaudare la continuità per 24–48 ore; sorvegliare la scadenza del token GitHub.

Cloudflare Workers Free, nei normali limiti per questo carico, non comporta canone aggiuntivo. Verificare i limiti correnti e gli eventuali altri utilizzi dell'account Cloudflare.

## Nota sul Worker già creato dalla dashboard Cloudflare

Il progetto Ecowitt è stato aggiunto al codice su GitHub, ma **la modifica su GitHub non aggiorna automaticamente il Worker installato manualmente nella dashboard Cloudflare**. Per attivare Ecowitt aprire **Cloudflare → Workers & Pages → sardegna-meteolive-watchdog → Edit code**, sostituire il contenuto con la versione corrente di `src/worker.js` e premere **Deploy**. Il cron e il secret `GITHUB_ACTIONS_TOKEN` già configurati non vanno ricreati.
