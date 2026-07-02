"""
Alpaca Client — wrapper intorno alle API Alpaca (REST).

Paper trading (default): https://paper-api.alpaca.markets
Live trading: richiede conferma esplicita all'avvio.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Optional

from alpaca.common.exceptions import APIError
from requests.exceptions import RequestException
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
)
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopOrderRequest,
)

from broker.api_errors import is_non_retryable_api_error

logger = logging.getLogger("regime-trader.broker")

_LIVE_CONFIRMATION = "YES I UNDERSTAND THE RISKS"

_TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1Min":  TimeFrame(1, TimeFrameUnit.Minute),
    "5Min":  TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
    "1Day":  TimeFrame(1, TimeFrameUnit.Day),
}


class AlpacaClient:
    """
    Wrapper leggero attorno a alpaca-py per separare la logica di business
    dai dettagli di autenticazione e serializzazione API.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        feed: str = "iex",
    ) -> None:
        """
        Args:
            feed: Feed dati di mercato — "iex" (gratuito, default) o "sip" (a pagamento).
        """
        self.paper = paper
        self._api_key = api_key
        self._secret_key = secret_key
        self._trading: Optional[TradingClient] = None
        self._data: Optional[StockHistoricalDataClient] = None
        # Free tier Alpaca → IEX; abbonamento dati → SIP
        self._feed = DataFeed.SIP if str(feed).lower() == "sip" else DataFeed.IEX

    # ─── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "AlpacaClient":
        """
        Costruisce il client leggendo credenziali da variabili d'ambiente.
        Per il live trading richiede conferma esplicita dell'utente.
        """
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
        paper_str = os.environ.get("ALPACA_PAPER", "true").lower()
        paper = paper_str not in ("false", "0", "no")
        # Feed dati: default IEX (gratuito). Imposta ALPACA_FEED=sip se hai l'abbonamento.
        feed = os.environ.get("ALPACA_FEED", "iex")

        if not api_key or not secret_key:
            raise EnvironmentError(
                "ALPACA_API_KEY e ALPACA_SECRET_KEY devono essere impostati nel .env"
            )

        if not paper:
            print(
                "\n⚠️  LIVE TRADING MODE — stai usando denaro reale.\n"
                "Digita 'YES I UNDERSTAND THE RISKS' per confermare: ",
                end="",
                flush=True,
            )
            confirmation = input().strip()
            if confirmation != _LIVE_CONFIRMATION:
                print("Conferma non ricevuta. Uscita per sicurezza.")
                sys.exit(1)
            logger.warning("Live trading mode attivato dall'utente.")

        client = cls(api_key=api_key, secret_key=secret_key, paper=paper, feed=feed)
        client.connect()
        return client

    # ─── Connessione ──────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Inizializza i client REST e verifica la connessione."""
        self._trading = TradingClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=self.paper,
        )
        self._data = StockHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
        )
        self._health_check()

    def _health_check(self) -> None:
        try:
            acc = self._trading.get_account()
            mode = "PAPER" if self.paper else "LIVE"
            logger.info(
                "[%s] Connessione OK — account=%s status=%s equity=$%.2f buying_power=$%.2f",
                mode, acc.id, acc.status, float(acc.equity), float(acc.buying_power),
            )
        except APIError as exc:
            raise ConnectionError(f"Health check Alpaca fallito: {exc}") from exc

    def _with_retry(self, fn, max_attempts: int = 3, base_delay: float = 1.0):
        """Esegue fn con retry esponenziale su errori API o di rete temporanei."""
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
            except (RequestException, OSError) as exc:
                # Errori di rete (timeout socket, connessione persa,
                # RemoteDisconnected): sempre temporanei → retry. Il timeout
                # globale sul socket (socket.setdefaulttimeout in main._startup)
                # garantisce che la chiamata non resti appesa all'infinito
                # bloccando il main loop — causa radice dell'HANG del 2026-06-15.
                if attempt == max_attempts - 1:
                    raise
                wait = base_delay * (2 ** attempt)
                logger.warning(
                    "Errore di rete (tentativo %d/%d): %s — retry in %.1fs",
                    attempt + 1, max_attempts, exc, wait,
                )
                time.sleep(wait)

    def _submit_idempotent(self, req, client_order_id: Optional[str]):
        """
        Invia un ordine con recupero IDEMPOTENTE sul client_order_id.

        Problema (bug del 2026-06-17): `_with_retry` ritenta la submission con lo
        STESSO client_order_id. Se il primo tentativo ha già registrato l'ordine su
        Alpaca ma la risposta è andata persa (timeout di rete), il retry riceve
        `client_order_id must be unique` (code 40010001) e l'ordine "fallisce" pur
        essendo vivo sul broker → ordine perso/phantom.

        Soluzione: se arriva 40010001, l'ordine con quel client_order_id ESISTE già
        → lo recuperiamo (`get_order_by_client_id`) e lo usiamo, invece di fallire o
        creare un doppione. Il retry su errori di rete resta attivo via `_with_retry`.
        """
        def _do():
            try:
                return self._trading.submit_order(req)
            except APIError as exc:
                try:
                    code = exc.code
                except Exception:
                    code = None
                if code == 40010001 and client_order_id:
                    try:
                        existing = self._trading.get_order_by_client_id(client_order_id)
                    except Exception:
                        existing = None
                    if existing is not None:
                        logger.warning(
                            "client_order_id '%s' già esistente: recuperato ordine %s "
                            "(retry idempotente, nessun doppione).",
                            client_order_id, getattr(existing, "id", "?"),
                        )
                        return existing
                raise
        return self._with_retry(_do)

    def get_order_by_client_id(self, client_order_id: str) -> dict:
        """Recupera un ordine dal suo client_order_id (per recupero idempotente)."""
        order = self._with_retry(lambda: self._trading.get_order_by_client_id(client_order_id))
        return self._order_to_dict(order)

    # ─── Account ──────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        acc = self._with_retry(self._trading.get_account)
        return {
            "id":              str(acc.id),
            "status":          str(acc.status),
            "equity":          float(acc.equity),
            "cash":            float(acc.cash),
            "buying_power":    float(acc.buying_power),
            "portfolio_value": float(acc.portfolio_value),
            "pattern_day_trader": bool(acc.pattern_day_trader),
            "trading_blocked": bool(acc.trading_blocked),
            "account_blocked": bool(acc.account_blocked),
        }

    def get_portfolio_value(self) -> float:
        return float(self._with_retry(self._trading.get_account).portfolio_value)

    def get_buying_power(self) -> float:
        return float(self._with_retry(self._trading.get_account).buying_power)

    def get_available_margin(self) -> float:
        acc = self._with_retry(self._trading.get_account)
        # Il margine disponibile è buying_power oltre alla liquidità cash
        return max(0.0, float(acc.buying_power) - float(acc.cash))

    def is_market_open(self) -> bool:
        return self._with_retry(self._trading.get_clock).is_open

    def get_clock(self) -> dict:
        clock = self._with_retry(self._trading.get_clock)
        return {
            "is_open":    clock.is_open,
            "next_open":  clock.next_open.isoformat() if clock.next_open else None,
            "next_close": clock.next_close.isoformat() if clock.next_close else None,
            "timestamp":  clock.timestamp.isoformat() if clock.timestamp else None,
        }

    # ─── Ordini ───────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        time_in_force: TimeInForce = TimeInForce.DAY,
        client_order_id: Optional[str] = None,
    ) -> dict:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )
        order = self._submit_idempotent(req, client_order_id)
        return self._order_to_dict(order)

    def place_limit_order(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        side: OrderSide,
        time_in_force: TimeInForce = TimeInForce.DAY,
        client_order_id: Optional[str] = None,
    ) -> dict:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            limit_price=round(limit_price, 2),
            side=side,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )
        order = self._submit_idempotent(req, client_order_id)
        return self._order_to_dict(order)

    def place_bracket_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        stop_price: float,
        take_profit_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict:
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import StopLossRequest, TakeProfitRequest

        stop_loss = StopLossRequest(stop_price=round(stop_price, 2))
        take_profit = (
            TakeProfitRequest(limit_price=round(take_profit_price, 2))
            if take_profit_price
            else None
        )
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_order_id=client_order_id,
        )
        order = self._submit_idempotent(req, client_order_id)
        return self._order_to_dict(order)

    def place_stop_order(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        side: OrderSide = OrderSide.SELL,
        time_in_force: TimeInForce = TimeInForce.GTC,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """
        Ordine STOP standalone (protezione). Default SELL per chiudere un long
        se il prezzo scende sotto stop_price. GTC = resta attivo finché non scatta.
        """
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            stop_price=round(stop_price, 2),
            side=side,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )
        order = self._submit_idempotent(req, client_order_id)
        return self._order_to_dict(order)

    def cancel_order(self, order_id: str) -> None:
        self._with_retry(lambda: self._trading.cancel_order_by_id(order_id))

    def cancel_all_orders(self) -> None:
        self._with_retry(self._trading.cancel_orders)

    def replace_order(
        self,
        order_id: str,
        *,
        qty: Optional[float] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> dict:
        req = ReplaceOrderRequest(
            qty=qty,
            limit_price=round(limit_price, 2) if limit_price else None,
            stop_price=round(stop_price, 2) if stop_price else None,
        )
        order = self._with_retry(lambda: self._trading.replace_order_by_id(order_id, req))
        return self._order_to_dict(order)

    def get_open_orders(self) -> list[dict]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._with_retry(lambda: self._trading.get_orders(req))
        return [self._order_to_dict(o) for o in orders]

    def get_order_history(self, limit: int = 100) -> list[dict]:
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit)
        orders = self._with_retry(lambda: self._trading.get_orders(req))
        return [self._order_to_dict(o) for o in orders]

    def get_order(self, order_id: str) -> dict:
        order = self._with_retry(lambda: self._trading.get_order_by_id(order_id))
        return self._order_to_dict(order)

    # ─── Posizioni ────────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        positions = self._with_retry(self._trading.get_all_positions)
        return [self._position_to_dict(p) for p in positions]

    def get_position(self, symbol: str) -> Optional[dict]:
        try:
            pos = self._with_retry(lambda: self._trading.get_open_position(symbol))
            return self._position_to_dict(pos)
        except APIError:
            return None

    def close_position(self, symbol: str) -> Optional[dict]:
        try:
            order = self._with_retry(lambda: self._trading.close_position(symbol))
            return self._order_to_dict(order)
        except APIError as exc:
            logger.warning("Impossibile chiudere posizione %s: %s", symbol, exc)
            return None

    def close_all_positions(self, cancel_orders: bool = True) -> list[dict]:
        # Alpaca ritorna una lista di ClosePositionResponse, NON di Order:
        # ognuno ha .symbol, .status (codice HTTP) e .order (l'ordine creato, se ok).
        responses = self._with_retry(
            lambda: self._trading.close_all_positions(cancel_orders=cancel_orders)
        )
        results: list[dict] = []
        for resp in (responses or []):
            # alpaca-py espone l'ordine creato nel campo `body` (non `order`!):
            # col nome sbagliato ogni chiusura risultava "nessun ordine creato"
            # anche con status 200 (visto il 2026-06-10 alle 21:45).
            order = getattr(resp, "body", None) or getattr(resp, "order", None)
            if order is not None and getattr(order, "id", None) is not None:
                results.append(self._order_to_dict(order))
            else:
                # Nessun ordine creato (es. errore per quel simbolo)
                results.append({
                    "symbol": getattr(resp, "symbol", "?"),
                    "status": str(getattr(resp, "status", "?")),
                    "order":  None,
                })
        return results

    # ─── Dati di mercato ──────────────────────────────────────────────────────

    def get_bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: str,
        end: Optional[str] = None,
    ) -> dict:
        tf = _TIMEFRAME_MAP.get(timeframe, TimeFrame(1, TimeFrameUnit.Day))
        kw: dict = {
            "symbol_or_symbols": symbols, "timeframe": tf,
            "start": start, "feed": self._feed,
        }
        if end:
            kw["end"] = end
        req = StockBarsRequest(**kw)
        bars = self._with_retry(lambda: self._data.get_stock_bars(req))
        # BarSet espone i dati via .data {symbol: [Bar, ...]}; `sym in bars` non funziona
        data = getattr(bars, "data", {})
        return {sym: data[sym] for sym in symbols if sym in data and data[sym]}

    def get_latest_bar(self, symbols: list[str]) -> dict:
        req = StockLatestBarRequest(symbol_or_symbols=symbols, feed=self._feed)
        return self._with_retry(lambda: self._data.get_stock_latest_bar(req))

    def get_latest_quote(self, symbols: list[str]) -> dict:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=self._feed)
        return self._with_retry(lambda: self._data.get_stock_latest_quote(req))

    def get_snapshot(self, symbols: list[str]) -> dict:
        req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=self._feed)
        return self._with_retry(lambda: self._data.get_stock_snapshot(req))

    # ─── Serializzazione interna ───────────────────────────────────────────────

    @staticmethod
    def _order_to_dict(order) -> dict:
        def _val(v, fallback=None):
            if v is None:
                return fallback
            return float(v) if fallback is not None and isinstance(fallback, float) else v

        return {
            "id":               str(order.id),
            "client_order_id":  str(order.client_order_id or ""),
            "symbol":           order.symbol,
            "side":             str(order.side.value) if hasattr(order.side, "value") else str(order.side),
            "type":             str(order.order_type.value) if hasattr(order.order_type, "value") else str(order.order_type),
            "status":           str(order.status.value) if hasattr(order.status, "value") else str(order.status),
            "qty":              float(order.qty or 0),
            "filled_qty":       float(order.filled_qty or 0),
            "filled_avg_price": float(order.filled_avg_price or 0),
            "limit_price":      float(order.limit_price) if order.limit_price else None,
            "stop_price":       float(order.stop_price) if getattr(order, "stop_price", None) else None,
            "submitted_at":     order.submitted_at.isoformat() if order.submitted_at else None,
            "filled_at":        order.filled_at.isoformat() if order.filled_at else None,
        }

    @staticmethod
    def _position_to_dict(pos) -> dict:
        return {
            "symbol":          pos.symbol,
            "qty":             float(pos.qty),
            "side":            str(pos.side.value) if hasattr(pos.side, "value") else str(pos.side),
            "avg_entry_price": float(pos.avg_entry_price),
            "current_price":   float(pos.current_price or 0),
            "market_value":    float(pos.market_value or 0),
            "unrealized_pl":   float(pos.unrealized_pl or 0),
            "unrealized_plpc": float(pos.unrealized_plpc or 0),
            "cost_basis":      float(pos.cost_basis or 0),
        }
