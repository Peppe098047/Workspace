"""
Test di regressione per i bug della prima sessione live (2026-06-10, 16:22–17:18).

Quattro difetti emersi dal report di sessione:
  1. Lo stream Alpaca invia barre da 1 MINUTO → la pipeline (pensata per barre
     orarie) girava 60 volte l'ora: 79 ordini in 55 minuti.
  2. Dopo uno stop-out il bot ricomprava lo stesso titolo in ~26 secondi
     (il segnale orario diceva ancora LONG): loop stop-out → ricompra.
  3. Top-up da 1 azione a ogni ciclo appena il prezzo si muoveva (churn).
  4. Allo shutdown `close_all_positions` esplodeva con KeyError 'id' se Alpaca
     non creava l'ordine per un simbolo.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from main import TradingSession
from broker.order_executor import OrderExecutor
from tests.test_crash_regression import RISK_CONFIG, _make_crash_bars
from tests.test_sizing import _make_signal
from tests.test_strategies import _make_regime_state


def _make_session(timeframe: str = "1Hour") -> TradingSession:
    config = {
        "risk": {**RISK_CONFIG, "reentry_cooldown_minutes": 30},
        "broker": {"symbols": ["SPY"], "timeframe": timeframe},
        "hmm": {}, "strategy": {},
    }
    args = argparse.Namespace(dry_run=False, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.data_feed = MagicMock()
    sess.data_feed.update.return_value = {}
    sess.alpaca = MagicMock()
    sess.tracker = MagicMock()
    sess.executor = MagicMock()
    sess._on_new_day = MagicMock()
    sess._process_all_symbols = MagicMock()
    return sess


def _bar(ts: str) -> dict:
    return {"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "timestamp": ts}


# ──────────────────────────────────────────────────────────────────────────────
# 1. Gate orario sulle barre-minuto
# ──────────────────────────────────────────────────────────────────────────────

class TestHourlyGate:

    def test_minute_bars_same_hour_run_pipeline_once(self) -> None:
        """60 barre-minuto nella stessa ora → UNA sola esecuzione della pipeline."""
        sess = _make_session()
        for minute in range(60):
            sess._handle_bar_event("SPY", _bar(f"2026-06-10T15:{minute:02d}:00"))

        assert sess._process_all_symbols.call_count == 1

    def test_new_hour_triggers_pipeline_again(self) -> None:
        sess = _make_session()
        sess._handle_bar_event("SPY", _bar("2026-06-10T15:30:00"))
        sess._handle_bar_event("SPY", _bar("2026-06-10T15:59:00"))
        sess._handle_bar_event("SPY", _bar("2026-06-10T16:00:00"))   # nuova ora

        assert sess._process_all_symbols.call_count == 2

    def test_cache_refreshed_before_pipeline(self) -> None:
        """Prima della pipeline la cache va ricaricata via REST (barre orarie complete)."""
        sess = _make_session()
        sess._handle_bar_event("SPY", _bar("2026-06-10T15:30:00"))

        sess.data_feed.update.assert_called_once_with(["SPY"])

    def test_minute_timeframe_processes_every_bar(self) -> None:
        """Con timeframe 1Min (nessun gate) ogni barra esegue la pipeline."""
        sess = _make_session(timeframe="1Min")
        sess._handle_bar_event("SPY", _bar("2026-06-10T15:30:00"))
        sess._handle_bar_event("SPY", _bar("2026-06-10T15:31:00"))

        assert sess._process_all_symbols.call_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# 5. Timestamp timezone-aware dello stream vs datetime naive del bot (crash 2026-06-15)
#    Lo stream Alpaca invia timestamp aware (UTC); il polling e i fallback usano
#    datetime.now() naive. Confrontare naive con aware solleva TypeError e fa
#    cadere il bot ("ARRESTO DA ERRORE"). bar_ts va normalizzato a naive locale.
# ──────────────────────────────────────────────────────────────────────────────

class TestTimezoneAwareBar:

    def test_aware_timestamp_does_not_crash(self) -> None:
        """Barra con timestamp aware (UTC, +00:00) → nessun TypeError, pipeline gira."""
        sess = _make_session()
        sess._handle_bar_event("SPY", _bar("2026-06-15T15:30:00+00:00"))
        assert sess._process_all_symbols.call_count == 1

    def test_aware_z_suffix_does_not_crash(self) -> None:
        """Anche il formato con suffisso 'Z' (UTC) deve essere gestito."""
        sess = _make_session()
        sess._handle_bar_event("SPY", _bar("2026-06-15T15:30:00Z"))
        assert sess._process_all_symbols.call_count == 1

    def test_last_pipeline_ts_normalized_to_naive(self) -> None:
        """Dopo una barra aware, _last_pipeline_ts deve essere naive (no tzinfo)."""
        sess = _make_session()
        sess._handle_bar_event("SPY", _bar("2026-06-15T15:30:00+00:00"))
        assert sess._last_pipeline_ts is not None
        assert sess._last_pipeline_ts.tzinfo is None

    def test_mixed_naive_then_aware_does_not_crash(self) -> None:
        """
        Scenario reale del crash: il polling imposta _last_pipeline_ts naive,
        poi arriva una barra aware dallo stream → il confronto non deve esplodere.
        """
        sess = _make_session()
        sess._last_pipeline_ts = datetime(2020, 1, 1, 0, 0, 0)   # naive (come da _poll_new_bars)
        # Non deve sollevare "can't compare offset-naive and offset-aware datetimes"
        sess._handle_bar_event("SPY", _bar("2026-06-15T16:00:00+00:00"))
        assert sess._process_all_symbols.call_count == 1


# ──────────────────────────────────────────────────────────────────────────────
# 2. Cooldown anti-churn dopo una vendita
# ──────────────────────────────────────────────────────────────────────────────

class TestReentryCooldown:

    def test_sell_fill_activates_cooldown(self) -> None:
        sess = _make_session()
        sess.tracker.get_position.return_value = None

        sess._on_tracker_fill_event("SPY", qty=20, price=730.63, side="sell")

        assert sess._reentry_blocked("SPY") is True
        assert sess._reentry_blocked("QQQ") is False

    def test_buy_fill_does_not_activate_cooldown(self) -> None:
        sess = _make_session()
        sess._on_tracker_fill_event("SPY", qty=20, price=730.63, side="buy")
        assert sess._reentry_blocked("SPY") is False

    def test_cooldown_expires(self) -> None:
        sess = _make_session()
        sess._reentry_block["SPY"] = datetime.now() - timedelta(seconds=1)
        assert sess._reentry_blocked("SPY") is False

    def test_cooldown_disabled_with_zero_minutes(self) -> None:
        sess = _make_session()
        sess.config["risk"]["reentry_cooldown_minutes"] = 0
        sess.tracker.get_position.return_value = None

        sess._on_tracker_fill_event("SPY", qty=20, price=730.63, side="sell")

        assert sess._reentry_blocked("SPY") is False


# ──────────────────────────────────────────────────────────────────────────────
# 3. Anti-churn: niente top-up sotto l'1% di scarto dal target
# ──────────────────────────────────────────────────────────────────────────────

class TestRebalanceMinDelta:

    def test_tiny_delta_returns_none(self) -> None:
        """Posizione al 14.5% con target 15% → delta 0.5% → nessun ordine."""
        sess = _make_session()
        sig = _make_signal(size=0.15, leverage=1.0)

        adjusted = sess._signal_for_rebalance_delta(
            signal=sig, current_weight=0.145, equity=100_000, exposure_budget=80_000,
        )

        assert adjusted is None

    def test_meaningful_delta_still_works(self) -> None:
        """Posizione a 0 con target 15% → delta 15% → ordine generato."""
        sess = _make_session()
        sig = _make_signal(size=0.15, leverage=1.0)

        adjusted = sess._signal_for_rebalance_delta(
            signal=sig, current_weight=0.0, equity=100_000, exposure_budget=80_000,
        )

        assert adjusted is not None
        assert adjusted.position_size_pct == pytest.approx(0.15, abs=0.01)


# ──────────────────────────────────────────────────────────────────────────────
# 4. close_all_positions tollera risposte senza ordine
# ──────────────────────────────────────────────────────────────────────────────

class TestCloseAllPositionsWithoutOrder:

    def test_skips_entries_without_order_id(self) -> None:
        client = MagicMock()
        client.close_all_positions.return_value = [
            {"symbol": "SPY", "id": "ord-1", "status": "accepted",
             "filled_qty": 20.0, "filled_avg_price": 730.0, "side": "sell"},
            {"symbol": "MU", "status": "422", "order": None},   # ordine NON creato
        ]
        executor = OrderExecutor(client=client)

        results = executor.close_all_positions()   # non deve sollevare KeyError

        assert len(results) == 1
        assert results[0].symbol == "SPY"


# ──────────────────────────────────────────────────────────────────────────────
# 5. Trailing stop col prezzo vivo (non col close stantio della cache)
# ──────────────────────────────────────────────────────────────────────────────

class TestTrailingUsesLivePrices:

    def test_pipeline_trailing_fetches_live_prices(self) -> None:
        """Senza live_prices espliciti il trailing li scarica dal broker."""
        bars = _make_crash_bars()
        sess = _make_session()
        sess._bars_cache = {"SPY": bars}
        sess.tracker.get_all_positions.return_value = {"SPY": MagicMock(qty=100.0)}
        live_price = float(bars["close"].iloc[-1]) * 0.95   # mercato sceso oltre la cache
        sess.alpaca.get_latest_bar.return_value = {"SPY": {"close": live_price}}
        sess.executor.modify_stop.return_value = True
        sess.executor.has_open_stop_order.return_value = True

        sess._update_trailing_stops(_make_regime_state(label="CRASH"))

        sess.alpaca.get_latest_bar.assert_called_once()
        _, stop_set = sess.tracker.set_stop_level.call_args.args
        assert stop_set < live_price   # mai uno stop sopra il prezzo VIVO
