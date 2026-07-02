"""
Order Executor — ciclo di vita completo di un ordine.

Flusso standard per submit_order():
  1. LIMIT a ±0.1% dal prezzo corrente (buy: +0.1%, sell: -0.1%)
  2. Polling ogni 2s per 30s
  3. Se ancora aperto dopo 30s: cancella → retry a mercato (se retry_market=True)

Ogni ordine riceve un trade_id univoco che collega:
  signal → risk_decision → order_id Alpaca → fill
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

from broker.alpaca_client import AlpacaClient
from broker.api_errors import is_non_retryable_api_error
from core.signal_generator import SignalType, TradingSignal

logger = logging.getLogger("regime-trader.broker")

_LIMIT_SLIP  = 0.001   # 0.1% di slippage per ordini LIMIT
_FILL_POLL_S = 2.0     # secondi tra un controllo e l'altro
_FILL_TIMEOUT = 30.0   # secondi prima di cancellare e ritentare a mercato


class OrderStatus(str, Enum):
    PENDING          = "pending"
    FILLED           = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED        = "cancelled"
    REJECTED         = "rejected"
    EXPIRED          = "expired"


_ALPACA_TO_STATUS: dict[str, OrderStatus] = {
    "new":              OrderStatus.PENDING,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled":           OrderStatus.FILLED,
    "done_for_day":     OrderStatus.EXPIRED,
    "canceled":         OrderStatus.CANCELLED,
    "expired":          OrderStatus.EXPIRED,
    "replaced":         OrderStatus.CANCELLED,
    "pending_cancel":   OrderStatus.PENDING,
    "pending_replace":  OrderStatus.PENDING,
    "accepted":         OrderStatus.PENDING,
    "held":             OrderStatus.PENDING,
    "accepted_for_bidding": OrderStatus.PENDING,
    "stopped":          OrderStatus.PENDING,
    "rejected":         OrderStatus.REJECTED,
    "suspended":        OrderStatus.REJECTED,
    "calculated":       OrderStatus.PENDING,
}


@dataclass
class ExecutionResult:
    """Risultato dell'esecuzione di un ordine."""
    trade_id:       str
    order_id:       str
    symbol:         str
    status:         OrderStatus
    filled_qty:     float
    avg_fill_price: float
    side:           str   # "buy" | "sell"
    retried_market: bool = False
    error:          Optional[str] = None
    metadata:       dict = field(default_factory=dict)


class OrderExecutor:
    """
    Esegue segnali di trading come ordini Alpaca e traccia il loro stato.
    """

    def __init__(
        self,
        client: AlpacaClient,
        on_fill: Optional[Callable[[ExecutionResult], None]] = None,
    ) -> None:
        self.client   = client
        self.on_fill  = on_fill
        self._pending: dict[str, ExecutionResult] = {}  # trade_id → result

    # ─── API pubblica ─────────────────────────────────────────────────────────

    def submit_order(
        self,
        symbol: str,
        shares: int,
        side: OrderSide,
        entry_price: float,
        trade_id: Optional[str] = None,
        *,
        retry_market: bool = True,
    ) -> ExecutionResult:
        """
        Invia un LIMIT order a ±0.1%; se non eseguito entro 30s,
        cancella e (opzionalmente) ritenta a mercato.
        """
        if shares <= 0:
            raise ValueError(f"shares deve essere > 0, ricevuto: {shares}")

        trade_id = trade_id or self._new_trade_id(symbol)
        limit_price = self._limit_price(entry_price, side)
        side_str = "buy" if side == OrderSide.BUY else "sell"

        logger.info(
            "[%s] LIMIT %s %d %s @ %.2f (trade_id=%s)",
            trade_id, side_str.upper(), shares, symbol, limit_price, trade_id,
        )

        order = self._with_retry(
            lambda: self.client.place_limit_order(
                symbol=symbol,
                qty=shares,
                limit_price=limit_price,
                side=side,
                client_order_id=trade_id,
            )
        )
        result = self._make_result(trade_id, order, side_str)
        self._pending[trade_id] = result

        # Attendi il fill entro il timeout
        result = self._wait_for_fill(trade_id, result)

        # Se non riempito: cancella e ritenta a mercato
        if result.status == OrderStatus.PENDING and retry_market:
            result = self._cancel_and_retry_market(trade_id, result, symbol, shares, side, side_str)

        self._pending.pop(trade_id, None)
        if result.status == OrderStatus.FILLED and self.on_fill:
            self.on_fill(result)

        return result

    def submit_bracket_order(
        self,
        symbol: str,
        shares: int,
        side: OrderSide,
        entry_price: float,
        stop_price: float,
        take_profit_price: Optional[float] = None,
        trade_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Invia un ordine bracket (entry + stop + take_profit) via OCO Alpaca."""
        if shares <= 0:
            raise ValueError(f"shares deve essere > 0, ricevuto: {shares}")

        # Per long: stop deve essere sotto l'entry
        if side == OrderSide.BUY and stop_price >= entry_price:
            raise ValueError("stop_price deve essere < entry_price per ordini LONG")

        trade_id = trade_id or self._new_trade_id(symbol)
        limit_price = self._limit_price(entry_price, side)
        side_str = "buy" if side == OrderSide.BUY else "sell"

        logger.info(
            "[%s] BRACKET %s %d %s entry=%.2f stop=%.2f tp=%s",
            trade_id, side_str.upper(), shares, symbol,
            limit_price, stop_price,
            f"{take_profit_price:.2f}" if take_profit_price else "—",
        )

        order = self._with_retry(
            lambda: self.client.place_bracket_order(
                symbol=symbol,
                qty=shares,
                side=side,
                limit_price=limit_price,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
                client_order_id=trade_id,
            )
        )
        result = self._make_result(trade_id, order, side_str)
        if result.status == OrderStatus.FILLED and self.on_fill:
            self.on_fill(result)
        return result

    def modify_stop(self, symbol: str, new_stop: float, side: str = "long") -> bool:
        """
        Stringe (mai allarga) lo stop di una posizione aperta.
        Cerca tra gli ordini aperti lo stop leg per il simbolo.
        Restituisce True se lo stop è stato modificato.
        """
        open_orders = self._with_retry(self.client.get_open_orders)
        stop_orders = [
            o for o in open_orders
            if o["symbol"] == symbol and o["type"] in ("stop", "stop_limit")
        ]
        if not stop_orders:
            logger.warning("Nessuno stop order trovato per %s", symbol)
            return False

        for stop_order in stop_orders:
            current_stop = stop_order.get("stop_price") or stop_order.get("limit_price") or 0.0
            is_tighter = (
                (side == "long"  and new_stop > current_stop) or
                (side == "short" and new_stop < current_stop)
            )
            if not is_tighter:
                logger.info(
                    "modify_stop %s: nuovo stop %.2f non è più stretto di %.2f — ignorato",
                    symbol, new_stop, current_stop,
                )
                continue

            self._with_retry(
                lambda: self.client.replace_order(
                    stop_order["id"], stop_price=new_stop
                )
            )
            logger.info(
                "modify_stop %s: stop spostato da %.2f → %.2f",
                symbol, current_stop, new_stop,
            )
            return True

        return False

    def has_open_stop_order(self, symbol: str) -> bool:
        """True se esiste uno stop order aperto per il simbolo."""
        open_orders = self._with_retry(self.client.get_open_orders)
        return any(
            o["symbol"] == symbol and o["type"] in ("stop", "stop_limit")
            for o in open_orders
        )

    def place_protective_stop(
        self,
        symbol: str,
        shares: int,
        stop_price: float,
        trade_id: Optional[str] = None,
    ) -> Optional[ExecutionResult]:
        """
        Piazza un ordine STOP di protezione (sell stop, GTC) per un long.

        Se esiste già uno stop per il simbolo lo aggiorna (più stretto) invece
        di crearne un altro. Lo stop resta attivo sul broker anche a bot spento.
        """
        if shares <= 0 or stop_price <= 0:
            return None

        # Se c'è già uno stop, prova a stringerlo invece di duplicare
        existing = [
            o for o in self._with_retry(self.client.get_open_orders)
            if o["symbol"] == symbol and o["type"] in ("stop", "stop_limit")
        ]
        if existing:
            self.modify_stop(symbol, new_stop=stop_price, side="long")
            return None

        trade_id = trade_id or self._new_trade_id(symbol)
        order = self._with_retry(
            lambda: self.client.place_stop_order(
                symbol=symbol, qty=shares, stop_price=stop_price,
                client_order_id=trade_id,
            )
        )
        logger.info("Stop protettivo %s: %d azioni @ %.2f", symbol, shares, stop_price)
        return self._make_result(trade_id, order, "sell")

    def cancel_order(self, order_id: str) -> None:
        """Cancella un ordine per ID Alpaca."""
        self._with_retry(lambda: self.client.cancel_order(order_id))

    def close_position(self, symbol: str) -> Optional[ExecutionResult]:
        """Chiude una posizione a mercato tramite l'API Alpaca."""
        result_dict = self._with_retry(lambda: self.client.close_position(symbol))
        if not result_dict:
            return None
        trade_id = self._new_trade_id(symbol)
        return ExecutionResult(
            trade_id=trade_id,
            order_id=result_dict["id"],
            symbol=symbol,
            status=_ALPACA_TO_STATUS.get(result_dict["status"], OrderStatus.PENDING),
            filled_qty=result_dict["filled_qty"],
            avg_fill_price=result_dict["filled_avg_price"],
            side=result_dict["side"],
        )

    def close_all_positions(self) -> list[ExecutionResult]:
        """Chiude tutte le posizioni aperte a mercato."""
        orders = self._with_retry(self.client.close_all_positions)
        results = []
        for o in orders:
            # Alpaca può non creare l'ordine per un simbolo (es. errore 4xx):
            # in quel caso il dict non ha "id" e va saltato, non fatto esplodere
            # (KeyError 'id' bloccava il primo tentativo di shutdown il 2026-06-10).
            if not o.get("id"):
                logger.warning(
                    "close_all_positions: nessun ordine creato per %s (status %s)",
                    o.get("symbol", "?"), o.get("status", "?"),
                )
                continue
            trade_id = self._new_trade_id(o["symbol"])
            results.append(ExecutionResult(
                trade_id=trade_id,
                order_id=o["id"],
                symbol=o["symbol"],
                status=_ALPACA_TO_STATUS.get(o["status"], OrderStatus.PENDING),
                filled_qty=o["filled_qty"],
                avg_fill_price=o["filled_avg_price"],
                side=o["side"],
            ))
        return results

    def cancel_all_pending(self) -> int:
        """Cancella tutti gli ordini aperti. Restituisce il numero cancellato."""
        open_orders = self._with_retry(self.client.get_open_orders)
        cancelled = 0
        for order in open_orders:
            try:
                self.client.cancel_order(order["id"])
                cancelled += 1
            except APIError as exc:
                logger.warning("Impossibile cancellare ordine %s: %s", order["id"], exc)
        logger.info("Cancellati %d ordini pendenti", cancelled)
        return cancelled

    def sync_order_status(self) -> None:
        """Aggiorna lo stato degli ordini pendenti interrogando l'API."""
        for trade_id, result in list(self._pending.items()):
            try:
                order = self.client.get_order(result.order_id)
                new_status = _ALPACA_TO_STATUS.get(order["status"], OrderStatus.PENDING)
                result.filled_qty     = order["filled_qty"]
                result.avg_fill_price = order["filled_avg_price"]
                result.status         = new_status
                if new_status == OrderStatus.FILLED:
                    self._pending.pop(trade_id, None)
                    if self.on_fill:
                        self.on_fill(result)
            except APIError as exc:
                logger.warning("Impossibile aggiornare ordine %s: %s", result.order_id, exc)

    # ─── Segnali di alto livello ───────────────────────────────────────────────

    def execute_signal(self, signal: TradingSignal) -> ExecutionResult:
        """Converte un TradingSignal in un submit_order."""
        if signal.signal == SignalType.HOLD:
            raise ValueError(f"execute_signal: segnale HOLD per {signal.symbol} — nessun ordine da eseguire")

        side = OrderSide.BUY if signal.signal == SignalType.BUY else OrderSide.SELL
        trade_id = f"{signal.symbol}-{uuid.uuid4().hex[:8]}"

        # Prezzo di ingresso dall'underlying Signal (se disponibile)
        underlying = getattr(signal.risk_decision, "modified_signal", None)
        entry_price = underlying.entry_price if underlying else 0.0

        return self.submit_order(
            symbol=signal.symbol,
            shares=int(signal.shares),
            side=side,
            entry_price=entry_price,
            trade_id=trade_id,
        )

    def execute_batch(self, signals: list[TradingSignal]) -> list[ExecutionResult]:
        """
        Esegue una lista di segnali rispettando la priorità:
        prima le vendite (per liberare liquidità), poi gli acquisti.
        """
        sells  = [s for s in signals if s.signal == SignalType.SELL]
        buys   = [s for s in signals if s.signal == SignalType.BUY]
        holds  = [s for s in signals if s.signal == SignalType.HOLD]

        results: list[ExecutionResult] = []
        for signal in sells + buys:
            try:
                results.append(self.execute_signal(signal))
            except Exception as exc:
                logger.error("execute_batch: errore su %s: %s", signal.symbol, exc)

        # HOLD non genera ordini ma logga per audit
        for signal in holds:
            logger.debug("HOLD %s — nessun ordine emesso", signal.symbol)

        return results

    # ─── Helpers interni ──────────────────────────────────────────────────────

    def _with_retry(self, fn, max_attempts: int = 3, base_delay: float = 1.0):
        for attempt in range(max_attempts):
            try:
                return fn()
            except APIError as exc:
                if is_non_retryable_api_error(exc):
                    logger.warning("APIError non temporaneo: %s — nessun retry.", exc)
                    raise
                if attempt == max_attempts - 1:
                    raise
                wait = base_delay * (2 ** attempt)
                logger.warning(
                    "APIError (tentativo %d/%d): %s — retry in %.1fs",
                    attempt + 1, max_attempts, exc, wait,
                )
                time.sleep(wait)

    def _wait_for_fill(self, trade_id: str, result: ExecutionResult) -> ExecutionResult:
        """Polling dell'ordine fino al fill o al timeout."""
        elapsed = 0.0
        while elapsed < _FILL_TIMEOUT:
            time.sleep(_FILL_POLL_S)
            elapsed += _FILL_POLL_S
            try:
                order = self.client.get_order(result.order_id)
            except APIError:
                break

            status = _ALPACA_TO_STATUS.get(order["status"], OrderStatus.PENDING)
            result.filled_qty     = order["filled_qty"]
            result.avg_fill_price = order["filled_avg_price"]
            result.status         = status

            if status in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                          OrderStatus.REJECTED, OrderStatus.EXPIRED):
                break

        return result

    def _cancel_and_retry_market(
        self,
        trade_id: str,
        result: ExecutionResult,
        symbol: str,
        shares: int,
        side: OrderSide,
        side_str: str,
    ) -> ExecutionResult:
        """Cancella il LIMIT e ritenta a mercato."""
        try:
            self.client.cancel_order(result.order_id)
        except APIError as exc:
            logger.warning("[%s] Impossibile cancellare LIMIT prima del market retry: %s", trade_id, exc)

        logger.info("[%s] LIMIT scaduto — retry MARKET %s %d %s", trade_id, side_str.upper(), shares, symbol)
        try:
            # id distinto (-m) dal LIMIT appena cancellato: evita la collisione
            # "client_order_id must be unique" e abilita il recupero idempotente.
            order = self._with_retry(
                lambda: self.client.place_market_order(
                    symbol=symbol, qty=shares, side=side,
                    client_order_id=f"{trade_id}-m",
                )
            )
            result = self._make_result(trade_id, order, side_str)
            result.retried_market = True
        except APIError as exc:
            result.status = OrderStatus.REJECTED
            result.error  = str(exc)
            logger.error("[%s] Market retry fallito: %s", trade_id, exc)

        return result

    @staticmethod
    def _limit_price(entry_price: float, side: OrderSide) -> float:
        """BUY: +0.1% sopra mercato (aggressivo). SELL: -0.1% sotto mercato."""
        if entry_price <= 0:
            return entry_price
        slip = entry_price * _LIMIT_SLIP
        return entry_price + slip if side == OrderSide.BUY else entry_price - slip

    @staticmethod
    def _new_trade_id(symbol: str) -> str:
        return f"{symbol}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _make_result(trade_id: str, order: dict, side_str: str) -> ExecutionResult:
        return ExecutionResult(
            trade_id=trade_id,
            order_id=order["id"],
            symbol=order["symbol"],
            status=_ALPACA_TO_STATUS.get(order["status"], OrderStatus.PENDING),
            filled_qty=order["filled_qty"],
            avg_fill_price=order["filled_avg_price"],
            side=side_str,
        )
