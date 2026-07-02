# Report sessione — 2026-06-16 (RICOSTRUITO DAI LOG)

> ⚠️ **Report NON auto-generato.** Il 16/06 il bot principale **non si è spento**:
> ha fatto lo square-off alle 21:45 e ha continuato a girare tutta la notte, quindi
> il report automatico (che nasce solo allo spegnimento) non è mai stato scritto.
> Ricostruito a posteriori dai log (`alerts.log`, `main.log`) il 2026-06-17.

## Quadro generale

| | |
|---|---|
| Inizio sessione | 2026-06-16 16:04 (CEST) |
| Square-off serale | 2026-06-16 21:45 (CEST) |
| Modalità | PAPER |
| Timeframe | 1Hour |
| Regime dominante | STRONG_BULL (conf 98–100%) |
| Simboli operativi | SPY, SH, RWM, DOG, NVDA, MU, QQQ, AAPL, AMZN, INTC, IWM, MSFT |

## Risultati

| | |
|---|---|
| Equity iniziale | $97,459.51 |
| P&L round-trip del giorno | **−$1,164.71 (−1,20%)** |
| Posizioni aperte | 5 (SPY, MU, QQQ, AMZN, INTC) — tutte chiuse in perdita |
| Progressione equity | 16:10 $97.460 → 17:01 $96.824 → 18:01 $96.689 → square-off ~$96.295 |

## Trade del giorno (entry @16:10 → square-off @21:45)

| Simbolo | Azioni | Entry | Exit | P&L |
|---|---|---|---|---|
| SPY | 19 | 755.11 | 751.02 | −$77.71 |
| MU | 6 | 1087.86 | 1035.19 | −$316.02 |
| QQQ | 19 | 743.27 | 732.35 | −$207.48 |
| AMZN | 59 | 247.07 | 246.09 | −$57.82 |
| INTC | 84 | 124.76 | 118.74 | −$505.68 |
| | | | **TOTALE** | **−$1,164.71** |

## Anomalie / warning

- 3 warning di rete (Connection aborted / RemoteDisconnected / "Prezzi live non disponibili")
  tra le 17:30 e le 19:32: blip di rete. Il principale ha continuato a girare (il fork PM
  invece si bloccò ~1h44m per lo stesso problema — vedi cronaca del 16/06).
- ⚠️ Il bug `client_order_id` e l'HANG da timeout di rete erano **ancora presenti**
  (i fix sono stati applicati e deployati solo il 17/06).

## Lettura

Giornata **negativa** ma **coerente con la strategia**: regime STRONG_BULL → allocazione
alta, ma il mercato è sceso nel pomeriggio e — essendo il bot **sempre long** — ha chiuso
tutte le 5 posizioni in perdita allo square-off. Nessuno stop colpito (le perdite sono da
square-off, non da stop-out). MU e INTC i peggiori. Non è un malfunzionamento: è il costo
di una giornata di mercato in calo per un sistema long-only.

---
*Ricostruito dai log il 2026-06-17. Il report automatico non esiste perché il bot non fu spento il 16.*
