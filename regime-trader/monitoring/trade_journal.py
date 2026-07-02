"""
Trade Journal — statistiche aggregate su TUTTE le sessioni del bot.

Legge i JSON che SessionReporter scrive accanto a ogni report markdown
(`reports/sessions/session_*.json`) e produce `reports/journal.md`:
P&L cumulativo, win rate delle sessioni, ordini, stop-out, errori.

SCOPO: passare da "com'è andata oggi?" a "come sta andando IL SISTEMA?".
Le decisioni di strategia (tenere/cambiare stop, ranking, allocazioni)
vanno prese su queste statistiche, non sull'ultima sessione.

Si rigenera automaticamente a ogni fine sessione; a mano:
    venv/bin/python -m monitoring.trade_journal
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("regime-trader.journal")

DEFAULT_SESSIONS_DIR = Path("reports/sessions")
DEFAULT_JOURNAL_PATH = Path("reports/journal.md")


@dataclass
class SessionStats:
    """Statistiche estratte da un singolo JSON di sessione."""
    end: str                  # timestamp di fine (ordinabile)
    mode: str
    duration: str
    start_equity: float
    final_equity: float
    pnl: float
    pnl_pct: float
    orders: int
    closes: int               # posizioni chiuse (stop + square-off)
    stop_outs: int            # chiusure NON da square-off (stop/vendite)
    errors: int               # log ERROR/CRITICAL
    clean: bool


class TradeJournal:
    """Aggrega i JSON di sessione e scrive il journal markdown."""

    def __init__(
        self,
        sessions_dir: Path | None = None,
        journal_path: Path | None = None,
    ) -> None:
        self.sessions_dir = Path(sessions_dir or DEFAULT_SESSIONS_DIR)
        self.journal_path = Path(journal_path or DEFAULT_JOURNAL_PATH)

    # ──────────────────────────────────────────────────────────────────────
    # API principale
    # ──────────────────────────────────────────────────────────────────────

    def update(self) -> Path | None:
        """Rigenera il journal. Ritorna il path, None se nessuna sessione/errore."""
        try:
            sessions = self.load_sessions()
            if not sessions:
                logger.info("Trade journal: nessuna sessione da aggregare.")
                return None
            content = self._render(sessions)
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            self.journal_path.write_text(content, encoding="utf-8")
            logger.info("Trade journal aggiornato: %s (%d sessioni)", self.journal_path, len(sessions))
            return self.journal_path
        except Exception as exc:
            logger.error("Trade journal non aggiornato: %s", exc)
            return None

    def load_sessions(self) -> list[SessionStats]:
        """Carica e ordina cronologicamente tutte le sessioni registrate."""
        stats: list[SessionStats] = []
        for path in sorted(self.sessions_dir.glob("session_*.json")):
            try:
                stats.append(self._parse(json.loads(path.read_text(encoding="utf-8"))))
            except Exception as exc:
                logger.warning("Sessione %s illeggibile: %s", path.name, exc)
        stats.sort(key=lambda s: s.end)
        return stats

    # ──────────────────────────────────────────────────────────────────────
    # Parsing e rendering
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse(d: dict) -> SessionStats:
        start_eq = float(d.get("start_equity") or 0.0)
        final_eq = float(d.get("final_equity") or start_eq)
        pnl = final_eq - start_eq

        events = d.get("events") or []
        all_closes = [e for e in events if e.get("kind") == "CLOSE"]
        # Lo square-off genera DUE eventi per simbolo: il marker ("square-off
        # serale") e il fill con prezzo ("N az @ X"). Le chiusure reali sono i
        # fill; gli stop-out sono i fill che NON appartengono allo square-off.
        closes = [e for e in all_closes if " az @" in (e.get("detail") or "")]
        square_offs = [e for e in all_closes if "square-off" in (e.get("detail") or "")]

        warnings = d.get("warnings") or []
        errors = sum(1 for w in warnings if w.get("level") in ("ERROR", "CRITICAL"))

        start = str(d.get("session_start") or "")
        end = str(d.get("session_end") or "")
        duration = "?"
        if len(start) >= 19 and len(end) >= 19:
            duration = f"{start[11:16]}–{end[11:16]}"

        return SessionStats(
            end=end,
            mode=str(d.get("mode", "?")),
            duration=duration,
            start_equity=start_eq,
            final_equity=final_eq,
            pnl=pnl,
            pnl_pct=(pnl / start_eq * 100) if start_eq else 0.0,
            orders=len(d.get("trades") or []),
            closes=len(closes),
            stop_outs=max(0, len(closes) - len(square_offs)),
            errors=errors,
            clean=bool(d.get("clean_shutdown")),
        )

    def _render(self, sessions: list[SessionStats]) -> str:
        n = len(sessions)
        total_pnl = sum(s.pnl for s in sessions)
        first_eq = sessions[0].start_equity
        total_pct = (total_pnl / first_eq * 100) if first_eq else 0.0
        winners = [s for s in sessions if s.pnl > 0]
        total_orders = sum(s.orders for s in sessions)
        total_stop_outs = sum(s.stop_outs for s in sessions)
        total_errors = sum(s.errors for s in sessions)
        best = max(sessions, key=lambda s: s.pnl)
        worst = min(sessions, key=lambda s: s.pnl)

        lines: list[str] = []
        a = lines.append
        a("# Trade Journal — statistiche aggregate")
        a("")
        a("> Rigenerato automaticamente a ogni fine sessione.")
        a("> Le decisioni di strategia vanno prese su QUESTI numeri, non sull'ultima sessione.")
        a("")
        a("## Quadro cumulativo")
        a("")
        a("| | |")
        a("|---|---|")
        a(f"| Sessioni registrate | {n} |")
        a(f"| P&L cumulativo | ${total_pnl:+,.2f} ({total_pct:+.2f}%) |")
        a(f"| Sessioni in utile | {len(winners)}/{n} ({len(winners)/n:.0%}) |")
        a(f"| Migliore | {best.end[:16]} ({best.pnl:+,.2f}$) |")
        a(f"| Peggiore | {worst.end[:16]} ({worst.pnl:+,.2f}$) |")
        a(f"| Ordini totali | {total_orders} (media {total_orders/n:.1f}/sessione) |")
        a(f"| Stop-out / vendite | {total_stop_outs} |")
        a(f"| Errori ERROR/CRITICAL | {total_errors} |")
        a("")
        a("## Sessioni (cronologico)")
        a("")
        a("| Fine | Orario | Modo | P&L | P&L % | Ordini | Stop-out | Errori | Shutdown |")
        a("|---|---|---|---|---|---|---|---|---|")
        for s in sessions:
            a(
                f"| {s.end[:10]} | {s.duration} | {s.mode} "
                f"| ${s.pnl:+,.2f} | {s.pnl_pct:+.2f}% "
                f"| {s.orders} | {s.stop_outs} | {s.errors} "
                f"| {'pulito' if s.clean else '⚠️ crash'} |"
            )
        a("")
        a("## Equity")
        a("")
        a("| Fine sessione | Equity finale |")
        a("|---|---|")
        for s in sessions:
            a(f"| {s.end[:16]} | ${s.final_equity:,.2f} |")
        a("")
        return "\n".join(lines)


if __name__ == "__main__":
    path = TradeJournal().update()
    if path:
        print(path.read_text(encoding="utf-8"))
    else:
        print("Nessuna sessione registrata (i JSON compaiono da questa versione in poi).")
