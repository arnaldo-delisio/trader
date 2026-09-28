# Risultato finale

Conto Alpaca **paper** (soldi finti), letto il 2026-09-28 alle 10:35 UTC, dopo la vendita
dell'ultima posizione. Il trading è chiuso: `TRADING_ENABLED=false`, `LIQUIDATE=false`, workflow
`wake` disattivato e timer di riserva spento, tutto il 2026-09-28 alle 10:33 UTC.

## Il conto

| | |
|---|---|
| Capitale iniziale | 100.000,00 $ (conto creato il 2026-09-26) |
| Capitale all'avvio dell'agente | 99.999,87 $ (2026-09-26 22:31 UTC) |
| Capitale finale | **99.504,00 $**, tutto in liquidità |
| Risultato rispetto a 100.000 $ | **-496,00 $ (-0,50%)** |
| Risultato dei trade chiusi dall'agente | -495,78 $, commissioni stimate comprese |
| Posizioni aperte | nessuna vendibile: restano 0,000000001 unità su 6 monete (ARB, DOT, ETH, GRT, LINK, SOL), valore totale 0,000003 $ |
| Ordini aperti | 0 |

I 0,13 $ tra 100.000 e 99.999,87 sono tre ordini BTC di prova fatti a mano il 2026-09-26 tra
le 20:23 e le 20:24 UTC per verificare la connessione, prima che l'agente partisse. Non sono
contati come trade dell'agente.

Le frazioni da 0,000000001 sono il resto che Alpaca lascia quando trattiene la commissione di
acquisto nella moneta stessa. Sono sotto il minimo di vendita e non si possono chiudere.

## I trade

Periodo: primo ordine 2026-09-26 23:16 UTC, ultimo 2026-09-28 10:32 UTC.

| | |
|---|---|
| Ordini inviati dall'agente | 47: 15 acquisti eseguiti, 15 vendite eseguite, 17 acquisti annullati |
| Esecuzioni (fill) | 38 (un ordine può essere eseguito in più pezzi) |
| Trade chiusi | 15 |
| Vinti | 1 (GRT, take-profit, +89,33 $, +13,0%) |
| Percentuale vinti | 6,7% |
| Chiusi a stop | 4, in totale -167,96 $ |
| Chiusi dalla liquidazione | 10, in totale -417,15 $ |
| Peggiore | ARB, stop, -91,96 $ (-10,4%) |
| Durata media | 24,2 ore |
| Commissioni | 62,08 $ stimate (vedi sotto) |

14 trade su 15 erano esplorazione: acquisti piccoli su monete sotto la soglia di ingresso, fatti
apposta per avere dati da cui imparare. Uno solo (SKY, -161,95 $) è entrato con la regola
principale.

I 17 acquisti annullati sono ordini che Alpaca paper non ha mai eseguito (POL 8, RENDER 4,
ONDO 3, WIF 2): il codice li ha annullati come ordini fermi.

## La liquidazione

La vendita di tutto era prevista per il 2026-09-27 alle 21:00 UTC e non è partita: il portatile
che lanciava i risvegli di riserva è rimasto senza rete durante la notte. È partita il
2026-09-28 alle 08:45 UTC e ha venduto 9 posizioni. BONK è stata rifiutata da Alpaca (HTTP 403
"insufficient balance"): il codice chiedeva 156921866,806502372 unità, ne erano disponibili
156921866,806502359. La quantità passava da un numero decimale in virgola mobile, che per un
numero di 18 cifre non è esatto. Corretto nel commit b628d4c: la vendita di una posizione intera
ora usa la quantità esattamente come la scrive Alpaca, con un test che fallisce sul codice
vecchio. Il risveglio delle 10:30 UTC ha venduto BONK e il conto è rimasto in liquidità.

Tra le 08:45 e le 10:30 BONK ha perso valore: il capitale è sceso da 99.540,35 $ (dopo la prima
liquidazione) a 99.504,00 $.

## I risvegli

| | |
|---|---|
| Risvegli registrati nel giornale | 54, su 50 slot diversi da 15 minuti (4 doppioni che non hanno fatto nulla, come previsto) |
| Slot possibili | 145, dal 2026-09-26 22:30 al 2026-09-28 10:30 UTC |
| Riflessioni | 7, due con una modifica accettata |
| Esecuzioni su GitHub visibili ora | 31, tutte riuscite: 25 lanciate dal timer di riserva (`workflow_dispatch`), 6 dal cron di GitHub (`schedule`) |

Il cron di GitHub ogni 15 minuti è partito raramente: 6 volte in 27 ore. Il resto l'ha lanciato
un timer sul portatile del proprietario. Dal 2026-09-27 16:47 al 2026-09-28 08:47 UTC il
portatile era senza rete e sono partiti solo 5 risvegli, tutti dal cron di GitHub.

Le due modifiche accettate dalla riflessione, ognuna verificata con un backtest e salvata con un
commit che ne spiega il motivo:

- 2026-09-28 01:45 UTC: `entry_threshold` da 0,80 a 0,75
- 2026-09-28 08:15 UTC: `weights.volume` da 0,25 a 0,5

## Cosa non è verificato

- **Le commissioni.** Il conto paper non restituisce attività di commissione (richiesta
  `CFEE,FEE` vuota il 2026-09-28). I 62,08 $ sono calcolati dal codice con lo 0,25% per lato
  (`trader/learn.py`). Il capitale finale letto da Alpaca invece è un dato del conto.
- **Chi ha lanciato i primi risvegli.** Il repo pubblico è stato ricreato il 2026-09-27 alle
  06:59 UTC; le esecuzioni precedenti (25 risvegli nel giornale) non sono più consultabili su
  GitHub, quindi non so quante venivano dal cron e quante dal timer.
- **I buchi nella notte del 2026-09-26.** Tra le 00:30 e le 03:15 UTC del 2026-09-27 mancano
  alcuni slot. Anche il buco tra le 08:15 e le 13:00 UTC del 2026-09-27 non ha una causa
  registrata.
- **Il prezzo di esecuzione di una vendita paper** non è quello che si avrebbe su un conto
  reale: Alpaca paper simula i fill.

## Come rifare i conti

I numeri vengono dal conto Alpaca (`/v2/account`, `/v2/positions`, `/v2/orders`,
`/v2/account/activities/FILL`) e dal giornale `journal/decisions.jsonl`. I trade chiusi sono
calcolati da `learn.closed_trades`, lo stesso codice che usa la riflessione. Le esecuzioni
GitHub da `gh run list --workflow wake.yml`.

Il fill di BONK delle 10:32 UTC è stato aggiunto a `evidence/2026-09-28.jsonl` a trading chiuso,
perché il risveglio successivo, che lo avrebbe registrato, non parte più. La dashboard
(`ui/serve.sh`) conta solo gli ordini dell'agente, come la riflessione, e mostra gli stessi 15
trade.
