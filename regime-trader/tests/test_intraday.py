"""
Test per la modalità intraday: square-off serale (flat overnight, zero gap risk).
"""
import argparse
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from main import TradingSession
from broker.position_tracker import Position


def _session(intraday=True, square_off_minutes=15) -> TradingSession:
    config = {
        "broker": {"symbols": ["SPY"], "timeframe": "1Hour",
                   "intraday": intraday, "square_off_minutes": square_off_minutes},
        "risk": {}, "hmm": {}, "strategy": {},
    }
    args = argparse.Namespace(dry_run=False, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.alpaca   = MagicMock()
    sess.tracker  = MagicMock()
    sess.executor = MagicMock()
    sess.alerts   = None
    return sess


def _clock(minutes_to_close: float, is_open: bool = True) -> dict:
    """Orologio mock con la chiusura tra N minuti."""
    nc = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_close)
    return {"is_open": is_open, "next_close": nc.isoformat()}


def _pos(symbol="SPY") -> Position:
    return Position(
        symbol=symbol, qty=10, avg_entry_price=500.0, current_price=505.0,
        market_value=5050.0, unrealized_pnl=50.0, unrealized_pnl_pct=0.01, side="long",
    )


class TestSquareOff:
    def test_closes_positions_near_close(self) -> None:
        """Mancano 10 min alla chiusura (< 15) → chiude tutto."""
        sess = _session(square_off_minutes=15)
        sess.alpaca.get_clock.return_value = _clock(10)
        sess.tracker.get_all_positions.return_value = {"SPY": _pos()}
        sess.executor.close_all_positions.return_value = []

        result = sess._check_eod_closeout()

        assert result is True
        sess.executor.close_all_positions.assert_called_once()
        assert sess._squared_off_date == date.today()

    def test_does_not_close_when_far_from_close(self) -> None:
        """Mancano 60 min alla chiusura (> 15) → non chiude."""
        sess = _session(square_off_minutes=15)
        sess.alpaca.get_clock.return_value = _clock(60)
        sess.tracker.get_all_positions.return_value = {"SPY": _pos()}

        result = sess._check_eod_closeout()

        assert result is False
        sess.executor.close_all_positions.assert_not_called()

    def test_skips_if_already_squared_off_today(self) -> None:
        """Se lo square-off è già stato fatto oggi → non lo rifà."""
        sess = _session()
        sess._squared_off_date = date.today()

        result = sess._check_eod_closeout()

        assert result is True
        sess.executor.close_all_positions.assert_not_called()

    def test_no_squareoff_when_not_intraday(self) -> None:
        """In modalità multi-day (intraday=False) lo square-off non si attiva."""
        sess = _session(intraday=False)
        sess.alpaca.get_clock.return_value = _clock(5)
        sess.tracker.get_all_positions.return_value = {"SPY": _pos()}

        result = sess._check_eod_closeout()

        assert result is False
        sess.executor.close_all_positions.assert_not_called()

    def test_no_squareoff_when_market_closed(self) -> None:
        """A mercato chiuso non si fa nulla."""
        sess = _session()
        sess.alpaca.get_clock.return_value = _clock(5, is_open=False)

        result = sess._check_eod_closeout()

        assert result is False
        sess.executor.close_all_positions.assert_not_called()

    def test_marks_squared_off_even_with_no_positions(self) -> None:
        """Vicino alla chiusura senza posizioni → marca comunque lo square-off fatto."""
        sess = _session()
        sess.alpaca.get_clock.return_value = _clock(10)
        sess.tracker.get_all_positions.return_value = {}

        result = sess._check_eod_closeout()

        assert result is True
        assert sess._squared_off_date == date.today()
        sess.executor.close_all_positions.assert_not_called()

    def test_dry_run_disables_squareoff(self) -> None:
        """In dry-run lo square-off è disattivato (nessun ordine reale)."""
        sess = _session()
        sess.dry_run = True
        sess.alpaca.get_clock.return_value = _clock(5)

        result = sess._check_eod_closeout()

        assert result is False


class TestTrailingStopRecovery:
    def test_recreates_missing_protective_stop(self) -> None:
        """Se Alpaca non ha stop aperti per una posizione, il trailing deve ripiazzarlo."""
        sess = _session()
        sess.tracker.get_all_positions.return_value = {"SPY": _pos("SPY")}
        sess.executor.modify_stop.return_value = False
        sess.executor.has_open_stop_order.return_value = False
        sess._bars_cache = {
            "SPY": pd.DataFrame({
                "high":  [101.0 + i * 0.1 for i in range(60)],
                "low":   [99.0 + i * 0.1 for i in range(60)],
                "close": [100.0 + i * 0.1 for i in range(60)],
            })
        }
        regime = argparse.Namespace(label="CRASH")

        sess._update_trailing_stops(regime)

        sess.executor.modify_stop.assert_called_once()
        sess.executor.place_protective_stop.assert_called_once()
        kwargs = sess.executor.place_protective_stop.call_args.kwargs
        assert kwargs["symbol"] == "SPY"
        assert kwargs["shares"] == 10
        assert kwargs["stop_price"] < sess._bars_cache["SPY"]["close"].iloc[-1]


class TestShutdownFlatRetry:
    def test_retries_close_until_tracker_reports_flat(self) -> None:
        """Lo shutdown deve ritentare se dopo la prima chiusura resta una posizione."""
        sess = _session()
        sess.tracker.get_all_positions.side_effect = [
            {"SPY": _pos("SPY")},
            {"TSLA": _pos("TSLA")},
            {"TSLA": _pos("TSLA")},
            {},
        ]

        with patch("time.sleep"):
            result = sess._close_positions_until_flat(timeout_s=5.0, retry_delay_s=0.0)

        assert result is True
        assert sess.executor.cancel_all_pending.call_count == 2
        assert sess.executor.close_all_positions.call_count == 2
        assert sess.tracker.sync.call_count == 2

    def test_returns_false_when_positions_remain_after_timeout(self) -> None:
        """Se Alpaca continua a riportare posizioni aperte, lo shutdown segnala fallimento."""
        sess = _session()
        sess.tracker.get_all_positions.return_value = {"SPY": _pos("SPY")}

        with patch("time.monotonic", side_effect=[0.0, 2.0]), patch("time.sleep"):
            result = sess._close_positions_until_flat(timeout_s=1.0, retry_delay_s=0.0)

        assert result is False
        sess.executor.close_all_positions.assert_called_once()


class TestMarketOpenGate:
    """Gate ingressi sul mercato regolare aperto (bug 2026-06-17: entrate a
    mercato chiuso/extended hours tenute overnight). Vedi _market_open_now."""

    def test_open_when_clock_open(self) -> None:
        sess = _session()
        sess.alpaca.get_clock.return_value = {"is_open": True}
        assert sess._market_open_now() is True

    def test_closed_when_clock_closed(self) -> None:
        sess = _session()
        sess.alpaca.get_clock.return_value = {"is_open": False}
        assert sess._market_open_now() is False

    def test_closed_on_clock_error(self) -> None:
        """Su errore di lettura clock: conservativo → niente ingressi (False)."""
        sess = _session()
        sess.alpaca.get_clock.side_effect = Exception("network down")
        assert sess._market_open_now() is False

    def test_dry_run_does_not_block(self) -> None:
        sess = _session()
        sess.dry_run = True
        sess.alpaca.get_clock.return_value = {"is_open": False}
        assert sess._market_open_now() is True
