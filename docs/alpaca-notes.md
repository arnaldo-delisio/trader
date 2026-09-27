# Alpaca: cosa ho verificato e dove

Letto sulla documentazione ufficiale il 26 settembre 2026. Dove due pagine si
contraddicono, o la documentazione tace, lo dico e scrivo cosa fa il codice.

| Regola | Fonte | Nel codice |
|---|---|---|
| URL paper: `https://paper-api.alpaca.markets`; le chiavi paper sono diverse da quelle live | [paper-trading](https://docs.alpaca.markets/docs/paper-trading) | `config.PAPER_URL`, `assert_paper()` |
| Il paper simula anche le crypto; fill parziali casuali nel 10% dei casi | [paper-trading](https://docs.alpaca.markets/docs/paper-trading) | nessuna ipotesi di fill completo: si leggono ordini e fill da Alpaca |
| `POST /v2/orders`; campi `symbol`, `qty` o `notional` (mai entrambi), `side`, `type`, `time_in_force`, `client_order_id` | [postorder](https://docs.alpaca.markets/reference/postorder) | `orders.place()` |
| `client_order_id`: massimo 128 caratteri | [postorder](https://docs.alpaca.markets/reference/postorder) | `orders.client_order_id()` (29-30 caratteri) |
| Crypto: tipi `market`, `limit`, `stop_limit`; `time_in_force` solo `gtc` e `ioc` | [crypto-orders](https://docs.alpaca.markets/docs/crypto-orders), [orders-at-alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca) | ordini `market` + `gtc` |
| Ordini frazionari con `notional` o `qty`; simboli nel formato `BTC/USD` | [crypto-trading](https://docs.alpaca.markets/docs/crypto-trading) | acquisti in `notional`, vendite in `qty` |
| Niente margine, niente vendite allo scoperto per le crypto | [crypto-trading](https://docs.alpaca.markets/docs/crypto-trading) | il gate usa `non_marginable_buying_power` e rifiuta vendite oltre la quantità detenuta |
| Massimo 200.000 $ di nozionale per ordine | [crypto-trading](https://docs.alpaca.markets/docs/crypto-trading) | irrilevante: il tetto nel codice è 500 $ |
| `GET /v2/orders:by_client_order_id?client_order_id=...` | [getorderbyclientorderid](https://docs.alpaca.markets/reference/getorderbyclientorderid) | `Alpaca.order_by_client_id()` |
| `GET /v2/orders`: `status` open/closed/all, `limit` max 500, `after`, `direction` | [getallorders-1](https://docs.alpaca.markets/reference/getallorders-1) | `Alpaca.orders()` |
| Stati aperti: `new`, `accepted`, `pending_new`, `accepted_for_bidding`, `pending_cancel`, `pending_replace`, `partially_filled` | [orders-at-alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca) | `orders.OPEN_STATUSES` |
| `GET /v2/account/activities/FILL` con `after`, `direction`, `page_size` (max 100), `page_token` (l'id dell'ultima attività della pagina) | [getaccountactivitiesbyactivitytype-1](https://docs.alpaca.markets/reference/getaccountactivitiesbyactivitytype-1) | `Alpaca.fills()` segue le pagine |
| `GET /v2/assets?asset_class=crypto&status=active`: `symbol`, `tradable`, `min_order_size` | [get-v2-assets](https://docs.alpaca.markets/reference/get-v2-assets-1) | `Alpaca.assets()`: l'universo di ogni risveglio |
| Commissioni crypto, fascia 1 (sotto 100.000 $ di volume a 30 giorni): 0,15% maker, 0,25% taker | [crypto-fees](https://docs.alpaca.markets/docs/crypto-fees) | ordini a mercato: 0,25% per lato nel backtest, nella soglia d'ingresso e nel calcolo delle operazioni |
| `GET /v2/account`: `equity`, `last_equity` (equity alla chiusura del giorno di trading precedente, 16:00 ET), `non_marginable_buying_power`, `trading_blocked`, `crypto_status` | [getaccount-1](https://docs.alpaca.markets/reference/getaccount-1) | limite di perdita giornaliera e controllo liquidità |
| Dati: `GET https://data.alpaca.markets/v1beta3/crypto/us/bars` (`symbols`, `timeframe` es. `1Hour`, `start`, `limit` max 10.000, `sort`) e `/latest/quotes` (`ap`, `bp`, ...) con gli header `APCA-API-KEY-ID` e `APCA-API-SECRET-KEY` | [cryptobars-1](https://docs.alpaca.markets/reference/cryptobars-1), [cryptolatestquotes-1](https://docs.alpaca.markets/reference/cryptolatestquotes-1) | `Alpaca.bars()`, `Alpaca.latest_quotes()` |
| Un `client_order_id` già usato da un ordine attivo risponde HTTP 422 `{"code":40010001,"message":"client_order_id must be unique"}` | [guida agli errori](https://alpaca.markets/learn/how-to-fix-common-trading-api-errors-at-alpaca) | il 422 duplicato si risolve cercando l'ordine per id e adottandolo |

## Verificato sul conto paper

Il 26 settembre 2026, con tre ordini di prova (due acquisti da 11 $ di BTC e una vendita):

- **Le commissioni ci sono anche sul paper.** L'acquisto trattiene lo 0,25% in crypto: sono
  stati comprati 0,000256643 BTC e ne restavano vendibili 0,000255999. Il conto è sceso di 0,13 $.
  Per questo il codice vende sempre la quantità detenuta (`qty_available`), non quella comprata,
  e `learn.closed_trades` considera chiusa un'operazione quando resta meno dell'1%.
- **Un ordine si divide in più fill.** Ogni ordine ha prodotto due attività FILL con lo stesso
  `order_id`: il codice le somma per ordine.
- **Un acquisto con `notional` da 11 $ riceve crypto per circa 10,78 $: è il collare del 2%.**
  Riletti il 26 settembre 2026 gli ordini e i fill dei due acquisti di prova: `notional` "11",
  `filled_qty` 0,000128322 e 0,000128321, `filled_avg_price` 84.029,58 e 84.033,94, cioè 10,78 $
  ciascuno (l'1,98% in meno). Gli ordini non hanno campi `commission` o `fees` (valgono `null`), e
  la commissione non spiega la differenza: lo 0,25% trattenuto in BTC si vede a parte, nella
  posizione (0,000256643 comprati, 0,000255999 vendibili). Il conto è sceso di 0,13 $ in tutto, quindi
  Alpaca ha addebitato i 10,78 $ eseguiti e non gli 11 $ chiesti. La causa è scritta in una pagina di
  supporto ([supporto](https://alpaca.markets/support/why-do-i-see-a-discrepancy-between-the-notional-value-requested-and-the-order-value-ultimately-filled),
  letta il 26 settembre 2026): ogni ordine crypto a mercato diventa un limite con un collare del 2%
  sul prezzo ask, e un ordine in `notional` può riempirsi per meno del nozionale chiesto. I numeri
  tornano con una quantità calcolata come `notional / (ask x 1,02)` e arrotondata a 1e-9: con un
  ask di circa 0,01% sopra il prezzo del fill si ottengono esattamente le quantità ricevute. Questa
  formula è una mia ricostruzione su due ordini, la pagina non la scrive.
  Effetto sul codice: una posizione viene circa il 2% più piccola dell'importo approvato dal gate, e
  la spesa non supera mai l'importo approvato. Ho scelto di non correggere la dimensione: comprare in
  `qty` darebbe la dimensione esatta ma potrebbe spendere fino al 2% in più di quanto il gate ha
  controllato, e i tetti devono valere sulla spesa. Stop, take-profit e risultati usano il prezzo e la
  quantità dei fill reali, quindi non sono falsati. Il problema che questo creava (una posizione vicina
  ai 10 $ che scendeva sotto il minimo e non poteva più uscire) è chiuso da `risk.exit_min_usd`: la
  vendita di tutta la posizione chiede solo il minimo di Alpaca.
- **Le barre da 15 minuti di 31 crypto per 10 giorni** sono 40 pagine (circa 750 righe l'una) e
  30 secondi in sequenza; il codice chiede una crypto per richiesta, 8 alla volta.
- **`min_order_size`** degli asset `/USD`, ricontrollato il 26 settembre 2026 alle 22:30 UTC su tutte
  le 31 crypto: tra 0,96 $ e 1,07 $ al prezzo del momento, memecoin comprese (BONK 271.003 unità,
  SHIB 166.667). Il minimo del codice è 10 $, quindi sopra quello di Alpaca per ogni crypto.
- **Precisione della quantità.** `min_trade_increment` e `price_increment` valgono 0,000000001 su tutte
  le 31 crypto. Una posizione in BONK o SHIB è di milioni di unità con 9 decimali, cioè circa 16 cifre
  significative, più di quante ne tenga un float: arrotondando al più vicino, il 3,8% delle quantità
  di quella grandezza superava di 0,000000001 il detenuto. `orders.format_qty` arrotonda per difetto.
- **Commissione nella valuta ricevuta** ([crypto-fees](https://docs.alpaca.markets/docs/crypto-fees),
  riletta il 26 settembre 2026): su un acquisto in crypto, su una vendita in USD. Torna con quanto
  visto sul conto: l'acquisto lascia lo 0,25% in meno di coin, e per questo le vendite usano
  `qty_available`. La pagina non dice se il paper applichi le commissioni; il conto dice di sì.

## Punti aperti

- **Ordine minimo.** Una pagina dice che il minimo per le coppie in USD è `10 / prezzo`
  ([crypto-trading-1](https://docs.alpaca.markets/us/docs/crypto-trading-1)), una pagina di supporto parla di 1 $
  ([supporto](https://alpaca.markets/support/can-we-submit-orders-smaller-than-1-usd-in-notional-value)).
  Il codice usa la più prudente per gli acquisti e le vendite parziali: minimo 10 $, e il file di
  configurazione non può abbassarlo. La vendita di tutta la posizione (ogni uscita) chiede solo il
  minimo di Alpaca: `min_order_size` della crypto, letto a ogni risveglio, e comunque almeno 1 $
  (`config.EXIT_MIN_ORDER_USD`). Il 26 settembre 2026 `min_order_size` valeva tra 0,96 $ e 1,07 $.
- **`notional` e `time_in_force`.** La reference di `POST /v2/orders` dice che `notional` funziona solo
  con `day`, ma per le crypto `day` non è ammesso. Il codice manda `notional` con `gtc`, come nelle guide crypto.
  Sul paper funziona: i due acquisti di prova del 26 settembre 2026 erano `notional` con `gtc` e sono stati eseguiti.
- **404 su `orders:by_client_order_id`.** La reference documenta solo il 200. Il codice tratta il 404 come
  "ordine non esistente". Qualsiasi altro errore su quella verifica blocca l'invio.
- **Duplicato su ordini chiusi.** La guida parla di id usato da un ordine *attivo*. Non ho verificato se un id
  di un ordine già eseguito venga rifiutato. Il codice non dipende da questo: prima di inviare cerca sempre l'id.
- **Simbolo nelle posizioni.** Non ho trovato un esempio di posizione crypto: potrebbe essere `BTCUSD` o `BTC/USD`.
  Il codice confronta i simboli senza la barra (`norm_symbol`).
- **Paginazione dei fill.** La reference descrive `page_token` come l'id dell'ultima attività della
  pagina precedente; non ho ancora visto una seconda pagina reale (i fill di 8 giorni erano 6).
