"""
Test per il report di fine sessione (monitoring/session_report.py)
e per il hook _write_session_report di TradingSession.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import monitoring.session_report as sr
from main import TradingSession
from monitoring.session_report import SessionLogCapture, SessionReporter


def _report_data(**overrides) -> dict:
    data = {
        "session_start":  datetime(2026, 6, 10, 15, 30, 0),
        "session_end":    datetime(2026, 6, 10, 21, 45, 0),
        "mode":           "PAPER",
        "timeframe":      "1Hour",
        "symbols":        ["SPY", "QQQ"],
        "clean_shutdown": True,
        "start_equity":   100_000.0,
        "final_equity":   100_500.0,
        "peak_equity":    101_000.0,
        "trades": [{
            "timestamp": "2026-06-10T16:00:00", "symbol": "SPY", "direction": "LONG",
            "shares": 10, "entry": 500.0, "stop": 495.0, "regime": "BULL", "dry_run": False,
        }],
        "events": [{"time": "16:00:01", "kind": "BUY", "symbol": "SPY", "detail": "10 az @ 500.00"}],
        "regime_changes": [{"time": "17:00:00", "old": "BULL", "new": "NEUTRAL", "probability": 0.7}],
        "warnings": [],
        "open_positions": {},
    }
    data.update(overrides)
    return data


class TestSessionReporter:

    def test_writes_report_and_latest(self, tmp_path) -> None:
        reporter = SessionReporter(reports_dir=tmp_path)
        path = reporter.write(_report_data())

        assert path is not None and path.exists()
        assert path.name == "session_2026-06-10_21-45.md"
        assert (tmp_path / "LATEST.md").read_text() == path.read_text()

    def test_report_content(self, tmp_path) -> None:
        path = SessionReporter(reports_dir=tmp_path).write(_report_data())
        text = path.read_text()

        assert "$100,000.00" in text          # equity iniziale
        assert "+500.00" in text              # P&L
        assert "Nessuna anomalia" in text     # shutdown pulito e flat
        assert "| SPY | LONG" in text.replace("16:00:00 | SPY | LONG", "| SPY | LONG")
        assert "BULL" in text and "NEUTRAL" in text   # cambio regime

    def test_anomalies_on_crash_and_open_positions(self, tmp_path) -> None:
        data = _report_data(
            clean_shutdown=False,
            open_positions={"NVDA": {"qty": 10.0, "avg_entry_price": 200.0, "stop_level": 195.0}},
            warnings=[{"time": "18:00:00", "level": "ERROR", "message": "stop non piazzato"}],
        )
        text = SessionReporter(reports_dir=tmp_path).write(data).read_text()

        assert "ERRORE/crash" in text
        assert "NON è flat" in text
        assert "ERROR/CRITICAL" in text
        assert "stop non piazzato" in text
        assert "Posizioni residue" in text

    def test_never_raises_on_bad_data(self, tmp_path) -> None:
        """Il report non deve mai bloccare lo shutdown, nemmeno con dati rotti."""
        reporter = SessionReporter(reports_dir=tmp_path / "sub")
        path = reporter.write({})   # tutto mancante
        assert path is not None and path.exists()


class TestSessionLogCapture:

    def test_captures_warning_and_above(self) -> None:
        capture = SessionLogCapture()
        test_logger = logging.getLogger("regime-trader-test-capture")
        test_logger.addHandler(capture)
        test_logger.setLevel(logging.DEBUG)

        test_logger.debug("debug ignorato")
        test_logger.info("info ignorata")
        test_logger.warning("warning catturato")
        test_logger.error("errore catturato")

        levels = [r["level"] for r in capture.records]
        assert levels == ["WARNING", "ERROR"]
        assert capture.records[0]["message"] == "warning catturato"

    def test_caps_record_count(self) -> None:
        capture = SessionLogCapture(max_records=10)
        test_logger = logging.getLogger("regime-trader-test-capture-cap")
        test_logger.addHandler(capture)

        for i in range(50):
            test_logger.warning("w%d", i)

        assert len(capture.records) == 10
        assert capture.records[-1]["message"] == "w49"


class TestWriteSessionReportHook:

    def test_session_writes_report_on_shutdown(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sr, "DEFAULT_REPORTS_DIR", tmp_path / "sessions")

        config = {"risk": {}, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
        args = argparse.Namespace(dry_run=True, train_only=False, verbose=False)
        sess = TradingSession(config, args)
        sess._start_equity = 100_000.0
        sess._peak_equity = 100_000.0
        sess._clean_shutdown = True
        sess.alpaca = MagicMock()
        sess.alpaca.get_portfolio_value.return_value = 99_500.0
        sess.tracker = MagicMock()
        sess.tracker.get_all_positions.return_value = {}

        sess._write_session_report()

        files = list((tmp_path / "sessions").glob("session_*.md"))
        assert len(files) == 1
        text = files[0].read_text()
        assert "$99,500.00" in text
        assert (tmp_path / "sessions" / "LATEST.md").exists()

    def test_report_failure_does_not_raise(self, tmp_path, monkeypatch) -> None:
        """Anche se il reporter esplode, lo shutdown prosegue."""
        config = {"risk": {}, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
        args = argparse.Namespace(dry_run=True, train_only=False, verbose=False)
        sess = TradingSession(config, args)

        class _Boom:
            def write(self, data):
                raise RuntimeError("disk full")

        monkeypatch.setattr(sr, "SessionReporter", lambda *a, **kw: _Boom())

        sess._write_session_report()   # non deve sollevare
