# Cruscotto

Una pagina web che mostra cosa sta facendo l'agente: patrimonio, posizioni aperte con stop e
take profit, ultime operazioni con il motivo del modello e i suoi pro e contro, la sequenza dei
risvegli, l'ultimo passaggio di consegne, le lezioni imparate, le modifiche ai parametri e i
limiti rigidi in vigore.

È generata dai record del repo, gira solo in locale e non chiama né Alpaca né altri servizi.

## Avviarla

Dalla radice del repo:

```sh
ui/serve.sh
```

e poi apri <http://localhost:8765>. Lo script costruisce la pagina, la ricostruisce ogni 60
secondi e la serve con `python -m http.server` su `127.0.0.1`. La pagina controlla ogni 60
secondi se i dati sono cambiati e in quel caso si ricarica da sola.

Opzioni: `PORT=9000` cambia porta; `PULL=1` fa anche `git pull --ff-only` a ogni giro, per
vedere i record che il cron di GitHub Actions spinge sul branch.

Solo la costruzione, senza server:

```sh
python3 -m ui.build                      # scrive ui/site/index.html e ui/site/data.json
python3 -m ui.build --root ALTRA/CARTELLA --out /tmp/site
```

Per provarla senza dati veri, con record inventati:

```sh
python3 -m ui.fixtures /tmp/finto --wakes 400
python3 -m ui.build --root /tmp/finto --out /tmp/finto-site
python3 -m http.server 8765 --directory /tmp/finto-site
```

## Da dove prende i dati

Ogni file è facoltativo: se manca, la sezione lo dice e la pagina resta intera. Una riga
illeggibile viene saltata e segnalata in fondo alla pagina.

| Sezione | File | Campi usati |
|---|---|---|
| Patrimonio (curva) | `state/portfolio_history.json` (formato di `GET /v2/account/portfolio/history`: `timestamp`, `equity`) | se manca: righe `kind: "account"` in `evidence/`, poi `equity` delle righe del journal, poi `equity` del handoff |
| Numeri in alto | `state/last_handoff.json` | `equity`, `last_equity` o `day_change_pct`, `positions` |
| Posizioni aperte | `state/last_handoff.json` → `positions` (come le restituisce Alpaca: `symbol`, `qty`, `avg_entry_price`, `current_price`, `market_value`, `unrealized_pl`, `unrealized_plpc`) | livelli di uscita da `state/positions.json`, `state/exits.json` o `state/levels.json` (per simbolo: `entry_price`, `stop`, `take_profit`, `opened_at`), oppure `exits`/`levels` nel handoff |
| Ultime operazioni | `journal/decisions.jsonl` | `verdicts` (`symbol`, `action`, `outcome`, `client_order_id`, `reason`, `reason_model`, `bull_case`, `bear_case`, `order`, `stop`, `take_profit`) ed `exits` (`symbol`, `reason`, `outcome`, `client_order_id`, `stop`); prezzo di esecuzione dai fill in `evidence/` |
| Operazioni chiuse, vinte, migliore e peggiore | `evidence/*.jsonl` | fill e ordini, accoppiati da `trader.learn.closed_trades` con le commissioni |
| Risvegli | `journal/decisions.jsonl` | `slot`, `status`, `run_url`, `error`, `skipped`, `proposal.market_view`, `jev.regime`; `kind: "reflection"` per le riflessioni |
| Regime di Jev | ultima riga del journal con `jev` | `regime`, `multiplier`, `confidence`, `source` |
| Passaggio di consegne | `state/last_handoff.json`, altrimenti la prima voce di `state/progress.md` | `status`, `slot`, `outcome`, `changed`, `market_view`, `remaining_risk`, `next_job`, `warnings` |
| Lezioni | `lessons/lessons.md` | le sezioni scritte da `trader.learn.write_lessons` |
| Modifiche ai parametri | righe `kind: "reflection"` del journal | `accepted` e `rejected` con `name`, `old_value`, `new_value`, `reason`, `why`, `score_before`, `score_after` |
| Parametri attuali | `config/params.json` | |
| Limiti rigidi | `config/limits.toml` letto con `trader.config.load_limits`, tetti da `trader.config.HARD_CEILINGS` e `HARD_FLOORS` | kill switch: file `KILL` nella radice |

I link ai run di GitHub Actions compaiono solo se iniziano con `https://github.com/`. Tutto il
testo che viene dai record (motivi del modello, lezioni, errori) è trattato come testo, mai come HTML.

## Com'è fatta

- `ui/data.py` legge i record e produce un dizionario; `ui/render.py` lo trasforma in una pagina
  HTML con CSS, SVG e JavaScript in linea, senza CDN né font esterni; `ui/build.py` scrive
  `index.html` e `data.json`.
- `ui/fixtures.py` scrive record inventati con gli stessi writer dell'agente, per i test
  (`tests/test_ui.py`) e per le prove.
- La pagina è pensata per uno schermo 1920×1080: tema scuro, testo grande, numeri tabulari.
