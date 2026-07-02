# AGENTS.md — Handoff & stato del progetto `regime-trader`

> **Per qualsiasi agent AI (Claude, Codex, Gemini, …) che apre questo progetto.**
> Questo file è la fonte di verità sullo stato del lavoro. Leggilo **per intero** prima
> di agire. Ultimo aggiornamento: **2026-06-10** (screener, ranking momentum,
> manutenzione stop, report di sessione).
>
> **📊 Leggi anche `reports/sessions/LATEST.md`**: è il report che il bot scrive
> automaticamente a ogni arresto (risultati, eventi, errori). Parti dalla sezione
> *Anomalie* per trovare errori o strategie mal funzionanti da correggere.

---

## ⚠️ REGOLE NON NEGOZIABILI (vincoli dell'utente)

1. **Rispondi SEMPRE in italiano** (messaggi e commenti nel codice).
2. **🔴 Git — AUTO-COMMIT (rafforzata il 2026-06-15, richiesta utente)**: **ogni
   modifica** (codice, config, doc) va **committata e pushata SUBITO e in automatico**,
   senza chiedere conferma — un commit per modifica logica, messaggi in italiano.
   (Prima, dal 2026-06-11, era "un commit a fine task".)
3. **`.env` e `config/credentials.yaml` NON vanno mai committati.** `*.example` sì.
   Questa regola resta ASSOLUTA anche col pieno potere di commit.
4. **NIENTE MARGINE**: il bot opera solo sul capitale proprio (`max_leverage: 1.0`,
   esposizione ≤ 80% dell'equity). L'esposizione non deve mai superare l'equity.
5. **NIENTE gap risk overnight**: modalità intraday con **square-off serale** obbligatorio
   (chiude tutto ~15 min prima della chiusura del mercato).
6. **Chiedi conferma** prima di operazioni rischiose (chiusura posizioni reali, ordini,
   eliminazione file, deploy). Per le operazioni ordinarie procedi.
7. L'utente è un **principiante** di trading: spiega i concetti, non dare per scontato nulla.

---

## COS'È IL PROGETTO

`regime-trader` è un bot di trading algoritmico in Python che usa un **Hidden Markov Model
(HMM)** per classificare il **regime di volatilità** del mercato (NON la direzione del prezzo)
e adattare l'allocazione di conseguenza.

**Filosofia di base:**
- L'HMM rileva l'**ambiente di volatilità** (bassa / media / alta), non prevede i prezzi.
- **SEMPRE LONG, MAI SHORT.** In alta volatilità riduce l'allocazione (95% → 60%), non
  inverte. Motivo: i rimbalzi a V sono violenti e shortare li perderebbe.
- Forward algorithm (inferenza filtrata) → niente look-ahead bias.
- Stack: `alpaca-py` (broker), `hmmlearn` (HMM), `pandas`/`numpy`, `rich` (dashboard).

**Conseguenza importante:** in un regime CRASH un sistema long-only **perde** — è inevitabile
per design. Il meglio che può fare è limitare le perdite (ridurre esposizione, stop loss,
square-off serale). NON è un bug.

---

## STATO ATTUALE (2026-06-10)

- **Modalità: INTRADAY** (timeframe `1Hour`, flat overnight). Migrato il 2026-06-09 da daily.
- **Paper trading** su Alpaca (account PAPER, feed dati **IEX** gratuito).
- **Account flat**: 0 posizioni, equity ~**$97.458** (tutto cash, nessun margine).
  - Partito da $100k; il grosso del drawdown viene dal rodaggio (bug poi corretti).
- **Test: 335 verdi** (`venv/bin/python -m pytest tests/ -q`).
- **🐛 FIX 2026-06-19 — NameError `client_order_id` nel retry MARKET**: il 18/06 il BUY
  DOG (LIMIT scaduto → retry a mercato) è crashato con `name 'client_order_id' is not
  defined` → entrata persa. Causa: il fix del 18/06 aveva aggiunto `client_order_id` a
  `place_limit/bracket/stop_order` ma **non** a `place_market_order`, che però usava la
  variabile a riga 268. **Fix**: aggiunto il parametro `client_order_id` a
  `place_market_order` (coerente con gli altri); `_cancel_and_retry_market` ora passa un
  id distinto `f"{trade_id}-m"` (evita la collisione "must be unique" col LIMIT cancellato
  e abilita il recupero idempotente). Test: `tests/test_orders.py::test_market_retry_passes_distinct_client_order_id`.
  Stesso bug latente trovato e fixato nel fork PM `~/repo/TradingSalvo` (non era mai
  esploso: opera su pochi simboli, mai un retry MARKET).
  - ⚠️ Nota sessione 17→18/06 serale: hang transitorio del main loop per **blackout di
    rete reale sulla macchina/WSL** (DNS giù, `Temporary failure in name resolution`,
    `CRITICAL Connessione API persa` alle 22:04 CEST). Avvenuto **dopo** lo square-off
    (conto già flat) → nessun danno. Lo square-off serale è scattato regolarmente (4
    posizioni chiuse alle 21:50 CEST). I bot si sono riavviati il 19/06 alle 07:12 CEST.
- **Repository Git attivo** (branch `main`): dal 2026-06-11 gli agenti hanno pieno
  potere di commit e push (mai `.env`). Remote: `github.com/Peppe098047/AlpacaTrades`
  (privato), push via deploy key dedicata `~/.ssh/id_ed25519_alpacatrades`
  (alias ssh `github.com-alpacatrades`, senza passphrase, scoped al solo repo).
- **Trade journal**: `reports/journal.md` (statistiche cumulative cross-sessione).
- **🐛 FIX 2026-06-18 — ingressi a mercato CHIUSO tenuti overnight**: il 17→18/06 il bot ha
  aperto 5 posizioni alle **00:01** (mezzanotte CEST = extended hours ET, sessione regolare
  chiusa) e le ha tenute ~9h overnight — contro il principio intraday/no-gap. Causa: il guard
  ingressi controllava solo `_squared_off_date == oggi`, che si azzera a mezzanotte; se lo
  stream consegna una barra in extended hours, il bot apriva posizioni. **Fix**: nuovo metodo
  `_market_open_now()` (clock Alpaca `is_open`) come PRIMO gate del blocco ingressi — niente
  BUY se la sessione regolare è chiusa. Stop/trailing/square-off restano attivi. Conservativo
  su errore clock (niente BUY). Test: `tests/test_intraday.py::TestMarketOpenGate` (4 test).
- **🐛 FIX 2026-06-18 — ordine fallito "client_order_id must be unique" (code 40010001)**:
  il 17/06 il BUY SPY fu rifiutato (id duplicato) coi retry futili → entrata persa. Causa:
  `_with_retry` rilancia la submission con lo STESSO client_order_id; se il 1° tentativo aveva
  già registrato l'ordine (risposta persa), il retry collide. **Fix**: `_submit_idempotent` in
  `alpaca_client.py` — su 40010001 recupera l'ordine esistente (`get_order_by_client_id`)
  invece di fallire/doppiare; 40010001 aggiunto ai codici non-retryable. Test:
  `tests/test_orders.py::TestSubmitIdempotent` (3 test).
- **🐛 FIX 2026-06-16 — HANG dopo riconnessione websocket (causa radice individuata)**:
  durante la sessione del 15/06 il bot principale si era **bloccato** (processo vivo ma main
  loop fermo) intorno alle 19:25: niente manutenzione stop (ogni 5 min) né pipeline oraria.
  Poco dopo (19:52) il trading stream ebbe un `keepalive ping timeout` con auto-restart, ma il
  main loop era già fermo. Le 5 posizioni erano protette dagli stop sul broker, ma lo
  **square-off serale rischiava di saltare**. Risolto sul momento con kill+riavvio.
  **Causa radice**: le chiamate REST sincrone nel main loop (`_poll_new_bars`,
  `_run_stop_maintenance`, `_process_all_symbols`) **non avevano timeout** → quando la rete si
  degrada (stesso evento del keepalive timeout del websocket) una `socket.recv` bloccante
  resta appesa all'infinito su un socket semi-aperto, congelando il loop. Il main loop in sé
  è robusto (timeout su `queue.get`): il blocco era *dentro* la chiamata di rete.
  **Fix (2 livelli)**: (1) `socket.setdefaulttimeout(network_timeout_seconds, default 30s)` in
  `main._startup` — rete di sicurezza globale che copre Alpaca REST e urllib (calendario/
  orologio); non tocca il websocket (socket asyncio non-bloccanti). (2) `_with_retry` in
  `alpaca_client.py` ora ritenta anche su `(RequestException, OSError)`, non solo `APIError`,
  così un timeout transitorio ritenta invece di propagarsi. Tutte le chiamate REST passano da
  `_with_retry`. Test di regressione in `tests/test_orders.py` (`TestAlpacaClientRetry`:
  `test_retries_on_network_error`, `test_raises_network_error_after_max_attempts`). Resta
  attivo anche l'**heartbeat** del monitor (allerta se `main.log` fermo >12 min a processo
  vivo) come difesa in profondità.
- **🐛 FIX 2026-06-15 — crash "can't compare offset-naive and offset-aware datetimes"**:
  in `_handle_bar_event` il timestamp del bar (stream Alpaca = timezone-aware UTC) veniva
  confrontato con `_last_pipeline_ts` che poteva essere naive (impostato da `_poll_new_bars`
  o dai fallback `datetime.now()`). Al primo mix naive/aware il confronto sollevava TypeError
  → "ARRESTO DA ERRORE". Fix: `bar_ts` normalizzato a **naive locale** (`.astimezone().replace(tzinfo=None)`),
  coerente con la convenzione naive del resto del bot. Test di regressione in
  `tests/test_live_cadence.py` (classe `TestTimezoneAwareBar`, 4 test). Stesso fix portato nel
  fork PM `~/repo/TradingSalvo`.
- **⚠️ LIMITE INFRA (2026-06-15)**: il feed dati **IEX gratuito consente 1 sola connessione
  streaming per login Alpaca**. I due paper account (principale + fork PM) sono sotto lo stesso
  login → **non si possono tenere accesi entrambi i bot insieme** (il 2° riceve
  `connection limit exceeded` all'infinito). Soluzioni: uno alla volta / piano dati a pagamento
  / login Alpaca separati. Tenuto acceso il principale.
- **Sessione 2026-06-12 (15:34–22:08): +0,35%** ($97.458) — regime STRONG_BULL, 5 ordini
  (SPY, MU, QQQ, IWM, INTC), square-off pulito, **nessuna anomalia**. 2ª sessione del
  periodo di osservazione. Unico rumore: websocket TradingStream caduto alle 22:07
  (mercato già chiuso), riconnesso da solo — benigno.
- **Sessione 2026-06-11 (15:34–21:48): +0,14%** ($97.116) — nessuna anomalia, 1ª sessione
  del periodo di osservazione.
- **4ª sessione paper (21:17–21:48): −0,02%** — SH comprato dal ranking, stop ampi
  (nessuno stop-out da rumore), square-off ok. L'auto-copertura SPY+SH vista in
  sessione è risolta dal fix n.13 (mutua esclusione).
- **Prima sessione paper live (2026-06-10 16:22–17:18)**: P&L −1,41% ($97.757), causato
  dal churn della pipeline a cadenza minuto (bug 10a-d, tutti corretti). Conto flat.
- Modello HMM addestrato su barre orarie: `models/hmm_model.pkl` (6 stati).
- **Nuovo dal 2026-06-10** (ora collaudato in 2 sessioni complete a mercato aperto):
  screener di liquidità, ranking momentum, manutenzione stop ogni 5 min,
  report automatico di fine sessione in `reports/sessions/`.

---

## ARCHITETTURA (moduli principali)

```
core/
  hmm_engine.py         HMM: BIC selection, forward inference, stability filter, flicker.
                        MIN_TRAIN_BARS = 504 (feature dopo warm-up). RegimeState, RegimeInfo.
  regime_strategies.py  3 strategie (Low/Mid/HighVol) + StrategyOrchestrator. Signal dataclass.
  screener.py           LiquidityScreener: all'avvio seleziona i top-N titoli per dollar
                        volume dall'universo ampio in config (lo spread è l'unico costo reale).
  ranking.py            MomentumRanker: a ogni ciclo classifica i titoli (momentum
                        risk-adjusted + relative strength vs SPY); solo i top-K ricevono BUY,
                        le posizioni aperte non vengono mai escluse (niente churn).
  risk_manager.py       VETO ASSOLUTO sui segnali. CircuitBreaker (lock file), PortfolioState,
                        PositionInfo (richiede campo stop_loss!), RiskDecision.
  signal_generator.py   TradingSignal (collega regime+risk). Skeleton, poco usato.
broker/
  alpaca_client.py      Wrapper alpaca-py. Feed IEX di default. get_bars usa .data (vedi GOTCHA).
                        place_stop_order, place_protective_stop, close_all_positions.
  order_executor.py     submit_order (LIMIT±0.1% poi market), place_protective_stop, modify_stop.
  position_tracker.py   Posizioni in-memory + sync con Alpaca + WebSocket fill.
data/
  market_data.py        MarketDataFeed: load_history, _fill_gaps (solo daily!), WebSocket bar/quote.
  feature_engineering.py FeatureEngineer: z-score rolling 252 (warm-up), ATR, EMA, returns.
backtest/
  backtester.py         Walk-forward. Applica slippage_pct ai fill. Skippa fold con IS<504.
  performance.py        Metriche (Sharpe, Sortino, CAGR, DD…), benchmark, export CSV.
  stress_test.py        Monte Carlo, crash injection, gap risk.
monitoring/
  logger.py             Logging JSON rotante (main/trades/alerts/regime.log). Campi riservati protetti.
  dashboard.py          Dashboard terminale rich. dashboard_state_from_dict().
  web_dashboard.py       Dashboard browser (http.server stdlib, zero dipendenze). Porta 8787.
  alerts.py             AlertManager: 7 trigger, rate limit, console/log/webhook/email.
  session_report.py     SessionReporter: report markdown a ogni arresto in reports/sessions/
                        (+ LATEST.md). SessionLogCapture cattura i WARNING+ per il report.
reports/sessions/       Report automatici di fine sessione (vedi README nella cartella).
main.py                 TradingSession (loop live), run_backtest/stress/dashboard, CLI.
start.sh                Launcher: train|dry|live|dashboard|web + menu interattivo.
scripts/set_stops_now.py Piazza stop di protezione sulle posizioni aperte (uso manuale d'emergenza).
```

---

## CONFIGURAZIONE CHIAVE (`config/settings.yaml`)

```yaml
broker:
  timeframe: "1Hour"          # intraday
  intraday: true              # square-off serale attivo
  square_off_minutes: 15      # chiude tutto 15 min prima della chiusura mercato
  stop_refresh_minutes: 5     # manutenzione trailing stop tra una barra e l'altra
  symbols: [SPY, QQQ, ...]    # FALLBACK se lo screener è disattivato o fallisce
screener:
  enabled: true               # selezione automatica titoli all'AVVIO (non a metà sessione)
  top_n: 10                   # titoli operativi per la sessione
  min_dollar_volume: 200000000  # minimo $200M/giorno di scambi (spread bassi)
  always_include: [SPY]       # SPY sempre primo: termometro HMM + benchmark ranking
  universe: [~50 large cap]   # lista completa in settings.yaml
ranking:
  enabled: true               # a ogni ciclo solo i top-K possono ricevere BUY
  lookback_bars: 20           # momentum su ~3 giorni di barre orarie
  top_k: 5                    # = max_concurrent
  benchmark: SPY
risk:
  max_exposure: 0.80          # esposizione max (% equity) → niente margine
  max_leverage: 1.0           # SOLO capitale proprio
  max_single_position: 0.15   # max 15% per singolo titolo
  ...
```

**Come interagiscono regime, screener e ranking:** l'HMM (su SPY) decide *quanto*
investire (95%/60%); lo screener decide *su quale universo* lavorare (titoli liquidi,
solo all'avvio); il ranking decide *quali* titoli comprare a ogni ciclo (top-5 momentum).
Il `risk_manager` resta il veto finale. L'uscita dalle posizioni è SOLO di trailing
stop e square-off: il ranking non vende mai (evita churn e costi spread).

Credenziali in `.env`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER=true`,
opzionale `ALPACA_FEED=iex` (default), `ALERT_WEBHOOK_URL`, `ALERT_EMAIL`.

---

## COME SI USA

```bash
# SEMPRE usare l'interprete del venv (NON esiste `python` di sistema):
venv/bin/python ...

# Launcher (preferito):
./start.sh train      # addestra l'HMM (forza retrain) ed esce
./start.sh dry        # pipeline completa SENZA ordini reali
./start.sh live       # trading paper (attende l'apertura, square-off serale)
./start.sh dashboard  # dashboard nel terminale
./start.sh web        # dashboard browser → http://localhost:8787
./start.sh            # menu interattivo

# Backtest intraday con costi (le finestre vanno aumentate per via di MIN_TRAIN_BARS=504):
venv/bin/python main.py backtest --symbols SPY --start 2025-07-01 --end 2026-06-01 \
  --timeframe 1Hour --train-window 756 --test-window 252 --step 252 --slippage 0.0001
```

**Orari mercato USA in ora italiana:** apertura **15:30**, chiusura **22:00** (6h di differenza;
nelle ~2 settimane di sfasamento DST a marzo/ottobre diventa 14:30–21:00). Square-off ~21:45 IT.

**Uptime:** con scheduler locale serve PC acceso + WSL attivo durante la sessione. In intraday,
se il PC è spento alle 21:45 lo square-off NON avviene (ma gli stop sul broker proteggono).
Per il live serio → VPS cloud sempre acceso (~5€/mese). Codice identico, basta copiare la cartella.

---

## LAVORI FATTI IL 2026-06-10 (attivi al prossimo riavvio del bot)

1. **Test di regressione casi-crash** (`tests/test_crash_regression.py`): stop SEMPRE
   sotto il prezzo in regime crash, verificato su strategie, `_execute_signal`
   (fill sotto lo stop → stop a fill×0.99) e `_update_trailing_stops`.
2. **Manutenzione stop ogni 5 min** (`_run_stop_maintenance` + `stop_refresh_minutes`):
   tra una barra oraria e l'altra il bot aggiorna i trailing stop coi prezzi correnti
   (`get_latest_bar`), ricrea stop broker mancanti e aggiorna lo snapshot dashboard.
   NON genera nuovi segnali: le decisioni restano sulla barra chiusa. È la risposta
   "a basso rischio" alla richiesta di stop più reattivi senza migrare a barre 15Min.
3. **Screener di liquidità** (`core/screener.py` + sezione `screener` in settings):
   all'avvio scarica 20 giorni daily dell'universo (~50 large cap) e seleziona i
   top-10 per dollar volume medio. Fail-safe: su errore usa `broker.symbols`.
4. **Ranking momentum** (`core/ranking.py` + sezione `ranking`): punteggio
   0.6×z(momentum/ATR%) + 0.4×z(relative strength vs SPY) su 20 barre; solo i top-5
   possono ricevere BUY. Posizioni aperte mai escluse. Fail-open su dati insufficienti.
5. **Report di sessione automatico** (`monitoring/session_report.py`): a ogni arresto
   scrive `reports/sessions/session_<data>.md` + `LATEST.md` con risultati, anomalie,
   regimi, ordini, eventi e tutti i WARNING+ catturati in memoria. Non blocca mai
   lo shutdown. CLAUDE.md e AGENTS.md rimandano a questi report.
6. **Dry-run utilizzabile dall'apertura**: rimosso il vincolo `hour >= 16` in
   `_poll_new_bars` (residuo della modalità daily) e primo polling immediato:
   la pipeline dry-run ora scatta subito dopo l'avvio, anche alle 15:30 IT.
7. **Fix polling intraday "Errore polling barre: 'code'"** (trovato dal PRIMO report di
   sessione, dry-run 15:31–15:50): doppio bug — `_bars_to_load` sommava +1 giorno
   all'ultima barra (ok daily, ma intraday chiedeva dati dal FUTURO → Alpaca "end
   should not be before start") e `is_non_retryable_api_error` mascherava l'errore
   vero perché `.code` di alpaca-py è una property che solleva KeyError se il JSON
   d'errore non ha "code" (getattr non protegge dalle property). Effetto: pipeline
   MAI eseguita in dry-run. Fix in `data/market_data.py` + `broker/api_errors.py`,
   regressione in `tests/test_incremental_update.py`, verificato live a mercato aperto.
8. **Tetto di esposizione TOTALE legato al regime** (`_effective_max_exposure` in
   `main.py`): il budget del ciclo ora usa min(`risk.max_exposure`, allocazione di
   regime × leva cappata). In CRASH il 60% vale per il PORTAFOGLIO intero (prima:
   15%×5 = 75%); in low-vol vince ancora l'80% di sicurezza; in incertezza il tetto
   si dimezza col sizing. Stesso tetto usato dalla dashboard. Test in
   `tests/test_regime_exposure_cap.py`. Richiesto dall'utente prima del paper live.
9. **Restyling dashboard web** (solo `_HTML_PAGE`, server e contratto dati invariati):
   sparkline equity costruita lato client, anello di confidenza del regime, icone SVG
   al posto delle emoji, numeri in monospace, stato connessione (verde/stantio/down),
   stati vuoti/offline curati. Zero dipendenze come prima.
10. **Fix dalla PRIMA SESSIONE PAPER LIVE (16:22–17:18, P&L −1,41%, 79 ordini in 55 min!)**
   — quattro bug visibili SOLO con fill e stop veri (`tests/test_live_cadence.py`):
   a. **Gate orario sulle barre-minuto** (`_handle_bar_event`): lo stream Alpaca invia
      barre da 1 MINUTO → la pipeline girava 60×/ora. Ora scatta solo alla prima
      barra-minuto di una nuova ora, dopo refresh REST della cache (`data_feed.update`).
   b. **Cache stantia**: lo stream mergiava barre-minuto nella cache oraria (corrompendo
      ATR/EMA) e `pd.concat` ricrea i DataFrame → la sessione vedeva prezzi congelati
      all'avvio. Merge ora solo se `timeframe == "1Min"`; la sessione ricarica da update().
   c. **Cooldown anti-churn post-vendita** (`risk.reentry_cooldown_minutes`, default 30):
      dopo stop-out il bot ricomprava in ~26s (loop stop-out→ricompra in mercato in
      discesa). Ora nuovi ingressi bloccati N minuti dopo ogni vendita; top-up esclusi.
      In più: niente ribilanciamento sotto l'1% di scarto dal target (top-up da 1 azione).
   d. **Trailing coi prezzi vivi**: il guard "stop < prezzo" usava il close stantio della
      cache → stop sopra il mercato → errori Alpaca 42210000 a raffica. Ora il trailing
      scarica sempre i prezzi correnti. E `close_all_positions` non esplode più con
      KeyError 'id' se Alpaca non crea l'ordine per un simbolo (shutdown robusto).
11. **Modifiche post-analisi 3ª sessione (17:28–19:49, −0,77%, 10/10 stop-out)** —
   approvate dall'utente, attive al prossimo riavvio:
   a. **Cap dello stop coerente col regime**: il cap "stop sotto il prezzo" usa il
      moltiplicatore della strategia (`EMA_STOP_MULT`) invece dello 0,5 fisso.
      High-vol → `price − 1,0×ATR` (in crash il vecchio cap produceva stop da
      −0,3/−0,5% nel regime più rumoroso → stop-out garantito entro l'ora).
      Stesso criterio nel trailing (`price − atr_mult×ATR`). Low/Mid invariati.
   b. **ETF inversi 1x nell'universo**: SH (inverso S&P 500) in `always_include`,
      PSQ (inverso Nasdaq) in universo. Esposizione bearish restando LONG, senza
      short né margine: nei crash il ranking momentum li seleziona da solo. SOLO
      inversi 1x — MAI SQQQ/SDS (leva strutturale, contro `max_leverage 1.0`).
      Square-off serale = niente decay multi-giorno. Solo config, zero nuovo codice.
   c. **Report fedele**: i BUY skippati (wash-trade) non vengono più conteggiati
      tra gli "Ordini inviati" del report di sessione.
12. **Fix dalla 4ª sessione (21:17–21:48, −0,02%)** (`tests/test_squareoff_race.py`):
   a. **Race square-off ↔ trailing**: lo square-off cancella gli stop per vendere;
      la manutenzione trailing li vedeva "mancanti" e li RIPIAZZAVA, sequestrando
      le azioni in vendita (40310000 "insufficient qty"; su SH lo stop spurio è
      stato creato davvero — ripulito poi dallo shutdown). Ora il trailing è
      disattivato dopo lo square-off del giorno e durante lo shutdown.
   b. **`ClosePositionResponse.body`**: alpaca-py espone l'ordine creato nel campo
      `body`, non `order` → ogni chiusura risultava "nessun ordine creato (status
      200)" pur riuscendo. Parsing corretto (prova `body`, poi `order`).
   Verificato su Alpaca a fine sessione: conto flat, 0 ordini appesi.
13. **Mutua esclusione benchmark ↔ ETF inverso** (`ranking.inverse_pairs`: SH↔SPY,
   PSQ↔QQQ): nella 4ª sessione il bot era long su SPY e SH insieme (~15%+15% che
   si annullano = due spread pagati per esposizione netta ~0). Ora nel ranking:
   se uno dei due è in portafoglio l'altro non riceve BUY; se entrambi sono nel
   top-K sopravvive solo il punteggio migliore e lo slot va al titolo successivo.
   Test in `tests/test_screener_ranking.py`.
14. **Quattro migliorie "pensare in grande"** (`tests/test_journal_telegram_blackout.py`):
   a. **Git**: la cartella `.git` esisteva ma era VUOTA (init fallito) — repository
      inizializzato per davvero, branch `main`, commit iniziale, `.gitignore` esteso
      (snapshot, lock, results/, *.pkl, cartelle agent). Dal 2026-06-11 gli agenti
      hanno pieno potere di commit/push (vedi regole non negoziabili).
   b. **Trade journal aggregato** (`monitoring/trade_journal.py` → `reports/journal.md`):
      SessionReporter scrive anche un JSON per sessione; il journal li aggrega in
      P&L cumulativo, win rate sessioni, ordini, stop-out, errori. Rigenerato a ogni
      fine sessione; a mano: `venv/bin/python -m monitoring.trade_journal`.
      Le decisioni di strategia vanno prese su QUESTI numeri, non sull'ultima sessione.
   c. **Telegram** (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in .env): quinto canale
      dell'AlertManager + riepilogo automatico di fine sessione (P&L, ordini, durata,
      tipo di shutdown). L'UTENTE deve creare il bot con @BotFather e mettere le due
      variabili nel .env — finché mancano, il canale è silenziosamente disattivo.
   d. **Blackout d'apertura** (`broker.no_entry_first_minutes: 30`): niente BUY nei
      primi 30 min dalla campanella (9:30 ET, fuso gestito da zoneinfo) — spread
      larghi e movimenti falsi. Stop, trailing e square-off restano attivi.
15. **Calendario macro Forex Factory** (`data/economic_calendar.py`, sezione
   `calendar` in settings, `tests/test_economic_calendar.py`): blackout ingressi
   ±15 min attorno agli annunci USD ad alto impatto (CPI, NFP, FOMC...). Feed
   gratuito `nfs.faireconomy.media/ff_calendar_thisweek.json` (serve User-Agent
   da browser: con quello di default Python risponde 403!), cache su disco in
   `data/cache/`, fail-open se mancano feed e cache. Richiesto dall'utente
   ("trade più studiati"); il CPI del 2026-06-10 spiega il crash→rally di quel
   giorno — con questo filtro il bot non comprerà mai a ridosso del dato.
16. **Copertura bearish completa**: RWM (inverso Russell 2000) e DOG (inverso Dow)
   aggiunti a universo e `always_include` (da soli non passerebbero il filtro
   dollar volume); `top_n` 10→12 (4 fissi: SPY+SH+RWM+DOG, +8 azioni); coppie
   `RWM↔IWM` e `DOG↔DIA` in `inverse_pairs`. Nei ribassi il ranking sceglie
   l'inverso più forte (le discese non sono uniformi). Verificati 1000 barre
   orarie IEX per entrambi. ULTIMA modifica prima del periodo di osservazione.
17. **Fill su Telegram** (gap fix in freeze, NON tocca la logica di trading): gli
   eventi BUY/SELL/CLOSE/STOP passano da `_log_event` anche all'AlertManager
   (`trade_event`, AlertType.TRADE) ed escono su Telegram/webhook. SKIP/REJECT
   esclusi (rumore). Il rate limit NON si applica ai trade (i fill consecutivi
   devono arrivare tutti). Il messaggio di benvenuto prometteva i fill ma non
   erano mai stati collegati — ora la promessa è mantenuta.

---

## 🧊 PERIODO DI OSSERVAZIONE (dal 2026-06-11) — NON aggiungere feature!

Concordato con l'utente: il bot gira COSÌ COM'È per 3-5 sessioni complete per
popolare il trade journal con dati confrontabili. Solo bug fix se i report
mostrano errori. Le prossime feature (VPS, earnings calendar, take profit
parziale, "non inseguire") partono DOPO, sulla base dei numeri del journal.

**Lista d'attesa per fine freeze** (applicare in blocco, in un punto preciso
della storia del journal):
- PM (Philip Morris) nell'universo screener (richiesta utente 2026-06-11).

⚠️ Screener e ranking NON sono ancora stati validati a mercato aperto né in backtest:
prima sessione utile = osservare il report e confrontare con il comportamento atteso.

---

## FIX FATTI IL 2026-06-09 — non re-introdurli!

1. **Feed IEX, non SIP** (`alpaca_client.py`): il free tier Alpaca non ha SIP. Default `DataFeed.IEX`.
2. **BarSet `.data`**: `'SYM' in bars` è sempre False sul BarSet → leggere `bars.data[sym]`.
3. **Timestamp daily normalize** (`market_data.py`): Alpaca timestampa le barre daily alle 04:00 UTC;
   `_fill_gaps` fa reindex su business-days → solo per `1Day`, NON per intraday (distruggerebbe i dati).
4. **Sizing progressivo + no leva** (`main.py _execute_signal` + loop): budget esposizione =
   `80%×equity − già_impegnato`; leva forzata a `max_leverage`; cap 15% per posizione. Niente margine.
5. **Stop loss SEMPRE sotto il prezzo**: in crash `EMA50 − ATR` finisce sopra il prezzo (prezzo
   crollato sotto la media). Corretto in: tutte le strategie (`min(stop, price − 0.5×ATR)`),
   `_execute_signal` (abbassa a fill×0.99), `_update_trailing_stops` (cap + skip se ≥ prezzo).
6. **`PositionInfo` richiede `stop_loss`**: `_build_portfolio_state` ora lo passa (`pos.stop_level`).
7. **`close_all_positions`**: Alpaca ritorna `ClosePositionResponse` (non `Order`) → gestito.
8. **JsonFormatter**: i campi di sistema (level/message/…) sono protetti dall'override del context.
9. **Square-off serale** (`_check_eod_closeout`): chiude tutto vicino alla chiusura, no riapertura.
10. **`--train-only` forza il retrain** (prima caricava il modello esistente).
11. **Dedup fill + sync live**: lo stesso fill può arrivare sia da polling ordine sia da
    TradingStream; ora `PositionTracker.record_fill(..., fill_id=order_id)` lo conta una volta.
    Ogni ciclo live fa anche `tracker.sync()` prima del risk, così l'esposizione torna ai dati
    broker se lo stato RAM è sporco.
12. **Stop mancanti ricreati**: se il trailing stop non trova uno stop order aperto per una
    posizione, piazza un nuovo stop protettivo sotto il prezzo invece di limitarsi al warning.
13. **Shutdown volontario robusto**: `_shutdown_seq` ora cancella ordini pendenti, chiude,
    sincronizza con Alpaca e ritenta finché il tracker risulta flat o scade il timeout.
14. **Target rebalance operativo**: il loop live confronta i pesi correnti col target dopo cap
    reale (`max_single_position`, `max_leverage`), non col 60% teorico della strategia CRASH.
    Evita buy inutili/reject quando una posizione è già vicina al 15%.
    Inoltre skippa prima del risk i nuovi simboli se `max_concurrent` è già raggiunto/superato.
15. **Recovery stop più visibile**: se manca uno stop broker, il trailing prova a ripiazzarlo;
    se fallisce ora logga `ERROR`/`WARNING`, non solo debug.
16. **Dashboard più operativa**: snapshot e dashboard mostrano ora titoli valutati nell'ultimo
    ciclo, stato decisionale, variazione prezzo, peso corrente, target operativo e stop attivo.
    Le chiusure/fill SELL da Alpaca vengono registrate negli eventi dashboard.
17. **Wash-trade Alpaca evitato**: prima di un BUY live il bot verifica se esiste già uno stop
    SELL aperto sul simbolo; in quel caso skippa il BUY e logga un evento `SKIP`. Gli errori
    Alpaca non temporanei (40310000 wash trade, 422 validazione stop) non vengono più ritentati.

---

## 🐞 BUG NOTI / LAVORI IN SOSPESO (priorità per il prossimo agent)

1. **[ALTA→FATTO 2026-06-10] Validazione a mercato aperto COMPLETATA**: secondo dry-run
   (15:57–16:15) tutto verde — regime CRASH, ranking top-5 con SKIP corretti, stop tutti
   sotto il prezzo, sizing nei cap (14,8%/titolo, 74% totale), zero errori. **OK dato
   per il paper live.**
1b. **[FATTO 2026-06-10] Esposizione totale in CRASH cappata al 60%**: l'allocazione
   di regime ora vale come tetto TOTALE del portafoglio (fix n.8). Decisione presa
   dall'utente prima di partire col paper live.
2. **[MEDIA] Coordinamento sizing live ↔ risk_manager**: il cap di esposizione è applicato sia nel
   loop di `main.py` (budget) sia in `risk_manager.validate_signal`. Funziona ma è ridondante;
   valutare di unificarlo.
3. **[BASSA] Scheduler/VPS**: per uptime affidabile (richiesto dall'utente per il live).
4. **[BASSA] Take profit**: assente per design (le strategie generano `take_profit=None`).
   L'utente sa che il trailing stop lo sostituisce. Aggiungerlo solo se l'utente lo richiede.

**❌ DECISIONE CHIUSA (2026-06-10) — Ipotesi A2 (decisioni a 15Min) SCARTATA coi numeri:**
backtest walk-forward SPY a parità di costi (0,01%/trade) → 1Hour: 172 trade, lordo +2,42%,
**netto +1,73%**, Sharpe 0,20 | 15Min: 908 trade (5,3×), lordo +4,39% ma $3.748 di costi,
**netto +0,64%**, Sharpe 0,03, MaxDD −8,8% vs −6,8%. Il 15Min reagisce meglio (lordo più
alto) ma i costi mangiano 3,75 punti e il netto crolla. E SPY è il caso MIGLIORE (spread
minimo): sulle singole azioni sarebbe peggio. Le decisioni restano su 1Hour; gli stop
reattivi sono già coperti dalla manutenzione ogni 5 min. Non riaprire senza nuovi numeri.

---

## GOTCHA TECNICI (errori già incontrati)

- **`venv/bin/python`** sempre: `python` non esiste sul sistema (WSL). Per i comandi:
  `export PATH="$HOME/.local/bin:$PATH"` (g++ per hmmlearn).
- **`pkill` nei comandi Bash** causa exit 144 (il segnale colpisce la shell): non usarlo nello
  stesso comando dei test; killa i processi separatamente.
- **`trading_halted.lock`** nella root → il circuit breaker va in PEAK_HALT e blocca tutto.
  Si crea su drawdown dal picco > 10%. Se spurio (drawdown reale basso), rimuoverlo. I test devono
  isolare il lock file in `tmp_path` (vedi `tests/test_risk.py`, `tests/test_sizing.py`).
- **IEX storia intraday**: barre orarie disponibili solo da ~metà 2025. Daily molto più indietro.
- **Backtest intraday**: `--train-window` DEVE essere ≥ 504 (MIN_TRAIN_BARS) o tutti i fold vengono
  skippati ("dati insufficienti").
- **Mercato chiuso**: le quote bid/ask sono inaffidabili (spread apparenti 5-11%). Misurare gli
  spread reali solo a mercato aperto.

---

## NUMERI DI RIFERIMENTO (costi/performance, backtest SPY orario lug2025–giu2026)

| Scenario spread | Costo | Rendimento lordo | Rendimento netto |
|-----------------|-------|------------------|------------------|
| 1Hour, conservativo 0,05% | $3.459 | +2,39% | −1,06% |
| 1Hour, realistico SPY 0,01% | $694 | +2,42% | +1,73% |
| 15Min, realistico SPY 0,01% | $3.748 | +4,39% | +0,64% |

→ L'intraday è sostenibile solo con spread bassi (titoli liquidi). Più trade = più erosione.
Alpaca è commission-free su azioni US: l'unico costo è lo spread.
→ Il confronto 1Hour vs 15Min (2026-06-10) conferma: più frequenza = più lordo ma molto
meno netto. Decisioni su 1Hour, stop aggiornati ogni 5 min dalla manutenzione.

---

## COME LAVORARE SU QUESTO PROGETTO

1. Leggi questo file + `CLAUDE.md` globale (lingua italiana, no commit automatici)
   + **`reports/sessions/LATEST.md`** (report automatico dell'ultima sessione del bot).
2. Prima di modifiche: `venv/bin/python -m pytest tests/ -q` deve essere verde (321 test).
3. Dopo ogni modifica al flusso live, ricorda che il bot **in esecuzione ha il codice in RAM**:
   i fix si attivano solo al **riavvio** (`Ctrl+C` + `./start.sh live`).
4. I bug del flusso live (crash, fill, stop) emergono **solo col mercato aperto** — i test con
   dati statici non li intercettano. Testare anche con `--dry-run` a mercato aperto.
5. **REGOLA (richiesta dall'utente il 2026-06-10): alla fine di OGNI task completato
   aggiorna SEMPRE i file di riferimento per gli agenti AI** — questo `AGENTS.md`
   (fix, stato, conteggio test), il `CLAUDE.md` di progetto se cambiano i promemoria
   rapidi, e i file memory in `~/.claude/memory/`. Nessun task è "finito" finché
   la documentazione non riflette il nuovo stato.
