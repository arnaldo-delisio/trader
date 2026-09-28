# Lezioni

Scritte dalla riflessione ogni 6 ore, la più recente in alto. Il modello le rilegge a ogni risveglio.

<!-- lesson -->
## 2026-09-28 08:16 UTC · slot 20260928T0815Z

- Con 5 trade chiusi (tutti esplorazione, 0 dalla regola) siamo ancora sotto la soglia minima di ~10 indicata dalle linee guida: mantengo un solo cambiamento cauto e nessuna modifica a stop/tp o soglie di ingresso/uscita in questa riflessione.
- Le 5 operazioni esplorative hanno perso in aggregato -78.62$ con win rate 20% (4 stop su 5), quindi i punteggi appena sotto soglia (0.79, 0.89, 0.64 citati nei log) che non hanno attivato la regola si sono rivelati esiti negativi: questo smentisce l'ipotesi delle riflessioni precedenti che entry_threshold=0.75 fosse troppo rigido, e anzi supporta il mantenerlo o alzarlo, non abbassarlo.
- Il segnale volume mostra ora corr=0.966 con n=5 (positivo: +0.40% win 33.3%; non positivo: -6.70% win 0%), confermando lo stesso pattern già osservato con n=2 in una riflessione precedente: due letture consecutive coerenti giustificano un piccolo aumento del suo peso, attualmente il più basso (0.25).
- ADX ha corr=0.922 ma anche quando positivo il pnl medio resta negativo (-0.44% su 4 trade), quindi il segnale distingue i disastri (worst -10.4% quando adx non positivo) ma non garantisce trade vincenti: non alzare ancora il suo peso, servono più dati.
- I 4 trade chiusi a stop hanno impiegato 20-27h prima di scattare (non rapidamente), mentre l'unico take-profit è arrivato in 5.4h: non c'è evidenza che gli stop siano troppo stretti, quindi non tocco stop_atr_mult né tp_atr_mult con questo campione.
<!-- lesson -->
## 2026-09-28 01:52 UTC · slot 20260928T0145Z

- Con soli 2 trade chiusi (1 vinto a take-profit, 1 perso a stop) il campione resta troppo piccolo per un'attribuzione statistica affidabile sui pesi dei segnali: non tocco i pesi in questa riflessione.
- Entrambi i trade chiusi provengono dall'esplorazione (regola: 0 trade su decine di wake), e i giornali mostrano ripetutamente punteggi vicini ma sotto 0.80 (es. 0.79, 0.76, 0.89 citato ma mai eseguito come regola) per giorni consecutivi: questo pattern ripetuto, non un singolo trade, giustifica un piccolo abbassamento di entry_threshold per permettere qualche trade a regola vera e verificarne la q
- Il segnale volume mostra la massima divergenza osservata (positivo: +12.99% vincente; non positivo: -2.99% perdente, corr=1.0), ma con solo 2 osservazioni è ancora rumore: da confermare con più trade prima di alzarne il peso.
- Il trade perso (UNIUSD) ha impiegato 24h per toccare lo stop mentre il vincente (GRTUSD) ha centrato il take-profit in 5.4h: nessuna indicazione chiara su stop_atr_mult o tp_atr_mult con soli 2 esiti, meglio aspettare altri trade prima di modificarli.
- Servono almeno ~10 trade chiusi (regola + esplorazione) per distinguere segnale da fortuna: fino ad allora limitare i cambiamenti a un solo parametro alla volta e solo se supportato da pattern ripetuti nei log, come il mancato innesco della regola piena.
<!-- lesson -->
## 2026-09-27 20:27 UTC · slot 20260927T2015Z

- Con un solo trade chiuso (n=1, +89.33$ su GRTUSD, esplorazione, TP centrato) il campione è troppo piccolo per qualsiasi attribuzione statistica: non modifico pesi, soglie, stop o take-profit in questa riflessione.
- L'unico trade chiuso proviene dalla categoria esplorazione (regola: 0 trade, esplorazione: 1 trade, 100% win) e ha raggiunto il take-profit con un guadagno netto del 12.99%: è un indizio che entry_threshold=0.80 potrebbe essere troppo rigido, ma un solo esito non basta a distinguere segnale da fortuna.
- Tutti i segnali (adx, bollinger, macd, momentum, rsi, trend_1h, trend_4h, volume) risultano positivi sull'unico trade vincente, quindi non c'è ancora varianza sufficiente per alzare o abbassare pesi specifici: servono trade con segnali contrastanti per un'attribuzione affidabile.
- Il fatto che il take-profit sia stato raggiunto in sole 5.4 ore suggerisce che tp_atr_mult=6.0 non è troppo lontano per questo tipo di mossa, ma con un solo caso non è prudente dedurre che vada alzato o abbassato: servono almeno ~10 trade chiusi per verificare se i target vengono raggiunti troppo spesso o quasi mai.
- Mantenere tutti i parametri invariati finché non si accumula un campione minimo di operazioni chiuse (regola e esplorazione insieme) sufficiente a separare rumore da segnale reale, come già indicato nelle riflessioni precedenti.
<!-- lesson -->
## 2026-09-27 13:03 UTC · slot 20260927T1300Z

- Con 0 trade chiusi (solo posizioni esplorative ancora aperte su BAT, POL, FIL, DOT, LINK, SUSHI, UNI, RENDER, YFI) manca qualsiasi base statistica: non modifico pesi, soglie, stop/TP o size in questa riflessione.
- Il pattern ripetuto di punteggi appena sotto soglia (0.59-0.81, mai ≥0.80) resta un'ipotesi da testare con esiti reali di stop/TP/scadenza, non da anticipare abbassando entry_threshold senza dati.
- Attendere almeno ~10 trade chiusi con risultato netto misurabile prima di distinguere segnali utili da rumore e valutare l'attribuzione per singolo indicatore.
- Nessun cambiamento ai parametri finché il campione di operazioni chiuse resta a zero, per evitare di ottimizzare sul rumore delle sole posizioni ancora in corso.
<!-- lesson -->
## 2026-09-27 06:03 UTC · slot 20260927T0600Z

- Con 0 trade chiusi resta impossibile ogni attribuzione statistica: non modifico pesi, soglie o stop/TP in questa riflessione.
- Numerose posizioni esplorative sono aperte (BAT, POL, FIL, DOT, LINK, SUSHI, UNI, RENDER, YFI) ma nessuna è ancora chiusa: servono i loro esiti reali (stop/TP/scadenza) prima di poter giudicare se entry_threshold=0.80 è troppo restrittivo rispetto ai punteggi osservati (0.59-0.81).
- Il pattern ripetuto di score vicini alla soglia (0.76-0.81) senza mai chiuderla suggerisce un'ipotesi da verificare con dati reali, non da anticipare: aspettare almeno ~10 trade chiusi per evitare di ottimizzare sul rumore.
- Mantenere tutti i parametri invariati finché non si accumula un campione minimo di operazioni chiuse con risultato netto misurabile.
<!-- lesson -->
## 2026-09-27 00:01 UTC · slot 20260927T0000Z

- Ancora 0 trade chiusi: non esiste base statistica per modificare pesi, soglie o stop/TP; qualsiasi cambiamento ora sarebbe ottimizzazione sul rumore.
- Diverse posizioni esplorative sono aperte (BAT, POL, FIL, DOT, LINK) ma nessuna è ancora chiusa: attendere gli esiti (stop/TP/scadenza) prima di valutare se la soglia entry_threshold=0.80 è troppo restrittiva rispetto ai punteggi osservati (0.62-0.81).
- Il modello nota ripetutamente score vicini ma sotto soglia (0.76, 0.77, 0.81) senza mai raggiungere 0.80 pieno: utile tracciare quante di queste esplorazioni chiuderanno in profitto per capire se abbassare la soglia, ma servono almeno ~10 trade chiusi prima di agire.
- Nessun trade chiuso => nessuna attribuzione per segnale disponibile; mantenere invariati tutti i pesi finché non si osservano risultati reali collegati ai segnali positivi.
<!-- lesson -->
## 2026-09-26 22:31 UTC · slot 20260926T2230Z

- Non ci sono trade chiusi (0) né dati di attribuzione sui segnali: non c'è alcuna base statistica per modificare pesi o soglie, quindi non propongo cambiamenti in questa riflessione.
- Con 0 trade anche l'unico wake registrato è 'no_trade': prima di intervenire sui parametri serve osservare almeno una decina di trade chiusi per distinguere segnale da rumore.
- Mantenere i parametri attuali invariati fino a quando non si accumula un campione minimo di operazioni, per evitare di ottimizzare sul rumore.
