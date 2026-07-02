"""
Test per il tetto di esposizione TOTALE legato al regime (fix 2026-06-10).

Prima del fix: in regime CRASH l'allocazione 60% della strategia veniva cappata
al 15% per singolo titolo, ma con 5 posizioni il portafoglio saliva al 75%.
Ora l'allocazione di regime vale come tetto TOTALE: min(max_exposure, 60%) = 60%.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from main import TradingSession
from core.regime_strategies import Signal
from core.risk_manager import RiskManager
from tests.test_sizing import RISK_CONFIG, _make_state


def _make_session(tmp_path) -> TradingSession:
    config = {"risk": RISK_CONFIG, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
    args   = argparse.Namespace(dry_run=True, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.risk_mgr = RiskManager(config=RISK_CONFIG, initial_capital=100_000)
    sess.risk_mgr.circuit_breaker._lock_path = tmp_path / "trading_halted.lock"
    sess.risk_mgr.circuit_breaker.initialize(100_000, date.today())
    return sess


def _signal(symbol="SPY", size=0.60, leverage=1.0, regime="CRASH") -> Signal:
    return Signal(
        symbol=symbol, direction="LONG", confidence=0.9,
        entry_price=100.0, stop_loss=98.0, take_profit=None,
        position_size_pct=size, leverage=leverage,
        regime_id=0, regime_name=regime, regime_probability=0.9,
        timestamp=datetime.now(), reasoning="test", strategy_name="test",
    )


class TestEffectiveMaxExposure:

    def test_crash_regime_caps_total_at_60pct(self, tmp_path) -> None:
        """Allocazione regime 60% → tetto totale 60%, non 80%."""
        sess = _make_session(tmp_path)
        signals = [_signal(s, size=0.60) for s in ["SPY", "QQQ", "NVDA"]]
        assert sess._effective_max_exposure(signals) == pytest.approx(0.60)

    def test_low_vol_keeps_risk_cap(self, tmp_path) -> None:
        """Allocazione 95% (leva cappata a 1.0) → vince il max_exposure 80%."""
        sess = _make_session(tmp_path)
        signals = [_signal("SPY", size=0.95, leverage=1.25, regime="BULL")]
        assert sess._effective_max_exposure(signals) == pytest.approx(0.80)

    def test_uncertainty_halves_total_cap(self, tmp_path) -> None:
        """Sizing dimezzato dall'incertezza (30%) → anche il tetto totale scende al 30%."""
        sess = _make_session(tmp_path)
        signals = [_signal("SPY", size=0.30)]
        assert sess._effective_max_exposure(signals) == pytest.approx(0.30)

    def test_no_signals_falls_back_to_risk_cap(self, tmp_path) -> None:
        sess = _make_session(tmp_path)
        assert sess._effective_max_exposure([]) == pytest.approx(0.80)

    def test_leverage_never_raises_cap_above_max_leverage(self, tmp_path) -> None:
        """La leva 1.25 del segnale non gonfia il tetto: viene cappata a max_leverage."""
        sess = _make_session(tmp_path)
        signals = [_signal("SPY", size=0.60, leverage=1.25)]
        # 0.60 × min(1.25, 1.0) = 0.60, NON 0.75
        assert sess._effective_max_exposure(signals) == pytest.approx(0.60)


class TestCrashBatchStaysWithin60pct:

    def test_total_committed_capped_at_regime_allocation(self, tmp_path) -> None:
        """
        Simula il batch del loop live in CRASH: 7 segnali al 60%, budget dal
        tetto di regime. Il capitale totale impegnato non supera il 60%.
        """
        sess = _make_session(tmp_path)
        equity  = 100_000.0
        symbols = ["SPY", "QQQ", "NVDA", "MU", "GOOGL", "IWM", "AAPL"]
        signals = [_signal(s, size=0.60) for s in symbols]

        budget = equity * sess._effective_max_exposure(signals)   # 60.000
        total = 0.0
        for sig in signals:
            spent = sess._execute_signal(sig, _make_state(equity), equity=equity,
                                         exposure_budget=budget)
            budget -= spent
            total += spent

        # Tetto di regime rispettato (60%), non più il 75% del solo cap per titolo
        assert total <= equity * 0.60 + 100
        assert total > equity * 0.50   # e il budget viene comunque usato
