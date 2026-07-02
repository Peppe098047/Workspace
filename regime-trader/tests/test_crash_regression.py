"""
Test di regressione per i casi-crash: lo stop loss deve stare SEMPRE sotto il prezzo.

Scenario tipico del bug (2026-06-09): in regime CRASH il prezzo crolla sotto la
EMA50, quindi lo stop calcolato come EMA50 − k×ATR finisce SOPRA il prezzo.
Un BUY con stop sopra il prezzo viene eseguito immediatamente dal broker
(vendita istantanea in perdita) oppure rifiutato con errore 422.

Copre i tre livelli del fix:
  1. Strategie: min(stop, price − 0.5×ATR) in tutte e tre le strategie.
  2. _execute_signal: se il fill arriva sotto lo stop, lo stop scende a fill×0.99.
  3. _update_trailing_stops: cap sotto il prezzo + skip se lo stop resta >= prezzo.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from main import TradingSession
from core.regime_strategies import (
    Signal,
    LowVolBullStrategy,
    MidVolCautiousStrategy,
    HighVolDefensiveStrategy,
)
from core.risk_manager import RiskManager, PortfolioState
from tests.test_strategies import _make_regime_state


# ──────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ──────────────────────────────────────────────────────────────────────────────

RISK_CONFIG = {
    "max_risk_per_trade":  0.01,
    "max_exposure":        0.80,
    "max_leverage":        1.0,
    "max_single_position": 0.15,
    "max_concurrent":      5,
    "max_daily_trades":    20,
    "min_position_usd":    100.0,
}


def _make_crash_bars(n: int = 120, crash_bars: int = 15, drop_pct: float = 0.35) -> pd.DataFrame:
    """
    OHLCV sintetici: lungo periodo stabile a ~100, poi crollo violento.

    Risultato: il prezzo finale è MOLTO sotto la EMA50, quindi
    EMA50 − k×ATR > prezzo → lo stop "ingenuo" finirebbe sopra il prezzo.
    """
    rng = np.random.default_rng(42)
    dates = pd.date_range("2026-01-01", periods=n, freq="h")

    flat_n = n - crash_bars
    flat   = 100.0 + rng.normal(0, 0.3, flat_n).cumsum() * 0.05  # ~100, poco rumore
    crash  = np.linspace(flat[-1], flat[-1] * (1 - drop_pct), crash_bars)
    close  = np.concatenate([flat, crash])

    return pd.DataFrame({
        "open":   close * (1 + rng.normal(0, 0.001, n)),
        "high":   close * (1 + np.abs(rng.normal(0, 0.003, n))),
        "low":    close * (1 - np.abs(rng.normal(0, 0.003, n))),
        "close":  close,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    }, index=dates)


def _make_session(tmp_path) -> TradingSession:
    config = {"risk": RISK_CONFIG, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
    args   = argparse.Namespace(dry_run=True, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.risk_mgr = RiskManager(config=RISK_CONFIG, initial_capital=100_000)
    sess.risk_mgr.circuit_breaker._lock_path = tmp_path / "trading_halted.lock"
    sess.risk_mgr.circuit_breaker.initialize(100_000, date.today())
    return sess


def _make_state(equity=100_000.0) -> PortfolioState:
    return PortfolioState(
        equity=equity, cash=equity, buying_power=equity,
        positions={}, daily_pnl=0.0, weekly_pnl=0.0,
        peak_equity=equity, drawdown=0.0,
        current_regime="CRASH", current_date=date.today(),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. STRATEGIE — stop sotto il prezzo anche col prezzo crollato sotto la EMA50
# ──────────────────────────────────────────────────────────────────────────────

class TestStrategiesStopBelowPriceInCrash:

    @pytest.mark.parametrize("strategy_cls", [
        LowVolBullStrategy,
        MidVolCautiousStrategy,
        HighVolDefensiveStrategy,
    ])
    def test_stop_below_price_after_crash(self, strategy_cls) -> None:
        """Con prezzo crollato sotto la EMA50 lo stop deve restare sotto il prezzo."""
        bars  = _make_crash_bars()
        price = float(bars["close"].iloc[-1])
        ema50 = float(bars["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        # Precondizione del bug: il prezzo è davvero sotto la EMA50
        assert price < ema50, "fixture non valida: il crash deve portare il prezzo sotto la EMA50"

        strategy = strategy_cls({"min_confidence": 0.55})
        sig = strategy.generate_signal("SPY", bars, _make_regime_state(label="CRASH"))

        assert sig is not None
        assert sig.stop_loss < sig.entry_price, (
            f"{strategy_cls.__name__}: stop {sig.stop_loss:.2f} "
            f">= prezzo {sig.entry_price:.2f}"
        )
        # Margine minimo di EMA_STOP_MULT×ATR: lo stop non deve essere
        # appiccicato al prezzo (cap coerente col regime, fix 2026-06-10 sera)
        from core.regime_strategies import _atr
        atr = _atr(bars)
        assert sig.stop_loss <= price - strategy.EMA_STOP_MULT * atr + 1e-6

    def test_high_vol_cap_is_one_atr_not_half(self) -> None:
        """
        In crash il cap della strategia high-vol deve essere 1,0×ATR sotto il
        prezzo: col vecchio 0,5 fisso gli stop erano strettissimi proprio nel
        regime più rumoroso (10/10 stop-out nella sessione del 2026-06-10).
        """
        from core.regime_strategies import _atr
        bars  = _make_crash_bars()
        price = float(bars["close"].iloc[-1])
        atr   = _atr(bars)

        strategy = HighVolDefensiveStrategy({"min_confidence": 0.55})
        sig = strategy.generate_signal("SPY", bars, _make_regime_state(label="CRASH"))

        # Il cap scatta (EMA50 sopra il prezzo) → stop = price − 1,0×ATR
        assert sig.stop_loss == pytest.approx(price - 1.0 * atr, rel=1e-6)


# ──────────────────────────────────────────────────────────────────────────────
# 2. _execute_signal — fill sotto lo stop → stop abbassato a fill×0.99
# ──────────────────────────────────────────────────────────────────────────────

class TestExecuteSignalStopBelowFill:

    def _signal(self, entry: float, stop: float) -> Signal:
        return Signal(
            symbol="SPY", direction="LONG", confidence=0.85,
            entry_price=entry, stop_loss=stop, take_profit=None,
            position_size_pct=0.10, leverage=1.0,
            regime_id=0, regime_name="CRASH", regime_probability=0.85,
            timestamp=datetime.now(), reasoning="test", strategy_name="test",
        )

    def test_protective_stop_lowered_below_fill(self, tmp_path) -> None:
        """Fill a 99 con stop teorico 99.5 → lo stop piazzato deve stare sotto il fill."""
        sess = _make_session(tmp_path)
        sess.dry_run  = False
        sess.tracker  = MagicMock()
        sess.executor = MagicMock()
        sess.executor.has_open_stop_order.return_value = False
        # Il mercato gappa: ordine a 100, fill a 99 (sotto lo stop teorico 99.5)
        sess.executor.submit_order.return_value = MagicMock(
            status=MagicMock(value="FILLED"),
            filled_qty=100.0, avg_fill_price=99.0,
            order_id="ord-1", side="buy",
        )

        sig = self._signal(entry=100.0, stop=99.5)
        committed = sess._execute_signal(sig, _make_state(), equity=100_000)

        assert committed > 0
        sess.executor.place_protective_stop.assert_called_once()
        stop_placed = sess.executor.place_protective_stop.call_args.kwargs["stop_price"]
        assert stop_placed < 99.0, f"stop {stop_placed} non è sotto il fill 99.0"
        assert stop_placed == pytest.approx(99.0 * 0.99)
        # Anche il tracker deve conoscere lo stop corretto
        sess.tracker.set_stop_level.assert_called_with("SPY", stop_placed)

    def test_stop_below_fill_left_untouched(self, tmp_path) -> None:
        """Se lo stop è già sotto il fill non va modificato."""
        sess = _make_session(tmp_path)
        sess.dry_run  = False
        sess.tracker  = MagicMock()
        sess.executor = MagicMock()
        sess.executor.has_open_stop_order.return_value = False
        sess.executor.submit_order.return_value = MagicMock(
            status=MagicMock(value="FILLED"),
            filled_qty=100.0, avg_fill_price=100.0,
            order_id="ord-2", side="buy",
        )

        sig = self._signal(entry=100.0, stop=98.0)
        sess._execute_signal(sig, _make_state(), equity=100_000)

        stop_placed = sess.executor.place_protective_stop.call_args.kwargs["stop_price"]
        assert stop_placed == pytest.approx(98.0)

    def test_risk_manager_rejects_stop_above_entry(self, tmp_path) -> None:
        """Difesa a monte: un segnale con stop sopra l'entry non passa il risk check."""
        sess = _make_session(tmp_path)
        sig  = self._signal(entry=100.0, stop=105.0)

        committed = sess._execute_signal(sig, _make_state(), equity=100_000)

        assert committed == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# 3. _update_trailing_stops — stop sempre sotto il prezzo corrente
# ──────────────────────────────────────────────────────────────────────────────

class TestTrailingStopBelowPriceInCrash:

    def _session_with_position(self, tmp_path, bars: pd.DataFrame) -> TradingSession:
        sess = _make_session(tmp_path)
        sess.dry_run  = False
        sess.executor = MagicMock()
        sess.tracker  = MagicMock()
        pos = MagicMock(qty=100.0)
        sess.tracker.get_all_positions.return_value = {"SPY": pos}
        sess._bars_cache = {"SPY": bars}
        return sess

    def test_trailing_stop_stays_below_crashed_price(self, tmp_path) -> None:
        """In crash (prezzo sotto EMA50) il nuovo stop resta sotto il prezzo corrente."""
        bars  = _make_crash_bars()
        price = float(bars["close"].iloc[-1])
        sess  = self._session_with_position(tmp_path, bars)
        sess.executor.modify_stop.return_value = True
        sess.executor.has_open_stop_order.return_value = True

        sess._update_trailing_stops(_make_regime_state(label="CRASH"))

        sess.tracker.set_stop_level.assert_called_once()
        sym, stop_set = sess.tracker.set_stop_level.call_args.args
        assert sym == "SPY"
        assert stop_set < price, f"trailing stop {stop_set} >= prezzo {price}"
        # E lo stop inviato al broker è lo stesso valore
        new_stop_sent = sess.executor.modify_stop.call_args.kwargs["new_stop"]
        assert new_stop_sent == pytest.approx(stop_set)

    def test_missing_broker_stop_recreated_below_price(self, tmp_path) -> None:
        """Stop broker mancante in crash → ripiazzato comunque sotto il prezzo."""
        bars  = _make_crash_bars()
        price = float(bars["close"].iloc[-1])
        sess  = self._session_with_position(tmp_path, bars)
        sess.executor.modify_stop.return_value = False         # nessuno stop da modificare
        sess.executor.has_open_stop_order.return_value = False  # e nessuno aperto sul broker

        sess._update_trailing_stops(_make_regime_state(label="CRASH"))

        sess.executor.place_protective_stop.assert_called_once()
        kwargs = sess.executor.place_protective_stop.call_args.kwargs
        assert kwargs["symbol"] == "SPY"
        assert kwargs["shares"] == 100
        assert kwargs["stop_price"] < price
