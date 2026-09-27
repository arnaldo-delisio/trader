# Lezioni

Scritte dalla riflessione ogni 6 ore, la più recente in alto. Il modello le rilegge a ogni risveglio.

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
