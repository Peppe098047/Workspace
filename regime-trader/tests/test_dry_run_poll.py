"""
Test per _poll_new_bars: la pipeline dry-run deve scattare al primo polling
del giorno A QUALSIASI ORA (il vecchio vincolo `hour >= 16` rendeva inutili
i collaudi avviati all'apertura del mercato, 15:30 ora italiana).
"""
from __future__ import annotations

import argparse
from datetime import date
from unittest.mock import MagicMock

from main import TradingSession


def _make_session() -> TradingSession:
    config = {"risk": {}, "broker": {"symbols": ["SPY"]}, "hmm": {}, "strategy": {}}
    args = argparse.Namespace(dry_run=True, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.data_feed = MagicMock()
    sess.alpaca = MagicMock()
    sess._on_new_day = MagicMock()
    sess._process_all_symbols = MagicMock()
    return sess


class TestPollNewBars:

    def test_pipeline_runs_on_first_poll_any_hour(self) -> None:
        """Il primo polling del giorno esegue la pipeline, senza vincoli d'orario."""
        sess = _make_session()
        sess._last_processed_date = None

        sess._poll_new_bars()

        sess.data_feed.update.assert_called_once_with(["SPY"])
        sess._on_new_day.assert_called_once()
        sess._process_all_symbols.assert_called_once()
        assert sess._last_processed_date == date.today()

    def test_pipeline_not_repeated_same_day(self) -> None:
        """I polling successivi nello stesso giorno aggiornano i dati ma non rieseguono la pipeline."""
        sess = _make_session()
        sess._last_processed_date = date.today()

        sess._poll_new_bars()

        sess.data_feed.update.assert_called_once()
        sess._process_all_symbols.assert_not_called()

    def test_poll_survives_data_feed_error(self) -> None:
        """Un errore di rete nel polling non deve far crashare il loop."""
        sess = _make_session()
        sess.data_feed.update.side_effect = RuntimeError("API down")

        sess._poll_new_bars()   # non deve sollevare

        sess._process_all_symbols.assert_not_called()
