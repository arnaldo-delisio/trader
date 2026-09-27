# Strategia

Seguire le tendenze su più ore, solo quando il mercato nel suo insieme sale, e pagare
commissioni solo quando il movimento atteso le supera di molto. Tutto il resto è cassa.

A ogni risveglio (ogni 15 minuti) il codice calcola gli indicatori per tutte le crypto
dell'universo, dà un punteggio a ciascuna e passa al modello una lista corta. Il modello
propone; il controllo del rischio in codice decide. Le uscite (stop e take-profit) le
applica il codice a ogni risveglio, qualunque cosa proponga il modello.

## Perché così

Le commissioni di Alpaca per le crypto sono lo 0,25% per ordine a mercato (taker, fascia
sotto i 100.000 $ di volume a 30 giorni; fonte:
[crypto-fees](https://docs.alpaca.markets/docs/crypto-fees), letta il 26 settembre 2026).
In più un ordine a mercato paga metà dello spread: misurato il 26 settembre 2026 alle 21:30 UTC,
lo spread va dallo 0,02% di BTC e ETH allo 0,5-0,8% di BCH, LTC, AVAX, XTZ e BONK. Un giro
completo (compra e vendi) costa quindi tra lo 0,55% e l'1,3%.

Sui dati di aprile-settembre 2026 nessun indicatore prevede il movimento delle 4 ore
successive: a quella distanza è rumore. Su 24-72 ore invece la tendenza (medie mobili su 1h e 4h)
e l'RSI distinguono un po' le crypto che poi fanno meglio delle altre. Per questo la strategia
entra di rado, tiene per giorni e non per minuti, e il risveglio ogni 15 minuti serve soprattutto
a far rispettare gli stop in tempo.

## Cosa guarda

Barre da 15 minuti, e da queste barre da 1 ora e da 4 ore. Solo barre chiuse.

| Indicatore | Dove | Perché |
|---|---|---|
| EMA 9, 21, 50 | 1h e 4h | La tendenza: prezzo sopra la EMA 50, EMA 9 sopra la 21, 21 sopra la 50 |
| RSI 14 | 1h | Forza del movimento: meglio tra 60 e 72, male sotto 30 o sopra 85 (tirato) |
| MACD (12, 26, 9) | 1h | Accelerazione, misurata in ATR |
| ADX 14 con +DI e -DI | 1h | Quanto è forte la tendenza e in che direzione |
| Bollinger %B e ampiezza | 1h | Dove sta il prezzo dentro la banda; l'ampiezza dice se il mercato è compresso |
| ATR 14 | 1h | La volatilità: misura stop, take-profit e dimensione |
| Volume z-score | 1h | Volume insolito, con il segno della barra |
| Momentum relativo | 24h | Posizione della crypto nella classifica dei rendimenti a 24 ore |

Ogni segnale diventa un numero tra -1 e +1. Il punteggio è la media pesata con i pesi di
`config/params.json`, anch'esso tra -1 e +1.

## Regole

1. **Filtro di mercato.** Si compra solo se il paniere di tutte le crypto (a pesi uguali) è salito
   negli ultimi 7 giorni e almeno il 60% delle crypto è in tendenza positiva sul 4h. Se no, niente
   acquisti. Le uscite continuano.
2. **Entrata.** Punteggio almeno 0,8 (`entry_threshold`) e movimento atteso (6 ATR orari,
   `tp_atr_mult`) almeno 12 volte il costo di un giro completo su quella crypto (`min_edge_mult`).
   Sulle crypto con spread largo o poco volatili questo esclude quasi ogni entrata: è voluto.
3. **Dimensione.** Si rischia lo 0,5% del patrimonio se scatta lo stop iniziale, senza superare l'8%
   del patrimonio per crypto; poi Jev scala l'importo (x1,0 risk-on, x0,7 neutro, x0,4 risk-off).
   I tetti nel codice (60% investito, 8% per crypto, 3% per memecoin, 10 posizioni) valgono sempre.
4. **Stop.** Stop mobile a 4 ATR orari sotto la chiusura più alta dall'entrata. Sale e non scende mai.
5. **Take-profit.** Prezzo di entrata più 6 ATR orari (misurati all'entrata). Fisso.
6. **Altre uscite.** Punteggio sceso a -0,3 o meno (`exit_threshold`), oppure 7 giorni di tenuta
   (`max_hold_hours`).
7. **Pausa.** Dopo un'uscita, 24 ore senza rientrare sulla stessa crypto (`cooldown_hours`).
8. Senza dati freschi (ultima barra più vecchia di 2 ore) una crypto non si compra.

Il modello viene interpellato solo quando almeno una crypto passa tutte le regole d'ingresso.
Vede i candidati con l'importo massimo, il resto della lista corta (le 8 migliori per punteggio
più quelle in portafoglio) con gli indicatori, le posizioni con i loro stop, le ultime operazioni
e le lezioni. Per ogni proposta scrive una frase a favore e una contro. Può essere più prudente
delle regole, non più aggressivo: il gate respinge l'acquisto di una crypto che non è candidata
o oltre il suo importo massimo.

Ogni 6 ore una riflessione può spostare di poco i pesi, le soglie, i multipli di ATR e la
lunghezza della lista, solo entro i limiti in `trader/strategy.py` (`PARAM_BOUNDS`, `LEARNABLE`)
e solo se il backtest sugli ultimi giorni, al netto dei costi, non peggiora né il rendimento
netto né il drawdown massimo (di più di 1 punto). Il filtro di mercato,
`min_edge_mult` e i tetti di rischio non si imparano.

## Esplorazione

La regola d'ingresso è stretta di proposito, e nel backtest compra di rado: nei mesi in calo quasi
mai. Con pochi giorni di trading dal vivo questo vuol dire poche operazioni o nessuna, e una
riflessione che non ha niente da leggere. Per questo c'è un secondo canale, piccolo e separato:

- **Chi.** Una crypto della lista corta che non passa la regola d'ingresso, con tendenza positiva
  sul 4h e un take-profit (6 ATR orari) almeno 3 volte il costo di un giro. Il filtro di mercato e la
  soglia del punteggio non valgono.
- **Quanto.** L'1% del patrimonio, scalato dal regime di Jev, e al massimo il 5% del patrimonio in
  tutte le posizioni di esplorazione insieme. Sono tetti nel codice (`HARD_CEILINGS`): il file dei
  limiti può solo abbassarli, la riflessione non può toccarli.
- **Come esce.** Come tutte le altre: stop mobile, take-profit, tenuta massima, segnale.
- **Dove si vede.** L'etichetta `esplorazione` nel journal, nei livelli d'uscita, su Telegram e nel
  cruscotto. La riflessione riassume a parte le operazioni della regola e quelle di esplorazione.

Il rischio in soldi è piccolo: lo stop iniziale è a 4 ATR orari; con un ATR orario tra lo 0,5% e
l'1,5% (un esempio, non una misura) è tra il 2% e il 6% sotto l'entrata, quindi con tutte e cinque le
posizioni ferme allo stop si perde tra lo 0,1% e lo 0,3% del patrimonio. Quello che si compra è informazione: se le
esplorazioni sotto la soglia vanno bene più volte, la riflessione ha un motivo concreto per
proporre di abbassarla, e il backtest decide se accettarlo.

## Il backtest

`scripts/backtest.py` simula la parte deterministica: a ogni barra da 15 minuti aggiorna e
controlla le uscite, poi compra le crypto con il punteggio migliore che passano le regole, come
sostituto del modello. Costi: 0,25% di commissione per ordine più metà dello spread misurato per
ogni crypto. L'ordine viene eseguito alla chiusura della barra dopo la decisione (il risveglio è in
ritardo di 5-15 minuti). Patrimonio iniziale 10.000 $, Jev fisso su neutro (x0,7).

```sh
uv run python scripts/backtest.py fetch --days 180    # dati in data/, fuori da git
uv run python scripts/backtest.py run --cut 2026-07-22
uv run python scripts/backtest.py sweep --cut 2026-07-22   # la griglia della scelta, senza le varianti di pesi
```

Dati: 31 crypto, barre da 15 minuti dal 30 marzo al 26 settembre 2026. I primi 9 giorni servono
a scaldare gli indicatori. Train e test sono divisi nel tempo, il test viene dopo.

| Periodo (UTC) | Netto | Max drawdown | Operazioni | Vinte | Commissioni | Lordo | Paniere comprato e tenuto |
|---|---|---|---|---|---|---|---|
| Train, 8 apr - 21 lug | -1,23% | 2,86% | 54 | 28% | 116 $ | +0,58% | -14,79% |
| Test, 22 lug - 26 set | +4,86% | 3,89% | 85 | 46% | 183 $ | +7,74% | +49,12% |
| Tutto | +3,56% | 3,89% | 139 | 39% | 296 $ | +8,22% | +28,57% |

Con costi diversi (netto train / test):

| Ipotesi | Train | Test |
|---|---|---|
| Base (0,25% + metà spread) | -1,23% | +4,86% |
| Commissione 0,40% | -0,03% | +1,08% |
| Slippage fisso 0,30% per lato | -0,16% | +2,86% |
| Commissione maker 0,15% | -3,88% | +8,63% |

Con costi più alti le operazioni scendono (la regola 2 esclude di più) e il risultato si avvicina a
zero; con costi più bassi entrano più operazioni, e nel periodo in calo si perde di più.

### Come ho scelto i parametri

1. Prima versione provata su luglio-settembre (mercato in forte salita): positiva. Poi scaricati i
   dati da aprile e provata lì, su mesi mai visti: **-5,6%**. Era una strategia che guadagnava
   solo perché tutto saliva.
2. Studio degli eventi su tutti i 170 giorni: a 4 ore nessun segnale prevede niente; a 24-72 ore
   tendenza e RSI sì, MACD, ADX e volume no o al contrario.
3. Aggiunto il filtro di mercato (paniere a 7 giorni e ampiezza). Il filtro è stato scelto
   guardando entrambi i periodi.
4. Griglia di 972 combinazioni (pesi, soglia, stop, take-profit, costo minimo, pausa) valutata
   **solo sul train**, ordinata per rendimento meno metà del drawdown. La scelta è la prima. Spostando
   un parametro alla volta il train resta tra -4,9% e 0% e il test tra -0,6% e +7,8%: nessun
   punto isolato. I pesi sono quelli di partenza: le varianti con solo tendenza e RSI non hanno
   fatto meglio nel train.

### Cosa dicono i numeri, onestamente

- **Nel periodo in calo nessuna combinazione guadagna in modo apprezzabile al netto dei costi.**
  Le migliori stanno intorno allo zero (quella scelta perde l'1,2%) mentre il paniere perde il 15%. Quello che la strategia sa fare è stare fuori quando
  il mercato scende e prendere una parte della salita quando sale.
- **Guadagna molto meno di comprare e tenere in un mercato che sale** (+4,9% contro +49%): è
  investita solo in parte (al massimo il 60%) e taglia presto.
- **Il test non è del tutto pulito.** Ho visto luglio-settembre prima di scegliere, e il filtro di
  mercato è stato scelto guardando entrambi i periodi. Il vero fuori campione comincia con il
  primo risveglio.
- **Sei mesi sono pochi**: un calo e una salita. Un crollo rapido o mesi di mercato piatto
  potrebbero andare diversamente.
- **Il vantaggio lordo è piccolo** (+0,6% in tre mesi e mezzo nel calo): un aumento dei costi o
  degli spread lo porta via.

## Limiti del backtest

- Sostituisce il modello con la regola del punteggio: non sa se il modello farà meglio o peggio.
- Jev è fisso su neutro: nel conto vero le dimensioni cambieranno con il regime.
- Lo spread è una sola misura (sabato sera): nei giorni feriali è di solito più stretto, nei
  momenti agitati più largo. Il costo di ogni crypto è tenuto fisso per tutto il periodo.
- Non simula risvegli saltati, fill parziali, né ordini più grandi del volume della barra.
- Il conto paper di Alpaca addebita davvero le commissioni: verificato il 26 settembre 2026
  su tre ordini di prova (l'acquisto trattiene lo 0,25% in crypto; dettagli in
  [docs/alpaca-notes.md](docs/alpaca-notes.md#verificato-sul-conto-paper)).

## Cosa non fa

Niente leva, niente vendite allo scoperto, niente ordini che il gate non ha approvato, niente
recupero dei risvegli saltati.
