"""
Live Dashboard — interfaccia terminale in tempo reale basata su Rich.

Layout a pannelli:
  ┌─ REGIME ──────────┐  ┌─ PORTFOLIO ──────┐
  ┌─ POSITIONS ───────────────────────────────┐
  ┌─ RECENT SIGNALS ──────────────────────────┐
  ┌─ RISK STATUS ─────────────────────────────┐
  ┌─ SYSTEM ──────────────────────────────────┐

Aggiornamento ogni `refresh_seconds`. Barre di rischio color-coded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from broker.position_tracker import PortfolioSnapshot, Position
from core.hmm_engine import RegimeState


@dataclass
class DashboardState:
    """Tutti i dati necessari a renderizzare un frame della dashboard."""
    snapshot:       PortfolioSnapshot
    regime_state:   RegimeState
    # Portfolio / strategia
    allocation_pct: float = 0.0
    leverage:       float = 1.0
    flicker_rate:   float = 0.0
    flicker_window: int = 20
    daily_pnl:      float = 0.0
    daily_pnl_pct:  float = 0.0
    # Rischio
    daily_dd:       float = 0.0     # drawdown giornaliero corrente [0,1]
    daily_dd_limit: float = 0.03
    peak_dd:        float = 0.0     # drawdown dal picco [0,1]
    peak_dd_limit:  float = 0.10
    circuit_breaker: str = "NORMAL"
    # Sistema
    data_feed_ok:   bool = True
    api_ok:         bool = True
    api_latency_ms: Optional[float] = None
    hmm_age_str:    str = "?"
    trading_mode:   str = "PAPER"
    # Segnali recenti: lista di dict {time, symbol, action, regime}
    recent_signals: list = field(default_factory=list)
    considered_signals: list = field(default_factory=list)
    events: list = field(default_factory=list)
    symbols: list = field(default_factory=list)


def dashboard_state_from_dict(data: dict) -> DashboardState:
    """
    Ricostruisce un DashboardState dal dict di state_snapshot.json.

    Usa la sezione "dashboard" (dati live scritti ad ogni ciclo) e "positions".
    Tollerante verso snapshot vecchi privi della sezione "dashboard".
    """
    from datetime import datetime as _dt

    dash = data.get("dashboard", {}) or {}

    # ── Posizioni ──
    positions: dict[str, Position] = {}
    for sym, p in data.get("positions", {}).items():
        opened = None
        if p.get("opened_at"):
            try:
                opened = _dt.fromisoformat(p["opened_at"])
            except ValueError:
                opened = None
        positions[sym] = Position(
            symbol             = sym,
            qty                = p.get("qty", 0.0),
            avg_entry_price    = p.get("avg_entry_price", 0.0),
            current_price      = p.get("current_price", p.get("avg_entry_price", 0.0)),
            market_value       = p.get("qty", 0.0) * p.get("current_price", 0.0),
            unrealized_pnl     = p.get("unrealized_pnl", 0.0),
            unrealized_pnl_pct = p.get("unrealized_pnl_pct", 0.0),
            side               = p.get("side", "long"),
            opened_at          = opened,
            stop_level         = p.get("stop_level", 0.0),
            regime_at_entry    = p.get("regime_at_entry", "UNKNOWN"),
            current_regime     = p.get("current_regime", "UNKNOWN"),
            holding_bars       = p.get("holding_bars", 0),
        )

    equity = dash.get("equity", data.get("peak_equity", 0.0))
    snapshot = PortfolioSnapshot(
        total_value = equity,
        cash        = 0.0,
        positions   = positions,
    )

    state_probs = dash.get("state_probabilities") or [dash.get("probability", 1.0)]
    regime_state = RegimeState(
        label               = dash.get("regime", "UNKNOWN"),
        state_id            = 0,
        probability         = dash.get("probability", 0.0),
        state_probabilities = state_probs,
        timestamp           = None,
        is_confirmed        = dash.get("is_confirmed", True),
        consecutive_bars    = dash.get("consecutive_bars", 0),
    )

    return DashboardState(
        snapshot       = snapshot,
        regime_state   = regime_state,
        allocation_pct = dash.get("allocation_pct", 0.0),
        leverage       = dash.get("leverage", 1.0),
        flicker_rate   = dash.get("flicker_rate", 0.0),
        flicker_window = dash.get("flicker_window", 20),
        daily_pnl      = dash.get("daily_pnl", 0.0),
        daily_pnl_pct  = dash.get("daily_pnl_pct", 0.0),
        daily_dd       = dash.get("daily_dd", 0.0),
        daily_dd_limit = dash.get("daily_dd_limit", 0.03),
        peak_dd        = dash.get("peak_dd", 0.0),
        peak_dd_limit  = dash.get("peak_dd_limit", 0.10),
        circuit_breaker = dash.get("circuit_breaker", data.get("circuit_breaker", "NORMAL")),
        data_feed_ok   = dash.get("data_feed_ok", True),
        api_ok         = dash.get("api_ok", True),
        api_latency_ms = dash.get("api_latency_ms"),
        hmm_age_str    = dash.get("hmm_age_str", "?"),
        trading_mode   = dash.get("trading_mode", "PAPER"),
        recent_signals = dash.get("recent_signals", []),
        considered_signals = dash.get("considered_signals", []),
        events         = dash.get("events", []),
        symbols        = dash.get("symbols", []),
    )


def _risk_bar(value: float, limit: float, width: int = 12) -> Text:
    """
    Barra di rischio color-coded: verde < 50% del limite, giallo < 80%, rosso oltre.
    `value` e `limit` sono frazioni (es. 0.003 e 0.03).
    """
    ratio = (value / limit) if limit > 0 else 0.0
    ratio = max(0.0, min(ratio, 1.0))
    filled = int(round(ratio * width))

    if ratio < 0.5:
        color, icon = "green", "✅"
    elif ratio < 0.8:
        color, icon = "yellow", "⚠️"
    else:
        color, icon = "red", "🚨"

    bar = "█" * filled + "░" * (width - filled)
    text = Text()
    text.append(bar, style=color)
    text.append(f" {value:.1%}/{limit:.0%} {icon}")
    return text


class LiveDashboard:
    """
    Dashboard terminale che si aggiorna in background mentre il bot è in esecuzione.
    """

    def __init__(self, refresh_seconds: int = 5) -> None:
        self.refresh_seconds = refresh_seconds
        self._console = Console()
        self._live: Optional[Live] = None
        self._state: Optional[DashboardState] = None

    # ─── Ciclo di vita ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Avvia il rendering live (non bloccante)."""
        if self._live is not None:
            return
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=max(1, 1 / self.refresh_seconds) if self.refresh_seconds < 1 else 1,
            screen=False,
        )
        self._live.start()

    def stop(self) -> None:
        """Ferma la dashboard e ripristina il terminale."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    # ─── Aggiornamento ─────────────────────────────────────────────────────

    def update(self, state: DashboardState) -> None:
        """Aggiorna lo stato e ridisegna il frame."""
        self._state = state
        if self._live is not None:
            self._live.update(self._render())

    def render_once(self, state: DashboardState) -> None:
        """Disegna un singolo frame statico (utile per snapshot o test)."""
        self._state = state
        self._console.print(self._render())

    # ─── Rendering ─────────────────────────────────────────────────────────

    def _render(self) -> Group:
        if self._state is None:
            return Group(Panel(Text("In attesa di dati…"), title="REGIME TRADER"))

        s = self._state
        return Group(
            self._regime_portfolio_row(s),
            self._positions_panel(s),
            self._signals_panel(s),
            self._risk_panel(s),
            self._system_panel(s),
        )

    def _regime_portfolio_row(self, s: DashboardState) -> Group:
        return Group(self._regime_panel(s), self._portfolio_panel(s))

    def _regime_panel(self, s: DashboardState) -> Panel:
        rs = s.regime_state
        conf_color = "green" if rs.probability >= 0.70 else "yellow" if rs.probability >= 0.55 else "red"
        text = Text()
        text.append(f"{rs.label} ", style=f"bold {conf_color}")
        text.append(f"({rs.probability:.0%})", style=conf_color)
        text.append(f"  |  Stability: {rs.consecutive_bars} bars")
        text.append(f"  |  Flicker: {s.flicker_rate:.0f}/{s.flicker_window}")
        if not rs.is_confirmed:
            text.append("  [TRANSIZIONE]", style="yellow")
        return Panel(text, title="REGIME", title_align="left", border_style="cyan")

    def _portfolio_panel(self, s: DashboardState) -> Panel:
        snap = s.snapshot
        pnl_color = "green" if s.daily_pnl >= 0 else "red"
        text = Text()
        text.append(f"Equity: ${snap.total_value:,.2f}")
        text.append("  |  Daily: ")
        text.append(f"{'+' if s.daily_pnl >= 0 else ''}${s.daily_pnl:,.2f} "
                    f"({s.daily_pnl_pct:+.2%})", style=pnl_color)
        text.append(f"\nAllocation: {s.allocation_pct:.0%}  |  Leverage: {s.leverage:.2f}x")
        return Panel(text, title="PORTFOLIO", title_align="left", border_style="blue")

    def _positions_panel(self, s: DashboardState) -> Panel:
        positions = s.snapshot.positions
        if not positions:
            return Panel(Text("Nessuna posizione aperta.", style="dim"),
                         title="POSITIONS", title_align="left", border_style="magenta")

        table = Table(show_header=True, header_style="bold", expand=True, box=None)
        table.add_column("Symbol")
        table.add_column("Side")
        table.add_column("Qty", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Investito", justify="right")
        table.add_column("P&L $", justify="right")
        table.add_column("P&L %", justify="right")
        table.add_column("Stop", justify="right")
        table.add_column("Held", justify="right")

        total_invested = 0.0
        for sym, pos in positions.items():
            pnl_color = "green" if pos.unrealized_pnl >= 0 else "red"
            held = self._format_holding(pos)
            invested = pos.qty * pos.avg_entry_price   # capitale impegnato a prezzo di carico
            total_invested += invested
            table.add_row(
                sym,
                pos.side.upper(),
                f"{pos.qty:,.0f}",
                f"${pos.current_price:,.2f}",
                f"${invested:,.0f}",
                Text(f"{'+' if pos.unrealized_pnl >= 0 else ''}${pos.unrealized_pnl:,.0f}", style=pnl_color),
                Text(f"{pos.unrealized_pnl_pct:+.1%}", style=pnl_color),
                f"${pos.stop_level:,.2f}" if pos.stop_level else "[red]—[/red]",
                held,
            )
        # Riga totale
        table.add_row(
            Text("TOTALE", style="bold"), "", "", "",
            Text(f"${total_invested:,.0f}", style="bold"),
            "", "", "", "",
        )
        return Panel(table, title="POSITIONS", title_align="left", border_style="magenta")

    def _signals_panel(self, s: DashboardState) -> Panel:
        if not s.considered_signals:
            return Panel(Text("Nessun titolo valutato nell'ultimo ciclo.", style="dim"),
                         title="TITOLI VALUTATI", title_align="left", border_style="green")

        table = Table(show_header=True, header_style="bold", expand=True, box=None)
        table.add_column("Symbol")
        table.add_column("Stato")
        table.add_column("Prezzo", justify="right")
        table.add_column("Δ prezzo", justify="right")
        table.add_column("Peso", justify="right")
        table.add_column("Target", justify="right")
        table.add_column("Stop", justify="right")

        for sig in s.considered_signals[:10]:
            change = float(sig.get("price_change_pct") or 0.0)
            change_style = "green" if change >= 0 else "red"
            status = sig.get("status", "")
            status_style = "green" if status in ("APERTO", "IN TARGET", "AUMENTA") else "yellow"
            table.add_row(
                sig.get("symbol", ""),
                Text(status, style=status_style),
                f"${float(sig.get('price') or 0.0):,.2f}",
                Text(f"{change:+.2%}", style=change_style),
                f"{float(sig.get('current_weight') or 0.0):.1%}",
                f"{float(sig.get('target_weight') or 0.0):.1%}",
                f"${float(sig.get('active_stop') or sig.get('signal_stop') or 0.0):,.2f}",
            )
        return Panel(table, title="TITOLI VALUTATI", title_align="left", border_style="green")

    def _risk_panel(self, s: DashboardState) -> Panel:
        cb_color = "green" if s.circuit_breaker == "NORMAL" else "red"
        body = Text()
        body.append("Daily DD:    "); body.append_text(_risk_bar(s.daily_dd, s.daily_dd_limit)); body.append("\n")
        body.append("From Peak:   "); body.append_text(_risk_bar(s.peak_dd, s.peak_dd_limit)); body.append("\n")
        body.append("Circuit Breaker: ")
        body.append(s.circuit_breaker, style=f"bold {cb_color}")
        return Panel(body, title="RISK STATUS", title_align="left", border_style="red")

    def _system_panel(self, s: DashboardState) -> Panel:
        text = Text()
        text.append("Data: ")
        text.append("✅" if s.data_feed_ok else "🚨", style="green" if s.data_feed_ok else "red")
        text.append("  |  API: ")
        api_str = "✅" if s.api_ok else "🚨"
        if s.api_ok and s.api_latency_ms is not None:
            api_str += f" {s.api_latency_ms:.0f}ms"
        text.append(api_str, style="green" if s.api_ok else "red")
        text.append(f"  |  HMM: {s.hmm_age_str}")
        mode_color = "yellow" if s.trading_mode == "PAPER" else ("red" if s.trading_mode == "LIVE" else "cyan")
        text.append("  |  ")
        text.append(s.trading_mode, style=f"bold {mode_color}")
        return Panel(text, title="SYSTEM", title_align="left", border_style="white")

    @staticmethod
    def _format_holding(pos) -> str:
        """Formatta il periodo di holding in modo leggibile."""
        if pos.opened_at:
            delta = datetime.now() - pos.opened_at
            hours = delta.total_seconds() / 3600
            if hours < 24:
                return f"{hours:.0f}h"
            return f"{hours / 24:.0f}d"
        if pos.holding_bars:
            return f"{pos.holding_bars}b"
        return "—"
