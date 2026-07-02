"""
Position Tracker — vista in-memory delle posizioni aperte, sincronizzata con Alpaca.

Tiene traccia per ogni posizione di:
  - Prezzo e tempo di ingresso
  - Prezzo corrente e P&L non realizzato
  - Stop level attivo
  - Regime HMM al momento dell'ingresso vs regime corrente
  - Numero di barre di holding

Il WebSocket TradingStream (opzionale) garantisce notifiche di fill in tempo reale.
Senza WebSocket, sync() è il meccanismo di riconciliazione.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from broker.alpaca_client import AlpacaClient

logger = logging.getLogger("regime-trader.broker")


# ──────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    """Posizione aperta con metadati completi."""
    symbol:            str
    qty:               float
    avg_entry_price:   float
    current_price:     float
    market_value:      float
    unrealized_pnl:    float
    unrealized_pnl_pct: float
    side:              str          # "long" | "short"
    opened_at:         Optional[datetime] = None
    stop_level:        float = 0.0
    regime_at_entry:   str = "UNKNOWN"
    current_regime:    str = "UNKNOWN"
    holding_bars:      int = 0

    @property
    def realized_risk(self) -> float:
        """Distanza percentuale dal prezzo di ingresso allo stop."""
        if self.avg_entry_price <= 0:
            return 0.0
        return abs(self.avg_entry_price - self.stop_level) / self.avg_entry_price

    @property
    def is_in_profit(self) -> bool:
        return self.unrealized_pnl > 0


@dataclass
class PortfolioSnapshot:
    """Snapshot del portafoglio in un dato momento."""
    total_value:          float
    cash:                 float
    positions:            dict[str, Position] = field(default_factory=dict)
    total_unrealized_pnl: float = 0.0
    total_realized_pnl:   float = 0.0
    timestamp:            Optional[datetime] = None


# ──────────────────────────────────────────────────────────────────────────────
# TRACKER
# ──────────────────────────────────────────────────────────────────────────────

class PositionTracker:
    """
    Mantiene la vista in-memory delle posizioni e la sincronizza con Alpaca.
    """

    def __init__(self, client: AlpacaClient) -> None:
        self.client          = client
        self._positions:     dict[str, Position] = {}
        self._realized_pnl:  float = 0.0
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_stop:   threading.Event = threading.Event()
        self._fill_callbacks: list[Callable[[str, float, float, str], None]] = []
        self._processed_fill_ids: set[str] = set()

    # ─── Sincronizzazione ─────────────────────────────────────────────────────

    def sync(self) -> None:
        """
        Scarica le posizioni correnti da Alpaca e riconcilia con lo stato locale.
        Conserva i metadati (stop_level, regime_at_entry, ecc.) già noti.
        """
        raw = self.client.get_positions()
        reconciled: dict[str, Position] = {}

        for pos_dict in raw:
            sym = pos_dict["symbol"]
            existing = self._positions.get(sym)
            reconciled[sym] = Position(
                symbol           = sym,
                qty              = pos_dict["qty"],
                avg_entry_price  = pos_dict["avg_entry_price"],
                current_price    = pos_dict["current_price"],
                market_value     = pos_dict["market_value"],
                unrealized_pnl   = pos_dict["unrealized_pl"],
                unrealized_pnl_pct = pos_dict["unrealized_plpc"],
                side             = pos_dict["side"],
                # Preserva metadati locali se già presenti
                opened_at        = existing.opened_at if existing else None,
                stop_level       = existing.stop_level if existing else 0.0,
                regime_at_entry  = existing.regime_at_entry if existing else "UNKNOWN",
                current_regime   = existing.current_regime if existing else "UNKNOWN",
                holding_bars     = existing.holding_bars if existing else 0,
            )

        # Calcola P&L realizzato per le posizioni chiuse durante il sync
        closed = set(self._positions) - set(reconciled)
        for sym in closed:
            closed_pos = self._positions[sym]
            # P&L realizzato approssimativo: unrealized_pnl al momento della chiusura
            self._realized_pnl += closed_pos.unrealized_pnl
            logger.info("Posizione chiusa rilevata: %s (P&L realizzato: %.2f)", sym, closed_pos.unrealized_pnl)

        self._positions = reconciled
        logger.debug("sync() completato: %d posizioni attive", len(self._positions))

    def update_prices(self, latest_prices: dict[str, float]) -> None:
        """Aggiorna i prezzi correnti e ricalcola il P&L non realizzato."""
        for sym, price in latest_prices.items():
            pos = self._positions.get(sym)
            if not pos:
                continue
            pos.current_price    = price
            pos.market_value     = pos.qty * price
            pnl_per_share        = (price - pos.avg_entry_price)
            if pos.side == "short":
                pnl_per_share = -pnl_per_share
            pos.unrealized_pnl   = pnl_per_share * pos.qty
            pos.unrealized_pnl_pct = (
                pnl_per_share / pos.avg_entry_price if pos.avg_entry_price else 0.0
            )

    def increment_holding_bars(self) -> None:
        """Incrementa il contatore di barre per tutte le posizioni aperte."""
        for pos in self._positions.values():
            pos.holding_bars += 1

    def update_current_regime(self, regime_name: str) -> None:
        """Aggiorna il regime corrente per tutte le posizioni aperte."""
        for pos in self._positions.values():
            pos.current_regime = regime_name

    def set_stop_level(self, symbol: str, stop: float) -> None:
        """Aggiorna il livello di stop per una posizione."""
        pos = self._positions.get(symbol)
        if pos:
            pos.stop_level = stop

    # ─── Query ────────────────────────────────────────────────────────────────

    def get_snapshot(self) -> PortfolioSnapshot:
        """Snapshot del portafoglio incluso account Alpaca per cash e total_value."""
        try:
            acc = self.client.get_account()
            total_value = acc["portfolio_value"]
            cash        = acc["cash"]
        except Exception:
            total_value = sum(p.market_value for p in self._positions.values())
            cash        = 0.0

        total_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return PortfolioSnapshot(
            total_value          = total_value,
            cash                 = cash,
            positions            = dict(self._positions),
            total_unrealized_pnl = total_unrealized,
            total_realized_pnl   = self._realized_pnl,
            timestamp            = datetime.now(),
        )

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def get_all_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    def get_weights(self, portfolio_value: float) -> dict[str, float]:
        """Peso corrente di ogni posizione come frazione del NAV."""
        if portfolio_value <= 0:
            return {}
        return {
            sym: pos.market_value / portfolio_value
            for sym, pos in self._positions.items()
        }

    def get_total_exposure(self, portfolio_value: float) -> float:
        """Esposizione totale come frazione del NAV."""
        if portfolio_value <= 0:
            return 0.0
        return sum(p.market_value for p in self._positions.values()) / portfolio_value

    def get_positions_in_regime_drift(self) -> list[Position]:
        """Posizioni dove il regime corrente diverge da quello all'ingresso."""
        return [
            p for p in self._positions.values()
            if p.current_regime != p.regime_at_entry and p.regime_at_entry != "UNKNOWN"
        ]

    # ─── Aggiornamenti locali post-fill ───────────────────────────────────────

    def record_fill(
        self,
        symbol: str,
        qty: float,
        price: float,
        side: str,
        regime_at_entry: str = "UNKNOWN",
        stop_level: float = 0.0,
        fill_id: Optional[str] = None,
    ) -> None:
        """
        Aggiorna la posizione locale dopo un fill confermato.
        Per acquisti: aggiorna la media di ingresso (VWAP).
        Per vendite: riduce qty; se qty=0 rimuove la posizione e realizza il P&L.
        """
        if fill_id:
            if fill_id in self._processed_fill_ids:
                logger.info("Fill duplicato ignorato: %s (%s)", fill_id, symbol)
                return

        existing = self._positions.get(symbol)

        if side == "buy":
            if existing:
                # Media ponderata per il prezzo di ingresso
                total_cost = existing.avg_entry_price * existing.qty + price * qty
                new_qty    = existing.qty + qty
                existing.avg_entry_price = total_cost / new_qty
                existing.qty             = new_qty
                existing.market_value    = new_qty * price
                existing.current_price   = price
            else:
                self._positions[symbol] = Position(
                    symbol           = symbol,
                    qty              = qty,
                    avg_entry_price  = price,
                    current_price    = price,
                    market_value     = qty * price,
                    unrealized_pnl   = 0.0,
                    unrealized_pnl_pct = 0.0,
                    side             = "long",
                    opened_at        = datetime.now(),
                    stop_level       = stop_level,
                    regime_at_entry  = regime_at_entry,
                    current_regime   = regime_at_entry,
                )
            logger.info("Fill BUY: %s +%d @ %.2f — posizione totale: %.0f", symbol, qty, price, self._positions[symbol].qty)

        elif side == "sell" and existing:
            realized = (price - existing.avg_entry_price) * min(qty, existing.qty)
            self._realized_pnl += realized
            existing.qty -= qty

            if existing.qty <= 0:
                del self._positions[symbol]
                logger.info(
                    "Fill SELL: %s posizione chiusa @ %.2f — P&L realizzato: %.2f",
                    symbol, price, realized,
                )
            else:
                existing.current_price = price
                existing.market_value  = existing.qty * price
                logger.info("Fill SELL parziale: %s -%.0f @ %.2f — qty residua: %.0f", symbol, qty, price, existing.qty)

        if fill_id:
            self._processed_fill_ids.add(fill_id)

        for cb in self._fill_callbacks:
            try:
                cb(symbol, qty, price, side)
            except Exception as exc:
                logger.warning("Callback fill errore: %s", exc)

    def on_fill(self, callback: Callable[[str, float, float, str], None]) -> None:
        """Registra una callback chiamata ad ogni fill (symbol, qty, price, side)."""
        self._fill_callbacks.append(callback)

    # ─── WebSocket TradingStream ───────────────────────────────────────────────

    def start_stream(self) -> None:
        """Avvia il WebSocket Alpaca per ricevere fill in tempo reale (thread separato)."""
        if self._stream_thread and self._stream_thread.is_alive():
            logger.warning("Stream già attivo.")
            return

        self._stream_stop.clear()
        self._stream_thread = threading.Thread(
            target=self._run_stream,
            daemon=True,
            name="alpaca-trading-stream",
        )
        self._stream_thread.start()
        logger.info("TradingStream avviato.")

    def stop_stream(self) -> None:
        """Ferma il WebSocket TradingStream."""
        self._stream_stop.set()
        if self._stream_thread:
            self._stream_thread.join(timeout=5)
        logger.info("TradingStream fermato.")

    def _run_stream(self) -> None:
        """Loop asyncio per TradingStream — eseguito nel thread dedicato."""
        from alpaca.trading.stream import TradingStream

        stream = TradingStream(
            api_key    = self.client._api_key,
            secret_key = self.client._secret_key,
            paper      = self.client.paper,
        )

        async def _on_trade_update(data) -> None:
            try:
                event = getattr(data, "event", None)
                if event != "fill":
                    return
                order  = data.order
                symbol = order.symbol
                qty    = float(order.filled_qty or 0)
                price  = float(order.filled_avg_price or 0)
                side   = str(order.side.value).lower() if hasattr(order.side, "value") else str(order.side)
                fill_id = str(
                    getattr(order, "id", None)
                    or getattr(order, "client_order_id", None)
                    or ""
                ) or None
                logger.info("Stream fill: %s %s %.0f @ %.2f", event, symbol, qty, price)
                self.record_fill(symbol=symbol, qty=qty, price=price, side=side, fill_id=fill_id)
            except Exception as exc:
                logger.error("Errore nel handler trade update: %s", exc)

        stream.subscribe_trade_updates(_on_trade_update)

        async def _run():
            task = asyncio.create_task(stream._run_forever())
            while not self._stream_stop.is_set():
                await asyncio.sleep(1)
            task.cancel()

        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.error("TradingStream interrotto: %s", exc)
