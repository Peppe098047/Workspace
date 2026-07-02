"""
Test di regressione per la race square-off ↔ trailing stop (2026-06-10, 21:45).

Sequenza del bug: lo square-off serale cancella gli stop sul broker per poter
vendere; nei secondi successivi la manutenzione trailing vede "stop mancante"
e lo RIPIAZZA, sequestrando le azioni che la chiusura sta vendendo (errore
40310000 "insufficient qty available", e su SH lo stop spurio è stato creato
davvero). Dopo lo square-off — o durante lo shutdown — gli stop non vanno toccati.

Più il parsing di ClosePositionResponse: alpaca-py espone l'ordine creato nel
campo `body`, non `order` — col nome sbagliato ogni chiusura risultava
"nessun ordine creato (status 200)".
"""
from __future__ import annotations

import argparse
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from main import TradingSession
from broker.alpaca_client import AlpacaClient
from tests.test_crash_regression import RISK_CONFIG, _make_crash_bars
from tests.test_strategies import _make_regime_state


def _make_session() -> TradingSession:
    config = {
        "risk": RISK_CONFIG,
        "broker": {"symbols": ["SPY"], "timeframe": "1Hour", "intraday": True},
        "hmm": {}, "strategy": {},
    }
    args = argparse.Namespace(dry_run=False, train_only=False, verbose=False)
    sess = TradingSession(config, args)
    sess.alpaca = MagicMock()
    sess.tracker = MagicMock()
    sess.executor = MagicMock()
    sess._bars_cache = {"SPY": _make_crash_bars()}
    sess.tracker.get_all_positions.return_value = {"SPY": MagicMock(qty=20.0)}
    sess.alpaca.get_latest_bar.return_value = {}
    sess.executor.modify_stop.return_value = False
    sess.executor.has_open_stop_order.return_value = False
    return sess


class TestTrailingHaltedAfterSquareOff:

    def test_no_stop_recreation_after_square_off(self) -> None:
        """Dopo lo square-off di oggi il trailing non deve ripiazzare stop."""
        sess = _make_session()
        sess._squared_off_date = date.today()

        sess._update_trailing_stops(_make_regime_state(label="CRASH"))

        sess.executor.place_protective_stop.assert_not_called()
        sess.executor.modify_stop.assert_not_called()

    def test_no_stop_recreation_during_shutdown(self) -> None:
        """A shutdown avviato il trailing non deve toccare gli stop."""
        sess = _make_session()
        sess._shutdown_ev.set()

        sess._update_trailing_stops(_make_regime_state(label="CRASH"))

        sess.executor.place_protective_stop.assert_not_called()
        sess.executor.modify_stop.assert_not_called()

    def test_trailing_still_works_before_square_off(self) -> None:
        """Prima dello square-off il trailing resta pienamente operativo."""
        sess = _make_session()
        sess._squared_off_date = None

        sess._update_trailing_stops(_make_regime_state(label="CRASH"))

        sess.executor.place_protective_stop.assert_called_once()


class TestClosePositionResponseBody:

    def _client_with_responses(self, responses) -> AlpacaClient:
        client = AlpacaClient.__new__(AlpacaClient)   # niente connessione reale
        client._trading = MagicMock()
        client._trading.close_all_positions.return_value = responses
        return client

    def _fake_order(self, order_id="ord-1", symbol="SPY"):
        return SimpleNamespace(
            id=order_id, client_order_id="cid", symbol=symbol,
            side="sell", order_type="market", status="accepted",
            qty=20, filled_qty=0, filled_avg_price=None,
            limit_price=None, stop_price=None,
            submitted_at=None, filled_at=None,
        )

    def test_order_extracted_from_body_field(self) -> None:
        """L'ordine sta in resp.body (alpaca-py), non in resp.order."""
        resp = SimpleNamespace(symbol="SPY", status=200, body=self._fake_order())
        client = self._client_with_responses([resp])

        results = client.close_all_positions()

        assert len(results) == 1
        assert results[0]["id"] == "ord-1"
        assert results[0]["symbol"] == "SPY"

    def test_response_without_order_still_handled(self) -> None:
        """Risposta senza ordine (errore per quel simbolo) → entry senza id, nessuna eccezione."""
        resp = SimpleNamespace(symbol="MU", status=422, body=None)
        client = self._client_with_responses([resp])

        results = client.close_all_positions()

        assert len(results) == 1
        assert results[0]["symbol"] == "MU"
        assert results[0].get("order") is None
