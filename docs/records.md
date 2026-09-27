# I record

Ogni file ha un solo compito. Tutti passano da `Records._write`, che toglie i segreti.
I record vengono salvati con un commit alla fine di ogni risveglio, anche fallito (in
`--dry-run` vanno in una copia temporanea e non si salvano).

| File | Compito | Chi lo legge |
|---|---|---|
| `STRATEGY.md` | La strategia in parole semplici, con il backtest e i suoi limiti. | una persona |
| `config/params.json` | I parametri della strategia. La riflessione li cambia solo entro `PARAM_BOUNDS`, con un commit che dice perché. | ogni risveglio, il backtest |
| `lessons/lessons.md` | Le lezioni della riflessione, la più recente in alto. Le ultime entrano nel prompt. | il modello, una persona |
| `state/progress.md` | Il passaggio di consegne tra un risveglio e il successivo, il più recente in alto: slot, cosa è cambiato, esito, rischio residuo, prossimo compito. | una persona |
| `state/last_handoff.json` | Lo stesso passaggio di consegne, in forma leggibile dal codice. Serve per trovare slot saltati o ripetuti e per passare `next_job` al modello. | il risveglio successivo |
| `state/daily_summary.json` | L'ultimo giorno UTC il cui riepilogo è arrivato su Telegram. Evita doppioni. | il risveglio successivo |
| `state/positions.json` | Per ogni posizione aperta: prezzo e ora d'entrata, ATR all'entrata, massimo toccato, stop e take-profit, `explore: true` se è un acquisto di esplorazione. Se manca, i livelli si ricostruiscono dai fill di Alpaca e l'etichetta dal journal. | il risveglio successivo |
| `state/cadence.json` | L'ultimo blocco di 6 ore già riflettuto e quello già riportato su Telegram; il patrimonio di partenza, per il guadagno "dall'inizio". | il risveglio successivo |
| `journal/decisions.jsonl` | Una riga per risveglio (`kind: wake`): mercato, lista corta con i segnali (`signals`), candidati con l'importo massimo, candidati di esplorazione (`explore_candidates`), chiamata a Jev (`jev`), proposta del modello con `bull_case` e `bear_case` (o perché era inutilizzabile, o perché non è stato interpellato), verdetto del gate (con `explore: true` per gli acquisti di esplorazione), esito dell'ordine, uscite del codice (`exits`). Una riga per riflessione (`kind: reflection`): riassunto, riassunto separato di regola ed esplorazione (`summary_by_kind`), attribuzione, lezioni, modifiche accettate e respinte con il motivo. | la riflessione, il riepilogo, una persona |
| `evidence/AAAA-MM-GG.jsonl` | Ordini e fill esattamente come li ha restituiti Alpaca, con `fetched_at`. | la riconciliazione, una persona |

## Chi ha ragione

Alpaca è la fonte di verità per conto, posizioni e ordini. I record servono a
ricordare cosa l'agente ha deciso e perché, e a capire cosa manca: un ordine con
prefisso `trd-` che Alpaca conosce e `evidence/` no viene da un risveglio
interrotto, e il risveglio successivo lo scrive con la nota `recuperato`.
