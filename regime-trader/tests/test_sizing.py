"""
Test per il sizing live di _execute_signal:
  - nessuna leva oltre max_leverage (niente margine)
  - cap per singola posizione (max_single_position)
  - rispetto del budget di esposizione del ciclo (max_exposure)
  - uso dell'equity corrente (dinamica)
"""
import argparse
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from main import TradingSession
from core.regime_strategies import Signal
from core.risk_manager import RiskManager, PortfolioState


RISK_CONFIG = {
    "max_risk_per_trade":  0.01,
    "max_exposure":        0.80,
    "max_leverage":        1.0,    # no margine
    "max_single_position": 0.15,
    "max_concurrent":      5,
    "max_daily_trades":    20,
    "min_position_usd":    100.0,
}


def _make_session(tmp_path) -> TradingSession:
    config = {"risk": RISK_CONFIG, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
    args   = argparse.Namespace(dry_run=True, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.risk_mgr = RiskManager(config=RISK_CONFIG, initial_capital=100_000)
    # Isola il lock file in tmp_path per non interferire con altri test/CWD
    sess.risk_mgr.circuit_breaker._lock_path = tmp_path / "trading_halted.lock"
    sess.risk_mgr.circuit_breaker.initialize(100_000, date.today())
    return sess


def _make_signal(symbol="SPY", entry=500.0, stop=480.0, size=0.60, leverage=1.25) -> Signal:
    return Signal(
        symbol=symbol, direction="LONG", confidence=0.85,
        entry_price=entry, stop_loss=stop, take_profit=None,
        position_size_pct=size, leverage=leverage,
        regime_id=0, regime_name="LOW_VOL", regime_probability=0.85,
        timestamp=datetime.now(), reasoning="test", strategy_name="test",
    )


def _make_state(equity=100_000.0) -> PortfolioState:
    return PortfolioState(
        equity=equity, cash=equity, buying_power=equity * 4,
        positions={}, daily_pnl=0.0, weekly_pnl=0.0,
        peak_equity=equity, drawdown=0.0,
        current_regime="LOW_VOL", current_date=date.today(),
    )


class TestNoLeverage:
    def test_leverage_capped_to_max(self, tmp_path) -> None:
        """Signal con leva 1.25 ma max_leverage=1.0 → usa solo capitale proprio."""
        sess = _make_session(tmp_path)
        sig  = _make_signal(size=0.15, leverage=1.25)  # 15% così non lo limita il cap singola
        committed = sess._execute_signal(sig, _make_state(), equity=100_000)
        # Con leva 1.0: 15% di 100k = 15.000, NON 18.750 (che sarebbe con leva 1.25)
        assert committed <= 15_000 * 1.01
        assert committed < 18_000   # esclude la leva 1.25


class TestSinglePositionCap:
    def test_position_capped_at_15pct(self, tmp_path) -> None:
        """Signal che chiede 60% → cappato al 15% dell'equity."""
        sess = _make_session(tmp_path)
        sig  = _make_signal(size=0.60, leverage=1.0)
        committed = sess._execute_signal(sig, _make_state(), equity=100_000)
        assert committed <= 15_000 * 1.01   # ~15% di 100k (più arrotondamento azioni)


class TestExposureBudget:
    def test_respects_budget(self, tmp_path) -> None:
        """Il capitale impegnato non supera il budget di esposizione passato."""
        sess = _make_session(tmp_path)
        sig  = _make_signal(size=0.15, leverage=1.0)
        committed = sess._execute_signal(sig, _make_state(), equity=100_000, exposure_budget=5_000)
        assert committed <= 5_000

    def test_skips_when_budget_too_small(self, tmp_path) -> None:
        """Budget sotto la soglia minima → nessun ordine, ritorna 0."""
        sess = _make_session(tmp_path)
        sig  = _make_signal(entry=500.0, size=0.15)
        committed = sess._execute_signal(sig, _make_state(), equity=100_000, exposure_budget=50)
        assert committed == 0.0

    def test_skips_buy_when_opposite_stop_order_exists(self, tmp_path) -> None:
        """Evita wash-trade Alpaca: BUY bloccato se esiste già uno stop SELL aperto."""
        sess = _make_session(tmp_path)
        sess.dry_run = False
        sess.executor = MagicMock()
        sess.executor.has_open_stop_order.return_value = True
        sig = _make_signal(symbol="NVDA", entry=200.0, stop=190.0, size=0.05, leverage=1.0)

        committed = sess._execute_signal(sig, _make_state(), equity=100_000, exposure_budget=10_000)

        assert committed == 0.0
        sess.executor.submit_order.assert_not_called()
        # Un BUY skippato non deve finire nel report come "ordine inviato"
        assert sess._trade_log == []


class TestRebalanceTargets:
    def test_strategy_size_is_capped_before_rebalance_check(self, tmp_path) -> None:
        """Un segnale 60% va confrontato col target operativo 15%, non col 60% teorico."""
        sess = _make_session(tmp_path)
        sig = _make_signal(size=0.60, leverage=1.0)

        target = sess._effective_target_weight(sig)

        assert target == pytest.approx(0.15)

    def test_existing_position_near_capped_target_is_below_rebalance_threshold(self, tmp_path) -> None:
        """Se una posizione è già vicina al 15%, non deve sembrare lontana dal target 60%."""
        sess = _make_session(tmp_path)
        sig = _make_signal(size=0.60, leverage=1.0)

        target = sess._effective_target_weight(sig)

        assert abs(0.14 - target) < 0.10


class TestDynamicEquity:
    def test_uses_current_equity(self, tmp_path) -> None:
        """Se l'equity cala, il sizing si riduce di conseguenza."""
        sess = _make_session(tmp_path)
        sig  = _make_signal(size=0.15, leverage=1.0)
        big   = sess._execute_signal(sig, _make_state(100_000), equity=100_000)
        small = sess._execute_signal(sig, _make_state(50_000),  equity=50_000)
        # 15% di 50k deve essere circa la metà di 15% di 100k
        assert small < big
        assert small <= 7_500 * 1.05


class TestTotalExposureNeverExceedsCapital:
    def test_sum_of_positions_within_max_exposure(self, tmp_path) -> None:
        """
        Simula il loop del batch: 7 segnali, budget = 80% equity.
        La somma del capitale impegnato non supera l'80% (niente margine).
        """
        sess = _make_session(tmp_path)
        equity = 100_000.0
        budget = equity * RISK_CONFIG["max_exposure"]   # 80.000
        symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"]

        total = 0.0
        for sym in symbols:
            sig = _make_signal(symbol=sym, size=0.60, leverage=1.25)
            spent = sess._execute_signal(sig, _make_state(equity), equity=equity, exposure_budget=budget)
            budget -= spent
            total += spent

        # Esposizione totale mai oltre l'80% dell'equity (con tolleranza per arrotondamenti)
        assert total <= equity * 0.80 + 100
        # E mai oltre il capitale proprio (no margine)
        assert total < equity
