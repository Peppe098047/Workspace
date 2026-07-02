"""
Test per le migliorie del 2026-06-10 sera (punti 1-4 del piano "pensare in grande"):
trade journal aggregato, notifiche Telegram, filtro orario d'ingresso.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from main import TradingSession
from monitoring.alerts import AlertManager
from monitoring.session_report import SessionReporter
from monitoring.trade_journal import TradeJournal

ET = ZoneInfo("America/New_York")


def _session_json(end: str, start_eq: float, final_eq: float,
                  orders: int = 3, clean: bool = True) -> dict:
    return {
        "session_start": end[:11] + "15:30:00",
        "session_end":   end,
        "mode":          "PAPER",
        "clean_shutdown": clean,
        "start_equity":  start_eq,
        "final_equity":  final_eq,
        "trades":        [{"symbol": "SPY"}] * orders,
        "events": [
            {"kind": "CLOSE", "symbol": "SPY", "detail": "20 az @ 730.00"},      # stop-out
            {"kind": "CLOSE", "symbol": "QQQ", "detail": "square-off serale"},   # marker
            {"kind": "CLOSE", "symbol": "QQQ", "detail": "15 az @ 700.00"},      # fill square-off
        ],
        "warnings": [{"level": "ERROR", "message": "x"}] if not clean else [],
    }


# ──────────────────────────────────────────────────────────────────────────────
# TRADE JOURNAL
# ──────────────────────────────────────────────────────────────────────────────

class TestTradeJournal:

    def _write_sessions(self, tmp_path) -> None:
        (tmp_path / "session_2026-06-09_21-45.json").write_text(
            json.dumps(_session_json("2026-06-09 21:45:00", 100_000, 99_000)))
        (tmp_path / "session_2026-06-10_21-48.json").write_text(
            json.dumps(_session_json("2026-06-10 21:48:00", 99_000, 99_500, clean=False)))

    def test_aggregates_sessions(self, tmp_path) -> None:
        self._write_sessions(tmp_path)
        journal = TradeJournal(sessions_dir=tmp_path, journal_path=tmp_path / "journal.md")

        sessions = journal.load_sessions()

        assert len(sessions) == 2
        assert sessions[0].pnl == pytest.approx(-1000.0)   # ordinati cronologicamente
        assert sessions[1].pnl == pytest.approx(500.0)
        assert sessions[0].stop_outs == 1   # 2 CLOSE − 1 square-off
        assert sessions[1].errors == 1

    def test_writes_markdown(self, tmp_path) -> None:
        self._write_sessions(tmp_path)
        journal = TradeJournal(sessions_dir=tmp_path, journal_path=tmp_path / "journal.md")

        path = journal.update()

        text = path.read_text()
        assert "P&L cumulativo | $-500.00" in text
        assert "Sessioni in utile | 1/2" in text
        assert "⚠️ crash" in text   # la sessione non pulita è marcata

    def test_no_sessions_returns_none(self, tmp_path) -> None:
        journal = TradeJournal(sessions_dir=tmp_path, journal_path=tmp_path / "journal.md")
        assert journal.update() is None

    def test_session_reporter_writes_json(self, tmp_path) -> None:
        """SessionReporter produce anche il JSON che alimenta il journal."""
        reporter = SessionReporter(reports_dir=tmp_path)
        reporter.write({
            "session_start": datetime(2026, 6, 10, 15, 30),
            "session_end":   datetime(2026, 6, 10, 21, 45),
            "start_equity":  100_000.0, "final_equity": 99_900.0,
            "clean_shutdown": True, "mode": "PAPER",
        })

        json_files = list(tmp_path.glob("session_*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text())
        assert data["final_equity"] == 99_900.0


# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────

class TestTelegram:

    def test_telegram_channel_called_when_configured(self) -> None:
        mgr = AlertManager(telegram_token="tok123", telegram_chat_id="42")
        with patch("monitoring.alerts.urllib.request.urlopen") as mock_open:
            sent = mgr.session_summary(
                pnl=-20.92, pnl_pct=-0.02, equity=96_981.29,
                n_orders=5, duration="0:30:42", clean_shutdown=True,
            )

        assert sent is True
        mock_open.assert_called_once()
        req = mock_open.call_args.args[0]
        assert "api.telegram.org/bottok123/sendMessage" in req.full_url
        payload = json.loads(req.data.decode())
        assert payload["chat_id"] == "42"
        assert "-20.92" in payload["text"]

    def test_telegram_skipped_when_not_configured(self) -> None:
        mgr = AlertManager()   # nessun token
        with patch("monitoring.alerts.urllib.request.urlopen") as mock_open:
            mgr.session_summary(pnl=0, pnl_pct=0, equity=100_000,
                                n_orders=0, duration="0:01", clean_shutdown=True)
        mock_open.assert_not_called()

    def test_trade_events_not_rate_limited(self) -> None:
        """Due fill consecutivi → due notifiche (il rate limit NON si applica ai trade)."""
        mgr = AlertManager(telegram_token="tok", telegram_chat_id="42")
        with patch("monitoring.alerts.urllib.request.urlopen") as mock_open:
            first  = mgr.trade_event("BUY", "SH", "424 az @ 34.27")
            second = mgr.trade_event("STOP", "SH", "stop @ 33.35")

        assert first is True and second is True
        assert mock_open.call_count == 2

    def test_log_event_sends_trade_notification(self, tmp_path) -> None:
        """_log_event inoltra BUY/SELL/CLOSE/STOP alle notifiche, ma NON gli SKIP."""
        config = {"risk": {}, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
        args = argparse.Namespace(dry_run=False, train_only=False, verbose=False)
        sess = TradingSession(config, args)
        sess.alerts = MagicMock()

        sess._log_event("BUY", "SH", "424 az @ 34.27")
        sess._log_event("SKIP", "SPY", "fuori dal ranking")

        sess.alerts.trade_event.assert_called_once_with("BUY", "SH", "424 az @ 34.27")

    def test_telegram_failure_does_not_raise(self) -> None:
        mgr = AlertManager(telegram_token="tok", telegram_chat_id="42")
        with patch("monitoring.alerts.urllib.request.urlopen",
                   side_effect=OSError("rete giù")):
            sent = mgr.session_summary(pnl=0, pnl_pct=0, equity=100_000,
                                       n_orders=0, duration="0:01", clean_shutdown=True)
        assert sent is True   # gli altri canali hanno funzionato


# ──────────────────────────────────────────────────────────────────────────────
# BLACKOUT APERTURA
# ──────────────────────────────────────────────────────────────────────────────

class TestOpeningBlackout:

    def _session(self, minutes: int = 30) -> TradingSession:
        config = {"risk": {}, "broker": {"symbols": ["SPY"],
                                         "no_entry_first_minutes": minutes},
                  "hmm": {}, "strategy": {}}
        args = argparse.Namespace(dry_run=True, train_only=False, verbose=False)
        return TradingSession(config, args)

    def test_blocked_in_first_half_hour(self) -> None:
        sess = self._session()
        at_9_45 = datetime(2026, 6, 11, 9, 45, tzinfo=ET)
        assert sess._in_opening_blackout(now_et=at_9_45) is True

    def test_open_exactly_at_open(self) -> None:
        sess = self._session()
        at_open = datetime(2026, 6, 11, 9, 30, tzinfo=ET)
        assert sess._in_opening_blackout(now_et=at_open) is True

    def test_free_after_blackout(self) -> None:
        sess = self._session()
        at_10_15 = datetime(2026, 6, 11, 10, 15, tzinfo=ET)
        assert sess._in_opening_blackout(now_et=at_10_15) is False

    def test_free_before_open(self) -> None:
        sess = self._session()
        premarket = datetime(2026, 6, 11, 9, 0, tzinfo=ET)
        assert sess._in_opening_blackout(now_et=premarket) is False

    def test_disabled_with_zero(self) -> None:
        sess = self._session(minutes=0)
        at_9_45 = datetime(2026, 6, 11, 9, 45, tzinfo=ET)
        assert sess._in_opening_blackout(now_et=at_9_45) is False
