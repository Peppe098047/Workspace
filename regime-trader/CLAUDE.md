# regime-trader — istruzioni di progetto

👉 **Leggi prima `AGENTS.md`**: è la fonte di verità completa sullo stato del progetto,
l'architettura, i vincoli dell'utente, i fix recenti, i bug noti e i lavori in sospeso.

👉 **Poi leggi `reports/sessions/LATEST.md`**: è il report automatico dell'ultima
sessione del bot (risultati, eventi, errori, anomalie). Parti dalla sezione
*Anomalie* per individuare errori o strategie mal funzionanti da correggere.

## Promemoria rapidi (dettagli in AGENTS.md)

- Rispondi **sempre in italiano**.
- **🔴 Git — AUTO-COMMIT (rafforzata il 2026-06-15, richiesta utente)**: **ogni
  modifica** (codice, config, doc) va **committata e pushata SUBITO e in automatico**,
  senza chiedere conferma — un commit per modifica logica, messaggio in italiano.
  Resta assoluto: **mai committare `.env`** o altri segreti. Repo: `AlpacaTrades`,
  branch `main` (attivo dal 2026-06-10).
- Usa **`venv/bin/python`** (non esiste `python` di sistema).
- Prima di modifiche: `venv/bin/python -m pytest tests/ -q` deve restare verde (335 test).
- **🧊 Periodo di osservazione (dal 2026-06-11)**: NON aggiungere feature — solo
  bug fix dai report. Il bot deve girare invariato per 3-5 sessioni (journal).
- Il bot è **intraday, no margine, no gap overnight** (square-off serale).
- L'utente è principiante di trading: spiega i concetti.
- I fix al flusso live si attivano solo al **riavvio** del bot.

## ⚠️ REGOLA: documentazione a fine task (richiesta dall'utente)

**Alla fine di OGNI task completato, aggiorna SEMPRE i file di riferimento per
gli agenti AI** prima di considerare il lavoro finito:

1. `AGENTS.md` — fix applicati, stato del progetto, conteggio test;
2. questo `CLAUDE.md` — se cambiano promemoria rapidi o conteggio test;
3. i file memory in `~/.claude/memory/` (progetti, decisioni, todo).

Nessun task è "finito" finché la documentazione non riflette il nuovo stato.

## Novità del 2026-06-10

- **Report di sessione automatico**: a ogni arresto il bot scrive
  `reports/sessions/session_<data>.md` (+ copia `LATEST.md`).
- **Manutenzione stop ogni 5 min** (`stop_refresh_minutes`): trailing stop
  aggiornati coi prezzi correnti tra una barra oraria e l'altra.
- **Screener di liquidità** (`core/screener.py`): all'avvio seleziona i top-10
  titoli per dollar volume da un universo di ~50 large cap.
- **Ranking momentum** (`core/ranking.py`): a ogni ciclo solo i top-5 per
  momentum risk-adjusted + relative strength vs SPY possono ricevere BUY.
- **Test di regressione casi-crash**: stop sempre sotto il prezzo
  (strategie + trailing + execute).

## Lavori in sospeso prioritari (vedi AGENTS.md per dettagli)

1. Coordinare meglio sizing live e `risk_manager` (oggi entrambi applicano cap/limiti).
2. Scheduler/VPS per uptime affidabile durante tutta la sessione USA.

Ipotesi A2 (decisioni a 15Min) **scartata il 2026-06-10 coi numeri**: netto +0,64%
vs +1,73% dell'orario a parità di costi. Dettagli in AGENTS.md.

## Fix live completati il 2026-06-09

- Dedup fill polling/TradingStream e sync posizioni prima del risk.
- Stop broker mancanti ricreati dal trailing, con log visibile se il ripristino fallisce.
- Shutdown volontario con retry fino a conto flat.
- Rebalance basato sul target operativo reale, non sul 60% teorico della strategia CRASH.
- Dashboard web/terminale più operativa: titoli valutati, stato decisionale, variazione prezzo,
  peso corrente, target, stop attivo ed eventi BUY/SELL/CLOSE/STOP/SKIP.
- Wash-trade Alpaca evitato: skip BUY se esiste già uno stop SELL aperto; errori 403/422 non
  vengono più ritentati inutilmente.
