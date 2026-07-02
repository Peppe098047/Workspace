"""
Test per il ciclo di manutenzione stop (_run_stop_maintenance).

Il ciclo gira tra una barra oraria e l'altra (default ogni 5 min) e deve:
  - aggiornare i trailing stop usando i prezzi CORRENTI del broker,
  - ricreare stop broker mancanti,
  - NON generare nuovi segnali né ordini di ingresso,
  - non fare nulla in dry-run o a conto flat.
"""
from __future__ import annotations

import argparse
from datetime import date
from unittest.mock import MagicMock

import pytest

from main import TradingSession
from core.risk_manager import RiskManager
from tests.test_crash_regression import RISK_CONFIG, _make_crash_bars
from tests.test_strategies import _make_regime_state


def _make_session(tmp_path, dry_run: bool = False) -> TradingSession:
    config = {"risk": RISK_CONFIG, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
    args   = argparse.Namespace(dry_run=dry_run, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.risk_mgr = RiskManager(config=RISK_CONFIG, initial_capital=100_000)
    sess.risk_mgr.circuit_breaker._lock_path = tmp_path / "trading_halted.lock"
    sess.risk_mgr.circuit_breaker.initialize(100_000, date.today())
    sess.alpaca   = MagicMock()
    sess.tracker  = MagicMock()
    sess.executor = MagicMock()
    # Snapshot isolato: _save_snapshot legge tracker/risk_mgr mockati senza side effect
    sess._save_snapshot = MagicMock()
    return sess


class TestStopMaintenance:

    def test_noop_in_dry_run(self, tmp_path) -> None:
        sess = _make_session(tmp_path, dry_run=True)
        sess._run_stop_maintenance()
        sess.tracker.sync.assert_not_called()
        sess.executor.modify_stop.assert_not_called()

    def test_noop_when_flat(self, tmp_path) -> None:
        sess = _make_session(tmp_path)
        sess.tracker.get_all_positions.return_value = {}
        sess._run_stop_maintenance()
        sess.alpaca.get_latest_bar.assert_not_called()
        sess.executor.modify_stop.assert_not_called()

    def test_uses_live_price_for_trailing(self, tmp_path) -> None:
        """Il prezzo live (più basso del close in cache) abbassa il tetto dello stop."""
        bars = _make_crash_bars()
        cached_price = float(bars["close"].iloc[-1])
        live_price   = cached_price * 0.90   # il mercato è sceso ancora dall'ultima barra

        sess = _make_session(tmp_path)
        sess._bars_cache = {"SPY": bars}
        sess._last_regime_state = _make_regime_state(label="CRASH")
        sess.tracker.get_all_positions.return_value = {"SPY": MagicMock(qty=100.0)}
        sess.alpaca.get_latest_bar.return_value = {"SPY": {"close": live_price}}
        sess.executor.modify_stop.return_value = True
        sess.executor.has_open_stop_order.return_value = True

        sess._run_stop_maintenance()

        sess.tracker.set_stop_level.assert_called_once()
        _, stop_set = sess.tracker.set_stop_level.call_args.args
        # Lo stop deve stare sotto il prezzo LIVE, non solo sotto il close in cache
        assert stop_set < live_price

    def test_recreates_missing_broker_stop(self, tmp_path) -> None:
        bars = _make_crash_bars()
        sess = _make_session(tmp_path)
        sess._bars_cache = {"SPY": bars}
        sess._last_regime_state = _make_regime_state(label="CRASH")
        sess.tracker.get_all_positions.return_value = {"SPY": MagicMock(qty=50.0)}
        sess.alpaca.get_latest_bar.return_value = {}
        sess.executor.modify_stop.return_value = False
        sess.executor.has_open_stop_order.return_value = False

        sess._run_stop_maintenance()

        sess.executor.place_protective_stop.assert_called_once()
        assert sess.executor.place_protective_stop.call_args.kwargs["shares"] == 50

    def test_no_new_orders_generated(self, tmp_path) -> None:
        """La manutenzione non deve MAI inviare ordini di ingresso."""
        bars = _make_crash_bars()
        sess = _make_session(tmp_path)
        sess._bars_cache = {"SPY": bars}
        sess._last_regime_state = _make_regime_state(label="NEUTRAL")
        sess.tracker.get_all_positions.return_value = {"SPY": MagicMock(qty=100.0)}
        sess.alpaca.get_latest_bar.return_value = {}
        sess.executor.modify_stop.return_value = True
        sess.executor.has_open_stop_order.return_value = True

        sess._run_stop_maintenance()

        sess.executor.submit_order.assert_not_called()

    def test_survives_price_fetch_failure(self, tmp_path) -> None:
        """Se i prezzi live falliscono, usa il close in cache senza crashare."""
        bars = _make_crash_bars()
        sess = _make_session(tmp_path)
        sess._bars_cache = {"SPY": bars}
        sess._last_regime_state = _make_regime_state(label="CRASH")
        sess.tracker.get_all_positions.return_value = {"SPY": MagicMock(qty=100.0)}
        sess.alpaca.get_latest_bar.side_effect = RuntimeError("API down")
        sess.executor.modify_stop.return_value = True
        sess.executor.has_open_stop_order.return_value = True

        sess._run_stop_maintenance()   # non deve sollevare

        sess.tracker.set_stop_level.assert_called_once()

    def test_fallback_regime_when_no_bar_processed(self, tmp_path) -> None:
        """Dopo un riavvio con recovery (nessuna barra ancora) usa il fallback HIGH_VOL."""
        bars = _make_crash_bars()
        sess = _make_session(tmp_path)
        sess._bars_cache = {"SPY": bars}
        sess._last_regime_state = None
        sess.tracker.get_all_positions.return_value = {"SPY": MagicMock(qty=100.0)}
        sess.alpaca.get_latest_bar.return_value = {}
        sess.executor.modify_stop.return_value = True
        sess.executor.has_open_stop_order.return_value = True

        sess._run_stop_maintenance()   # non deve sollevare

        sess.tracker.set_stop_level.assert_called_once()
