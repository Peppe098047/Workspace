"""
Session Report — report markdown di fine sessione.

A ogni arresto del bot (volontario o per errore) viene scritto un report in
`reports/sessions/` con tutto quello che è successo: risultati, regimi,
trade, eventi, warning/errori e posizioni residue.

SCOPO: il report è pensato per essere letto da un agente AI (o da un umano)
all'inizio della sessione di lavoro successiva, per individuare errori o
strategie mal funzionanti senza dover ricostruire i log JSON grezzi.
Il file più recente è sempre disponibile anche come `reports/sessions/LATEST.md`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("regime-trader.report")

DEFAULT_REPORTS_DIR = Path("reports/sessions")


class SessionLogCapture(logging.Handler):
    """
    Handler che colleziona i log WARNING+ della sessione per il report finale.
    Tiene al massimo `max_records` voci (le più recenti).
    """

    def __init__(self, max_records: int = 200) -> None:
        super().__init__(level=logging.WARNING)
        self.max_records = max_records
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append({
                "time":    datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level":   record.levelname,
                "message": record.getMessage(),
            })
            if len(self.records) > self.max_records:
                self.records = self.records[-self.max_records:]
        except Exception:
            pass   # il logging non deve mai rompere il bot

    def install(self, logger_name: str = "regime-trader") -> None:
        logging.getLogger(logger_name).addHandler(self)


class SessionReporter:
    """Scrive il report markdown di fine sessione."""

    def __init__(self, reports_dir: Path | None = None) -> None:
        # Il default è risolto a runtime così i test possono ridirigere la cartella
        self.reports_dir = Path(reports_dir or DEFAULT_REPORTS_DIR)

    def write(self, data: dict) -> Path | None:
        """
        Scrive il report e aggiorna LATEST.md. Ritorna il path del file,
        None se la scrittura fallisce (non deve mai bloccare lo shutdown).

        Chiavi attese in `data` (tutte opzionali, il report degrada con grazia):
          session_start (datetime), session_end (datetime), mode (str),
          timeframe (str), symbols (list[str]), clean_shutdown (bool),
          start_equity, final_equity, peak_equity (float),
          trades (list[dict]),        # voci di _trade_log
          events (list[dict]),        # eventi dashboard BUY/SELL/CLOSE/STOP/SKIP/REJECT
          regime_changes (list[dict]),# {time, old, new, probability}
          warnings (list[dict]),      # da SessionLogCapture
          open_positions (dict),      # posizioni residue {sym: {...}}
        """
        try:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            end = data.get("session_end") or datetime.now()
            stem = f"session_{end.strftime('%Y-%m-%d_%H-%M')}"
            path = self.reports_dir / f"{stem}.md"
            content = self._render(data)
            path.write_text(content, encoding="utf-8")
            (self.reports_dir / "LATEST.md").write_text(content, encoding="utf-8")

            # Copia machine-readable per il trade journal aggregato
            try:
                json_path = self.reports_dir / f"{stem}.json"
                json_path.write_text(
                    json.dumps(data, indent=2, default=str, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("JSON di sessione non scritto: %s", exc)

            logger.info("Report di sessione salvato in %s", path)
            return path
        except Exception as exc:
            logger.error("Impossibile scrivere il report di sessione: %s", exc)
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Rendering
    # ──────────────────────────────────────────────────────────────────────

    def _render(self, d: dict) -> str:
        start: datetime | None = d.get("session_start")
        end:   datetime = d.get("session_end") or datetime.now()
        duration = str(end - start).split(".")[0] if start else "?"

        start_eq = float(d.get("start_equity") or 0.0)
        final_eq = float(d.get("final_equity") or start_eq)
        peak_eq  = float(d.get("peak_equity") or max(start_eq, final_eq))
        pnl      = final_eq - start_eq
        pnl_pct  = (pnl / start_eq * 100) if start_eq else 0.0
        max_dd   = ((peak_eq - final_eq) / peak_eq * 100) if peak_eq else 0.0

        trades   = d.get("trades") or []
        events   = d.get("events") or []
        regimes  = d.get("regime_changes") or []
        warns    = d.get("warnings") or []
        open_pos = d.get("open_positions") or {}
        shutdown = "volontario (pulito)" if d.get("clean_shutdown") else "⚠️ da ERRORE/crash"

        lines: list[str] = []
        a = lines.append

        a(f"# Report sessione — {end.strftime('%Y-%m-%d %H:%M')}")
        a("")
        a("> Report generato automaticamente alla chiusura del bot.")
        a("> Per agenti AI: leggere PRIMA la sezione *Anomalie* e i warning/errori.")
        a("")
        a("## Quadro generale")
        a("")
        a(f"| | |")
        a(f"|---|---|")
        a(f"| Inizio sessione | {start.strftime('%Y-%m-%d %H:%M:%S') if start else '?'} |")
        a(f"| Fine sessione | {end.strftime('%Y-%m-%d %H:%M:%S')} |")
        a(f"| Durata | {duration} |")
        a(f"| Modalità | {d.get('mode', '?')} |")
        a(f"| Timeframe | {d.get('timeframe', '?')} |")
        a(f"| Shutdown | {shutdown} |")
        a(f"| Simboli operativi | {', '.join(d.get('symbols') or []) or '?'} |")
        a("")
        a("## Risultati")
        a("")
        a(f"| | |")
        a(f"|---|---|")
        a(f"| Equity iniziale | ${start_eq:,.2f} |")
        a(f"| Equity finale | ${final_eq:,.2f} |")
        a(f"| P&L sessione | ${pnl:+,.2f} ({pnl_pct:+.2f}%) |")
        a(f"| Picco equity | ${peak_eq:,.2f} |")
        a(f"| Drawdown dal picco a fine sessione | {max_dd:.2f}% |")
        a(f"| Ordini inviati | {len(trades)} |")
        a("")

        # ── Anomalie: la sezione che un agente deve leggere per prima ──────
        a("## Anomalie")
        a("")
        anomalies: list[str] = []
        if not d.get("clean_shutdown"):
            anomalies.append("La sessione è terminata per **errore/crash**, non per spegnimento volontario.")
        if open_pos:
            anomalies.append(
                f"Il conto NON è flat: {len(open_pos)} posizioni ancora aperte "
                f"({', '.join(sorted(open_pos))}) — verificare gli stop sul broker."
            )
        errors = [w for w in warns if w.get("level") in ("ERROR", "CRITICAL")]
        if errors:
            anomalies.append(f"{len(errors)} log di livello ERROR/CRITICAL durante la sessione (vedi sotto).")
        if not anomalies:
            a("Nessuna anomalia rilevata: shutdown pulito, conto flat.")
        else:
            for item in anomalies:
                a(f"- {item}")
        a("")

        if regimes:
            a("## Cambi di regime")
            a("")
            a("| Ora | Da | A | Confidenza |")
            a("|---|---|---|---|")
            for r in regimes:
                prob = r.get("probability")
                prob_str = f"{prob:.0%}" if isinstance(prob, (int, float)) else "?"
                a(f"| {r.get('time', '?')} | {r.get('old', '?')} | {r.get('new', '?')} | {prob_str} |")
            a("")

        if trades:
            a("## Ordini inviati")
            a("")
            a("| Ora | Simbolo | Direzione | Azioni | Entry | Stop | Regime |")
            a("|---|---|---|---|---|---|---|")
            for t in trades:
                ts = str(t.get("timestamp", "?"))
                time_part = ts[11:19] if len(ts) >= 19 else ts
                stop = t.get("stop")
                stop_str = f"{stop:.2f}" if isinstance(stop, (int, float)) else "—"
                a(
                    f"| {time_part} | {t.get('symbol', '?')} | {t.get('direction', '?')} "
                    f"| {t.get('shares', '?')} | {t.get('entry', 0):.2f} "
                    f"| {stop_str} | {t.get('regime', '?')} |"
                )
            a("")

        if events:
            a("## Eventi (BUY / SELL / CLOSE / STOP / SKIP / REJECT)")
            a("")
            a("| Ora | Tipo | Simbolo | Dettaglio |")
            a("|---|---|---|---|")
            for e in events:
                a(
                    f"| {e.get('time', '?')} | {e.get('kind', '?')} "
                    f"| {e.get('symbol', '?')} | {e.get('detail', '')} |"
                )
            a("")

        if open_pos:
            a("## Posizioni residue (conto NON flat)")
            a("")
            a("| Simbolo | Qty | Entry | Stop attivo |")
            a("|---|---|---|---|")
            for sym, p in sorted(open_pos.items()):
                a(
                    f"| {sym} | {p.get('qty', '?')} | {p.get('avg_entry_price', 0):.2f} "
                    f"| {p.get('stop_level', 0):.2f} |"
                )
            a("")

        if warns:
            a("## Warning ed errori della sessione")
            a("")
            for w in warns:
                a(f"- `{w.get('time', '?')}` **{w.get('level', '?')}** — {w.get('message', '')}")
            a("")

        a("---")
        a(f"*Generato automaticamente da regime-trader il {end.strftime('%Y-%m-%d %H:%M:%S')}.*")
        a("")
        return "\n".join(lines)
