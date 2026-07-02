"""
Test per AlpacaClient, OrderExecutor e PositionTracker.

Tutti i test usano mock dell'API Alpaca: nessuna chiamata reale.
"""
import threading
from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

from broker.alpaca_client import AlpacaClient, _LIVE_CONFIRMATION
from broker.order_executor import (
    ExecutionResult,
    OrderExecutor,
    OrderStatus,
    _ALPACA_TO_STATUS,
)
from broker.position_tracker import Position, PositionTracker
from core.signal_generator import SignalType, TradingSignal
from core.hmm_engine import RegimeState
from core.risk_manager import CircuitBreakerLevel, RiskDecision


# ──────────────────────────────────────────────────────────────────────────────
# FIXTURES CONDIVISE
# ──────────────────────────────────────────────────────────────────────────────

def _make_order(
    order_id: str = "ord-1",
    symbol: str = "SPY",
    side: str = "buy",
    status: str = "filled",
    qty: float = 10.0,
    filled_qty: float = 10.0,
    filled_avg_price: float = 450.0,
    limit_price: float = 450.45,
) -> dict:
    return {
        "id":               order_id,
        "client_order_id":  "trade-abc",
        "symbol":           symbol,
        "side":             side,
        "type":             "limit",
        "status":           status,
        "qty":              qty,
        "filled_qty":       filled_qty,
        "filled_avg_price": filled_avg_price,
        "limit_price":      limit_price,
        "submitted_at":     "2024-01-15T10:00:00",
        "filled_at":        "2024-01-15T10:00:05",
    }


def _make_position_dict(
    symbol: str = "SPY",
    qty: float = 10.0,
    avg_entry_price: float = 440.0,
    current_price: float = 450.0,
) -> dict:
    market_value = qty * current_price
    unrealized   = (current_price - avg_entry_price) * qty
    return {
        "symbol":          symbol,
        "qty":             qty,
        "side":            "long",
        "avg_entry_price": avg_entry_price,
        "current_price":   current_price,
        "market_value":    market_value,
        "unrealized_pl":   unrealized,
        "unrealized_plpc": unrealized / (avg_entry_price * qty),
        "cost_basis":      avg_entry_price * qty,
    }


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=AlpacaClient)
    client.paper = True
    client._api_key = "test-key"
    client._secret_key = "test-secret"
    return client


@pytest.fixture
def executor(mock_client: MagicMock) -> OrderExecutor:
    return OrderExecutor(client=mock_client)


@pytest.fixture
def buy_signal() -> TradingSignal:
    regime = RegimeState(
        label="LOW_VOL",
        state_id=0,
        probability=0.85,
        state_probabilities=[0.85, 0.10, 0.05],
        timestamp=datetime.now(),
        is_confirmed=True,
        consecutive_bars=5,
    )
    decision = RiskDecision(
        approved=True,
        modified_signal=None,
        rejection_reason=None,
        size_multiplier=1.0,
    )
    return TradingSignal(
        symbol="SPY",
        signal=SignalType.BUY,
        target_weight=0.20,
        current_weight=0.05,
        shares=10.0,
        regime_state=regime,
        risk_decision=decision,
        confidence=0.85,
    )


@pytest.fixture
def sell_signal(buy_signal: TradingSignal) -> TradingSignal:
    from dataclasses import replace
    return replace(buy_signal, signal=SignalType.SELL)


# ──────────────────────────────────────────────────────────────────────────────
# AlpacaClient — from_env
# ──────────────────────────────────────────────────────────────────────────────

class TestAlpacaClientFromEnv:
    def test_raises_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        with pytest.raises(EnvironmentError, match="ALPACA_API_KEY"):
            AlpacaClient.from_env()

    def test_raises_without_secret_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="ALPACA_SECRET_KEY"):
            AlpacaClient.from_env()

    def test_paper_mode_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        monkeypatch.setenv("ALPACA_PAPER", "true")

        with patch("broker.alpaca_client.TradingClient") as mock_tc, \
             patch("broker.alpaca_client.StockHistoricalDataClient"):
            mock_acc = MagicMock()
            mock_acc.id = "acc-1"
            mock_acc.status = "ACTIVE"
            mock_acc.equity = "10000"
            mock_acc.buying_power = "10000"
            mock_tc.return_value.get_account.return_value = mock_acc

            client = AlpacaClient.from_env()
            assert client.paper is True

    def test_live_mode_requires_confirmation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        monkeypatch.setenv("ALPACA_PAPER", "false")

        with patch("builtins.input", return_value="WRONG"), \
             pytest.raises(SystemExit):
            AlpacaClient.from_env()

    def test_live_mode_proceeds_with_correct_confirmation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALPACA_API_KEY", "key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
        monkeypatch.setenv("ALPACA_PAPER", "false")

        with patch("builtins.input", return_value=_LIVE_CONFIRMATION), \
             patch("broker.alpaca_client.TradingClient") as mock_tc, \
             patch("broker.alpaca_client.StockHistoricalDataClient"):
            mock_acc = MagicMock()
            mock_acc.id = "acc-1"
            mock_acc.status = "ACTIVE"
            mock_acc.equity = "50000"
            mock_acc.buying_power = "50000"
            mock_tc.return_value.get_account.return_value = mock_acc

            client = AlpacaClient.from_env()
            assert client.paper is False


# ──────────────────────────────────────────────────────────────────────────────
# AlpacaClient — retry
# ──────────────────────────────────────────────────────────────────────────────

class TestAlpacaClientRetry:
    def test_retries_on_api_error(self) -> None:
        client = AlpacaClient(api_key="k", secret_key="s", paper=True)
        calls  = 0

        def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise APIError.__new__(APIError)
            return "ok"

        with patch("time.sleep"):
            result = client._with_retry(flaky, max_attempts=3, base_delay=0.01)

        assert result == "ok"
        assert calls == 3

    def test_raises_after_max_attempts(self) -> None:
        client = AlpacaClient(api_key="k", secret_key="s", paper=True)

        def always_fail():
            raise APIError.__new__(APIError)

        with patch("time.sleep"), pytest.raises(APIError):
            client._with_retry(always_fail, max_attempts=3, base_delay=0.01)

    def test_does_not_retry_wash_trade_validation_error(self) -> None:
        client = AlpacaClient(api_key="k", secret_key="s", paper=True)
        calls = 0

        def wash_trade():
            nonlocal calls
            calls += 1
            raise APIError(
                '{"code":40310000,"message":"potential wash trade detected. use complex orders",'
                '"reject_reason":"opposite side market/stop order exists"}'
            )

        with patch("time.sleep"), pytest.raises(APIError):
            client._with_retry(wash_trade, max_attempts=3, base_delay=0.01)

        assert calls == 1

    def test_retries_on_network_error(self) -> None:
        """Regressione HANG 2026-06-15: gli errori di rete (timeout/connessione
        persa) sono temporanei e devono essere ritentati, non propagati."""
        import socket as _socket
        from requests.exceptions import ConnectionError as ReqConnError

        for exc_factory in (
            lambda: _socket.timeout("timed out"),
            lambda: ReqConnError("Remote end closed connection without response"),
        ):
            client = AlpacaClient(api_key="k", secret_key="s", paper=True)
            calls = 0

            def flaky():
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise exc_factory()
                return "ok"

            with patch("time.sleep"):
                result = client._with_retry(flaky, max_attempts=3, base_delay=0.01)

            assert result == "ok"
            assert calls == 3

    def test_raises_network_error_after_max_attempts(self) -> None:
        from requests.exceptions import Timeout as ReqTimeout

        client = AlpacaClient(api_key="k", secret_key="s", paper=True)

        def always_timeout():
            raise ReqTimeout("read timed out")

        with patch("time.sleep"), pytest.raises(ReqTimeout):
            client._with_retry(always_timeout, max_attempts=3, base_delay=0.01)


# ──────────────────────────────────────────────────────────────────────────────
# OrderExecutor — submit_order
# ──────────────────────────────────────────────────────────────────────────────

class TestSubmitOrder:
    def test_buy_submits_limit_above_entry(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        filled = _make_order(status="filled")
        mock_client.place_limit_order.return_value = filled
        mock_client.get_order.return_value = filled

        result = executor.submit_order(
            symbol="SPY", shares=10, side=OrderSide.BUY,
            entry_price=450.0, retry_market=False,
        )

        args = mock_client.place_limit_order.call_args
        limit_price = args.kwargs.get("limit_price") or args.args[2]
        assert limit_price > 450.0, "BUY LIMIT deve essere sopra il prezzo di ingresso"

    def test_sell_submits_limit_below_entry(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        filled = _make_order(status="filled", side="sell")
        mock_client.place_limit_order.return_value = filled
        mock_client.get_order.return_value = filled

        result = executor.submit_order(
            symbol="SPY", shares=10, side=OrderSide.SELL,
            entry_price=450.0, retry_market=False,
        )

        args = mock_client.place_limit_order.call_args
        limit_price = args.kwargs.get("limit_price") or args.args[2]
        assert limit_price < 450.0, "SELL LIMIT deve essere sotto il prezzo di ingresso"

    def test_returns_filled_status_on_immediate_fill(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        filled = _make_order(status="filled")
        mock_client.place_limit_order.return_value = filled
        mock_client.get_order.return_value = filled

        result = executor.submit_order(
            symbol="SPY", shares=10, side=OrderSide.BUY,
            entry_price=450.0, retry_market=False,
        )
        assert result.status == OrderStatus.FILLED

    def test_trade_id_used_as_client_order_id(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        filled = _make_order(status="filled")
        mock_client.place_limit_order.return_value = filled
        mock_client.get_order.return_value = filled

        trade_id = "SPY-test001"
        executor.submit_order(
            symbol="SPY", shares=10, side=OrderSide.BUY,
            entry_price=450.0, trade_id=trade_id, retry_market=False,
        )

        kwargs = mock_client.place_limit_order.call_args.kwargs
        assert kwargs.get("client_order_id") == trade_id

    def test_raises_on_zero_shares(self, executor: OrderExecutor) -> None:
        with pytest.raises(ValueError, match="shares"):
            executor.submit_order("SPY", shares=0, side=OrderSide.BUY, entry_price=450.0)

    def test_retries_market_after_timeout(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        """Dopo 30s senza fill: cancella il LIMIT e invia a mercato."""
        pending = _make_order(status="new", filled_qty=0.0, filled_avg_price=0.0)
        market_filled = _make_order(status="filled", filled_qty=10.0, filled_avg_price=451.0)

        mock_client.place_limit_order.return_value = pending
        mock_client.get_order.return_value = pending       # mai cambia → timeout
        mock_client.place_market_order.return_value = market_filled

        with patch("broker.order_executor._FILL_TIMEOUT", 0.01), \
             patch("broker.order_executor._FILL_POLL_S", 0.005):
            result = executor.submit_order(
                symbol="SPY", shares=10, side=OrderSide.BUY,
                entry_price=450.0, retry_market=True,
            )

        mock_client.cancel_order.assert_called_once()
        mock_client.place_market_order.assert_called_once()
        assert result.retried_market is True

    def test_market_retry_passes_distinct_client_order_id(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        """
        Regressione 2026-06-19: il retry MARKET crashava con
        NameError('client_order_id' non definito) perché place_market_order
        non aveva il parametro. Il retry deve passare un id distinto (-m)
        dal LIMIT cancellato, senza sollevare eccezioni.
        """
        pending = _make_order(status="new", filled_qty=0.0, filled_avg_price=0.0)
        market_filled = _make_order(status="filled", filled_qty=10.0, filled_avg_price=451.0)

        mock_client.place_limit_order.return_value = pending
        mock_client.get_order.return_value = pending
        mock_client.place_market_order.return_value = market_filled

        trade_id = "SPY-deadbeef"
        with patch("broker.order_executor._FILL_TIMEOUT", 0.01), \
             patch("broker.order_executor._FILL_POLL_S", 0.005):
            executor.submit_order(
                symbol="SPY", shares=10, side=OrderSide.BUY,
                entry_price=450.0, trade_id=trade_id, retry_market=True,
            )

        kwargs = mock_client.place_market_order.call_args.kwargs
        assert kwargs.get("client_order_id") == f"{trade_id}-m"


# ──────────────────────────────────────────────────────────────────────────────
# OrderExecutor — execute_batch
# ──────────────────────────────────────────────────────────────────────────────

class TestExecuteBatch:
    def test_sells_before_buys(
        self,
        executor: OrderExecutor,
        mock_client: MagicMock,
        buy_signal: TradingSignal,
        sell_signal: TradingSignal,
    ) -> None:
        """Le vendite devono essere eseguite prima degli acquisti per liberare liquidità."""
        execution_order: list[str] = []

        def track_limit(symbol, qty, limit_price, side, **kw):
            execution_order.append(str(side.value))
            return _make_order(status="filled", side=str(side.value))

        mock_client.place_limit_order.side_effect = track_limit
        mock_client.get_order.return_value = _make_order(status="filled")

        with patch("broker.order_executor._FILL_TIMEOUT", 0.01), \
             patch("broker.order_executor._FILL_POLL_S", 0.005):
            executor.execute_batch([buy_signal, sell_signal])

        assert execution_order[0] == "sell", "La vendita deve venire prima dell'acquisto"

    def test_hold_signals_not_executed(
        self,
        executor: OrderExecutor,
        mock_client: MagicMock,
    ) -> None:
        regime = RegimeState(
            label="LOW_VOL", state_id=0, probability=0.8,
            state_probabilities=[0.8, 0.2], timestamp=datetime.now(),
            is_confirmed=True, consecutive_bars=3,
        )
        decision = RiskDecision(approved=True, modified_signal=None, rejection_reason=None)
        hold = TradingSignal(
            symbol="SPY", signal=SignalType.HOLD,
            target_weight=0.2, current_weight=0.2, shares=0.0,
            regime_state=regime, risk_decision=decision, confidence=0.8,
        )
        executor.execute_batch([hold])
        mock_client.place_limit_order.assert_not_called()
        mock_client.place_market_order.assert_not_called()

    def test_returns_result_per_non_hold_signal(
        self,
        executor: OrderExecutor,
        mock_client: MagicMock,
        buy_signal: TradingSignal,
    ) -> None:
        mock_client.place_limit_order.return_value = _make_order(status="filled")
        mock_client.get_order.return_value = _make_order(status="filled")

        with patch("broker.order_executor._FILL_TIMEOUT", 0.01), \
             patch("broker.order_executor._FILL_POLL_S", 0.005):
            results = executor.execute_batch([buy_signal])

        assert len(results) == 1
        assert isinstance(results[0], ExecutionResult)


# ──────────────────────────────────────────────────────────────────────────────
# OrderExecutor — retry logic
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryLogic:
    def test_retry_on_api_error(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        filled = _make_order(status="filled")
        mock_client.place_limit_order.side_effect = [
            APIError.__new__(APIError),
            filled,
        ]
        mock_client.get_order.return_value = filled

        with patch("time.sleep"), \
             patch("broker.order_executor._FILL_TIMEOUT", 0.01), \
             patch("broker.order_executor._FILL_POLL_S", 0.005):
            result = executor.submit_order(
                symbol="SPY", shares=10, side=OrderSide.BUY,
                entry_price=450.0, retry_market=False,
            )

        assert mock_client.place_limit_order.call_count == 2
        assert result.status == OrderStatus.FILLED

    def test_raises_after_max_attempts(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        mock_client.place_limit_order.side_effect = APIError.__new__(APIError)

        with patch("time.sleep"), pytest.raises(APIError):
            executor.submit_order(
                symbol="SPY", shares=10, side=OrderSide.BUY,
                entry_price=450.0, retry_market=False,
            )

    def test_does_not_retry_non_temporary_api_error(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        mock_client.place_limit_order.side_effect = APIError(
            '{"code":40310000,"message":"potential wash trade detected. use complex orders",'
            '"reject_reason":"opposite side market/stop order exists"}'
        )

        with patch("time.sleep"), pytest.raises(APIError):
            executor.submit_order(
                symbol="SPY", shares=10, side=OrderSide.BUY,
                entry_price=450.0, retry_market=False,
            )

        assert mock_client.place_limit_order.call_count == 1


# ──────────────────────────────────────────────────────────────────────────────
# OrderExecutor — cancel orders
# ──────────────────────────────────────────────────────────────────────────────

class TestCancelOrders:
    def test_cancel_all_pending_returns_count(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        mock_client.get_open_orders.return_value = [
            _make_order("o1", status="new"),
            _make_order("o2", status="new"),
        ]
        count = executor.cancel_all_pending()
        assert count == 2
        assert mock_client.cancel_order.call_count == 2

    def test_cancel_continues_on_partial_failure(
        self, executor: OrderExecutor, mock_client: MagicMock
    ) -> None:
        mock_client.get_open_orders.return_value = [
            _make_order("o1", status="new"),
            _make_order("o2", status="new"),
        ]
        mock_client.cancel_order.side_effect = [None, APIError.__new__(APIError)]

        count = executor.cancel_all_pending()
        assert count == 1   # solo il primo è andato a buon fine


# ──────────────────────────────────────────────────────────────────────────────
# OrderExecutor — on_fill callback
# ──────────────────────────────────────────────────────────────────────────────

class TestOnFillCallback:
    def test_on_fill_called_after_fill(
        self, mock_client: MagicMock
    ) -> None:
        received: list[ExecutionResult] = []
        executor = OrderExecutor(client=mock_client, on_fill=received.append)

        filled = _make_order(status="filled")
        mock_client.place_limit_order.return_value = filled
        mock_client.get_order.return_value = filled

        with patch("broker.order_executor._FILL_TIMEOUT", 0.01), \
             patch("broker.order_executor._FILL_POLL_S", 0.005):
            executor.submit_order(
                symbol="SPY", shares=10, side=OrderSide.BUY,
                entry_price=450.0, retry_market=False,
            )

        assert len(received) == 1
        assert received[0].status == OrderStatus.FILLED

    def test_on_fill_not_called_when_rejected(
        self, mock_client: MagicMock
    ) -> None:
        received: list[ExecutionResult] = []
        executor = OrderExecutor(client=mock_client, on_fill=received.append)

        pending  = _make_order(status="new", filled_qty=0.0, filled_avg_price=0.0)
        rejected = _make_order(status="rejected", filled_qty=0.0, filled_avg_price=0.0)

        mock_client.place_limit_order.return_value = pending
        mock_client.get_order.return_value = rejected
        mock_client.place_market_order.side_effect = APIError.__new__(APIError)

        with patch("broker.order_executor._FILL_TIMEOUT", 0.01), \
             patch("broker.order_executor._FILL_POLL_S", 0.005), \
             patch("time.sleep"):
            executor.submit_order(
                symbol="SPY", shares=10, side=OrderSide.BUY,
                entry_price=450.0, retry_market=False,
            )

        assert len(received) == 0


# ──────────────────────────────────────────────────────────────────────────────
# PositionTracker — sync e record_fill
# ──────────────────────────────────────────────────────────────────────────────

class TestPositionTracker:
    def test_sync_populates_positions(self, mock_client: MagicMock) -> None:
        mock_client.get_positions.return_value = [
            _make_position_dict("SPY", qty=10.0, avg_entry_price=440.0, current_price=450.0),
            _make_position_dict("QQQ", qty=5.0,  avg_entry_price=360.0, current_price=370.0),
        ]
        tracker = PositionTracker(client=mock_client)
        tracker.sync()
        assert "SPY" in tracker.get_all_positions()
        assert "QQQ" in tracker.get_all_positions()

    def test_sync_preserves_stop_level(self, mock_client: MagicMock) -> None:
        """sync() non deve sovrascrivere i metadati locali già presenti."""
        tracker = PositionTracker(client=mock_client)
        tracker._positions["SPY"] = Position(
            symbol="SPY", qty=10.0, avg_entry_price=440.0, current_price=445.0,
            market_value=4450.0, unrealized_pnl=50.0, unrealized_pnl_pct=0.011,
            side="long", stop_level=430.0, regime_at_entry="LOW_VOL",
        )
        mock_client.get_positions.return_value = [
            _make_position_dict("SPY", qty=10.0, avg_entry_price=440.0, current_price=450.0),
        ]
        tracker.sync()
        assert tracker.get_position("SPY").stop_level == 430.0
        assert tracker.get_position("SPY").regime_at_entry == "LOW_VOL"

    def test_record_fill_buy_creates_position(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill(symbol="SPY", qty=10.0, price=450.0, side="buy")
        pos = tracker.get_position("SPY")
        assert pos is not None
        assert pos.qty == 10.0
        assert pos.avg_entry_price == 450.0

    def test_record_fill_buy_averages_entry(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill(symbol="SPY", qty=10.0, price=440.0, side="buy")
        tracker.record_fill(symbol="SPY", qty=10.0, price=460.0, side="buy")
        pos = tracker.get_position("SPY")
        assert pos.qty == 20.0
        assert pos.avg_entry_price == pytest.approx(450.0)

    def test_record_fill_ignores_duplicate_fill_id(self, mock_client: MagicMock) -> None:
        """Lo stesso fill può arrivare sia da polling sia da TradingStream: va contato una volta."""
        tracker = PositionTracker(client=mock_client)

        tracker.record_fill(symbol="SPY", qty=10.0, price=450.0, side="buy", fill_id="ord-1")
        tracker.record_fill(symbol="SPY", qty=10.0, price=450.0, side="buy", fill_id="ord-1")

        pos = tracker.get_position("SPY")
        assert pos is not None
        assert pos.qty == 10.0
        assert pos.market_value == pytest.approx(4500.0)

    def test_record_fill_sell_reduces_qty(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill(symbol="SPY", qty=10.0, price=440.0, side="buy")
        tracker.record_fill(symbol="SPY", qty=5.0,  price=450.0, side="sell")
        pos = tracker.get_position("SPY")
        assert pos.qty == 5.0

    def test_record_fill_sell_removes_position(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill(symbol="SPY", qty=10.0, price=440.0, side="buy")
        tracker.record_fill(symbol="SPY", qty=10.0, price=450.0, side="sell")
        assert tracker.get_position("SPY") is None

    def test_record_fill_sell_accumulates_realized_pnl(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill(symbol="SPY", qty=10.0, price=440.0, side="buy")
        tracker.record_fill(symbol="SPY", qty=10.0, price=460.0, side="sell")
        # P&L = (460 - 440) * 10 = 200
        assert tracker._realized_pnl == pytest.approx(200.0)

    def test_update_prices_recalculates_pnl(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill(symbol="SPY", qty=10.0, price=440.0, side="buy")
        tracker.update_prices({"SPY": 460.0})
        pos = tracker.get_position("SPY")
        assert pos.current_price == 460.0
        assert pos.unrealized_pnl == pytest.approx(200.0)

    def test_get_weights_sums_to_exposure(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill("SPY", qty=10.0, price=450.0, side="buy")
        tracker.record_fill("QQQ", qty=5.0,  price=360.0, side="buy")
        portfolio_value = 10000.0
        weights = tracker.get_weights(portfolio_value)
        total_exposure = tracker.get_total_exposure(portfolio_value)
        assert abs(sum(weights.values()) - total_exposure) < 1e-9

    def test_sync_detects_closed_positions(self, mock_client: MagicMock) -> None:
        """sync() deve rilevare le posizioni chiuse dall'esterno e aggiornare il P&L realizzato."""
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill("SPY", qty=10.0, price=440.0, side="buy")
        tracker._positions["SPY"].unrealized_pnl = 100.0

        # Alpaca non riporta più SPY → la posizione è stata chiusa esternamente
        mock_client.get_positions.return_value = []
        tracker.sync()

        assert tracker.get_position("SPY") is None
        assert tracker._realized_pnl == pytest.approx(100.0)

    def test_fill_callback_triggered(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        fills: list[tuple] = []
        tracker.on_fill(lambda sym, qty, price, side: fills.append((sym, side)))

        tracker.record_fill("SPY", qty=10.0, price=450.0, side="buy")
        assert fills == [("SPY", "buy")]

    def test_set_stop_level(self, mock_client: MagicMock) -> None:
        tracker = PositionTracker(client=mock_client)
        tracker.record_fill("SPY", qty=10.0, price=450.0, side="buy")
        tracker.set_stop_level("SPY", 435.0)
        assert tracker.get_position("SPY").stop_level == 435.0


# ──────────────────────────────────────────────────────────────────────────────
# ModifyStop
# ──────────────────────────────────────────────────────────────────────────────

class TestModifyStop:
    def _stop_order(self, order_id: str, stop_price: float) -> dict:
        return {
            "id":         order_id,
            "symbol":     "SPY",
            "type":       "stop",
            "status":     "new",
            "qty":        10.0,
            "filled_qty": 0.0,
            "filled_avg_price": 0.0,
            "stop_price": stop_price,
            "limit_price": None,
            "side":       "sell",
            "submitted_at": None,
            "filled_at":    None,
            "client_order_id": "",
        }

    def test_tightens_stop_for_long(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        mock_client.get_open_orders.return_value = [self._stop_order("s1", stop_price=430.0)]
        result = executor.modify_stop("SPY", new_stop=435.0, side="long")
        assert result is True
        mock_client.replace_order.assert_called_once_with("s1", stop_price=435.0)

    def test_ignores_wider_stop_for_long(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        mock_client.get_open_orders.return_value = [self._stop_order("s1", stop_price=435.0)]
        result = executor.modify_stop("SPY", new_stop=420.0, side="long")
        assert result is False
        mock_client.replace_order.assert_not_called()

    def test_returns_false_when_no_stop_order(self, executor: OrderExecutor, mock_client: MagicMock) -> None:
        mock_client.get_open_orders.return_value = []
        result = executor.modify_stop("SPY", new_stop=435.0)
        assert result is False


class TestSubmitIdempotent:
    """Recupero idempotente sul client_order_id duplicato (bug 2026-06-17:
    'client_order_id must be unique' faceva fallire un ordine già accettato)."""

    def _client(self):
        c = AlpacaClient(api_key="k", secret_key="s", paper=True)
        c._trading = MagicMock()
        return c

    def _dup_error(self):
        return APIError('{"code":40010001,"message":"client_order_id must be unique"}')

    def test_recovers_existing_order_on_duplicate(self) -> None:
        """Se il client_order_id esiste già, recupera l'ordine invece di fallire."""
        c = self._client()
        c._trading.submit_order.side_effect = self._dup_error()
        existing = MagicMock(name="existing_order")
        c._trading.get_order_by_client_id.return_value = existing
        with patch("time.sleep"):
            result = c._submit_idempotent(req=MagicMock(), client_order_id="SPY-abc123")
        assert result is existing
        c._trading.get_order_by_client_id.assert_called_once_with("SPY-abc123")

    def test_no_duplicate_order_created(self) -> None:
        """Sul duplicato NON deve reinviare un secondo ordine (niente doppione)."""
        c = self._client()
        c._trading.submit_order.side_effect = self._dup_error()
        c._trading.get_order_by_client_id.return_value = MagicMock()
        with patch("time.sleep"):
            c._submit_idempotent(req=MagicMock(), client_order_id="SPY-abc123")
        # submit_order chiamato una sola volta (no retry futili: 40010001 non-retryable)
        assert c._trading.submit_order.call_count == 1

    def test_reraises_if_existing_not_found(self) -> None:
        """Se l'ordine non è recuperabile, propaga l'errore (no silenzio)."""
        c = self._client()
        c._trading.submit_order.side_effect = self._dup_error()
        c._trading.get_order_by_client_id.side_effect = Exception("not found")
        with patch("time.sleep"), pytest.raises(APIError):
            c._submit_idempotent(req=MagicMock(), client_order_id="SPY-abc123")
