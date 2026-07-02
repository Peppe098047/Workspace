# Report sessione — 2026-06-19 17:12

> Report generato automaticamente alla chiusura del bot.
> Per agenti AI: leggere PRIMA la sezione *Anomalie* e i warning/errori.

## Quadro generale

| | |
|---|---|
| Inizio sessione | 2026-06-18 16:17:48 |
| Fine sessione | 2026-06-19 17:12:31 |
| Durata | 1 day, 0:54:43 |
| Modalità | PAPER |
| Timeframe | 1Hour |
| Shutdown | volontario (pulito) |
| Simboli operativi | SPY, SH, RWM, DOG, NVDA, MU, QQQ, AAPL, AMZN, INTC, IWM, AVGO |

## Risultati

| | |
|---|---|
| Equity iniziale | $95,759.33 |
| Equity finale | $95,913.61 |
| P&L sessione | $+154.28 (+0.16%) |
| Picco equity | $100,624.81 |
| Drawdown dal picco a fine sessione | 4.68% |
| Ordini inviati | 9 |

## Anomalie

- 4 log di livello ERROR/CRITICAL durante la sessione (vedi sotto).

## Cambi di regime

| Ora | Da | A | Confidenza |
|---|---|---|---|
| 10:03:52 | STRONG_BEAR | EUPHORIA | 100% |

## Ordini inviati

| Ora | Simbolo | Direzione | Azioni | Entry | Stop | Regime |
|---|---|---|---|---|---|---|
| 16:20:04 | MU | LONG | 6 | 1118.26 | 1018.05 | STRONG_BEAR |
| 16:20:08 | AAPL | LONG | 47 | 299.25 | 296.22 | STRONG_BEAR |
| 16:20:11 | AVGO | LONG | 31 | 409.47 | 389.29 | STRONG_BEAR |
| 16:20:15 | SH | LONG | 431 | 33.31 | 33.24 | STRONG_BEAR |
| 16:20:16 | RWM | LONG | 698 | 13.79 | 13.74 | STRONG_BEAR |
| 17:01:05 | INTC | LONG | 54 | 131.00 | 119.29 | STRONG_BEAR |
| 18:01:05 | DOG | LONG | 651 | 22.04 | 22.00 | STRONG_BEAR |
| 20:01:06 | SH | LONG | 431 | 33.26 | 33.20 | STRONG_BEAR |
| 20:01:09 | DOG | LONG | 651 | 22.03 | 21.99 | STRONG_BEAR |

## Eventi (BUY / SELL / CLOSE / STOP / SKIP / REJECT)

| Ora | Tipo | Simbolo | Dettaglio |
|---|---|---|---|
| 17:01:04 | SKIP | AMZN, DOG, IWM, NVDA, QQQ, RWM, SPY | fuori dal ranking momentum top-K |
| 17:01:05 | SKIP | MU | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 17:01:09 | BUY | INTC | 54 az @ 130.92 |
| 17:01:10 | STOP | INTC | stop @ 119.29 |
| 17:01:10 | SKIP | AVGO | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 17:44:49 | CLOSE | AAPL | 47 az @ 296.21 |
| 18:01:04 | SKIP | AAPL, AMZN, IWM, NVDA, QQQ, RWM, SPY | fuori dal ranking momentum top-K |
| 18:01:05 | SKIP | MU | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 18:01:05 | SKIP | INTC | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 18:01:05 | SKIP | AVGO | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 18:01:10 | BUY | DOG | 651 az @ 22.04 |
| 18:01:10 | STOP | DOG | stop @ 22.00 |
| 19:05:32 | CLOSE | SH | 431 az @ 33.23 |
| 19:16:45 | CLOSE | DOG | 651 az @ 21.99 |
| 20:01:05 | SKIP | MU | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 20:01:05 | SKIP | INTC | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 20:01:06 | SKIP | AVGO | stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca |
| 20:01:08 | BUY | SH | 431 az @ 33.28 |
| 20:01:09 | STOP | SH | stop @ 33.20 |
| 21:35:06 | SKIP | AAPL, AMZN, DOG, IWM, NVDA, RWM, SPY | fuori dal ranking momentum top-K |
| 21:49:07 | CLOSE | AVGO | square-off serale |
| 21:49:27 | CLOSE | INTC | square-off serale |
| 21:49:47 | CLOSE | MU | square-off serale |
| 21:50:07 | CLOSE | SH | square-off serale |
| 21:54:26 | CLOSE | MU | 6 az @ 1147.22 |
| 21:54:26 | CLOSE | INTC | 54 az @ 134.54 |
| 21:54:26 | CLOSE | AVGO | 31 az @ 411.06 |
| 21:54:26 | CLOSE | SH | 431 az @ 33.26 |
| 22:05:15 | SKIP | * | mercato chiuso: niente BUY |
| 10:03:53 | SKIP | * | mercato chiuso: niente BUY |

## Warning ed errori della sessione

- `16:17:50` **WARNING** — Download calendario fallito: <urlopen error [Errno -5] No address associated with hostname> — provo la cache.
- `17:01:05` **WARNING** — SKIP MU: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `17:01:10` **WARNING** — SKIP AVGO: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `18:01:05` **WARNING** — SKIP MU: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `18:01:05` **WARNING** — SKIP INTC: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `18:01:05` **WARNING** — SKIP AVGO: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `20:01:05` **WARNING** — SKIP MU: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `20:01:05` **WARNING** — SKIP INTC: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `20:01:06` **WARNING** — SKIP AVGO: stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca
- `20:01:40` **ERROR** — Errore esecuzione ordine DOG: name 'client_order_id' is not defined
- `20:53:57` **WARNING** — Errore di rete (tentativo 1/3): ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer')) — retry in 1.0s
- `21:35:01` **WARNING** — Errore di rete (tentativo 1/3): ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer')) — retry in 1.0s
- `21:42:03` **WARNING** — Errore di rete (tentativo 1/3): ('Connection aborted.', RemoteDisconnected('Remote end closed connection without response')) — retry in 1.0s
- `21:42:24` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `21:42:46` **WARNING** — Impossibile verificare stop aperti per AVGO: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `21:43:06` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `21:43:27` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `21:43:49` **ERROR** — Stop broker mancante per AVGO ma ripristino fallito: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `21:44:09` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `21:44:30` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `21:44:53` **WARNING** — Errore trailing stop INTC: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `21:45:13` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `21:45:34` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `21:45:56` **WARNING** — Errore trailing stop MU: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `21:46:16` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `21:46:27` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `21:46:49` **WARNING** — Errore trailing stop SH: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/orders?status=open (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `21:47:09` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `21:47:30` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `21:47:52` **WARNING** — Impossibile leggere l'orologio del mercato: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `21:48:12` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `21:48:33` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `21:48:49` **WARNING** — Impossibile leggere l'orologio del mercato: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `21:49:27` **WARNING** — Invio Telegram fallito: <urlopen error [Errno -3] Temporary failure in name resolution>
- `21:49:47` **WARNING** — Invio Telegram fallito: <urlopen error [Errno -3] Temporary failure in name resolution>
- `21:50:07` **WARNING** — Invio Telegram fallito: <urlopen error [Errno -3] Temporary failure in name resolution>
- `21:50:27` **WARNING** — Invio Telegram fallito: <urlopen error [Errno -3] Temporary failure in name resolution>
- `22:01:20` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='data.alpaca.markets', port=443): Max retries exceeded with url: /v2/stocks/bars?start=2026-06-18T19%3A00%3A00%2B00%3A00&timeframe=1Hour&feed=iex&symbols=SPY&limit=10000 (Caused by NameResolutionError("HTTPSConnection(host='data.alpaca.markets', port=443): Failed to resolve 'data.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `22:01:41` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='data.alpaca.markets', port=443): Max retries exceeded with url: /v2/stocks/bars?start=2026-06-18T19%3A00%3A00%2B00%3A00&timeframe=1Hour&feed=iex&symbols=SPY&limit=10000 (Caused by NameResolutionError("HTTPSConnection(host='data.alpaca.markets', port=443): Failed to resolve 'data.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `22:02:03` **WARNING** — Aggiornamento cache pre-pipeline fallito: HTTPSConnectionPool(host='data.alpaca.markets', port=443): Max retries exceeded with url: /v2/stocks/bars?start=2026-06-18T19%3A00%3A00%2B00%3A00&timeframe=1Hour&feed=iex&symbols=SPY&limit=10000 (Caused by NameResolutionError("HTTPSConnection(host='data.alpaca.markets', port=443): Failed to resolve 'data.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — uso dati correnti.
- `22:02:23` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/positions (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `22:02:44` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/positions (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `22:03:06` **WARNING** — Sync posizioni Alpaca fallito: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/positions (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — uso stato locale.
- `22:03:26` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/account (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `22:03:47` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/account (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `22:04:09` **ERROR** — Impossibile leggere account Alpaca: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/account (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — skip esecuzione.
- `22:04:09` **CRITICAL** — Connessione API persa: HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/account (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)"))
- `22:04:19` **WARNING** — Invio Telegram fallito: <urlopen error [Errno 101] Network is unreachable>
- `22:04:39` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 1.0s
- `22:05:00` **WARNING** — Errore di rete (tentativo 2/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Max retries exceeded with url: /v2/clock (Caused by NameResolutionError("HTTPSConnection(host='paper-api.alpaca.markets', port=443): Failed to resolve 'paper-api.alpaca.markets' ([Errno -3] Temporary failure in name resolution)")) — retry in 2.0s
- `10:03:48` **WARNING** — Errore di rete (tentativo 1/3): HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): Read timed out. (read timeout=None) — retry in 1.0s
- `10:03:52` **WARNING** — Regime change confermato: STRONG_BEAR → EUPHORIA (3 barre)

---
*Generato automaticamente da regime-trader il 2026-06-19 17:12:31.*
