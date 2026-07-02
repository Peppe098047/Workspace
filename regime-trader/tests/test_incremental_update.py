"""
Test di regressione per il polling incrementale (bug del 2026-06-10).

Bug originale (doppio):
  1. `_bars_to_load` sommava +1 giorno all'ultima barra in cache — corretto per
     il daily, ma con barre INTRADAY chiedeva dati dal futuro → Alpaca
     rispondeva {"message":"end should not be before start"}.
  2. L'errore veniva mascherato: `is_non_retryable_api_error` faceva
     getattr(exc, "code", None), ma `.code` in alpaca-py è una PROPERTY che
     solleva KeyError se il JSON d'errore non contiene "code". Risultato nei
     log: "Errore polling barre: 'code'" a ogni ciclo, pipeline mai eseguita.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from alpaca.common.exceptions import APIError

from broker.api_errors import is_non_retryable_api_error
from data.market_data import MarketDataFeed


def _hourly_bars(n: int = 50) -> pd.DataFrame:
    idx = pd.date_range("2026-06-09 13:00", periods=n, freq="h")
    close = np.linspace(100, 102, n)
    return pd.DataFrame({
        "open": close, "high": close * 1.001, "low": close * 0.999,
        "close": close, "volume": np.full(n, 1e6),
    }, index=idx)


class TestBarsToLoadIntraday:

    def test_intraday_restarts_from_last_bar(self) -> None:
        """Con barre orarie lo start è l'ULTIMA barra in cache, non il giorno dopo."""
        feed = MarketDataFeed(MagicMock(), "1Hour")
        bars = _hourly_bars()
        feed._cache["SPY"] = bars

        start, end = feed._bars_to_load("SPY", lookback_bars=0)

        assert start == bars.index[-1].isoformat()
        assert end is None
        # MAI una data futura rispetto all'ultima barra (il bug originale)
        assert pd.Timestamp(start) <= bars.index[-1]

    def test_daily_still_starts_next_day(self) -> None:
        """Il comportamento daily (+1 giorno) resta invariato."""
        feed = MarketDataFeed(MagicMock(), "1Day")
        bars = _hourly_bars(10)
        feed._cache["SPY"] = bars

        start, _ = feed._bars_to_load("SPY", lookback_bars=0)

        expected = (bars.index[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        assert start == expected

    def test_update_keeps_cache_when_no_new_bars(self) -> None:
        """Risposta vuota dal broker → la cache resta intatta, nessuna eccezione."""
        client = MagicMock()
        client.get_bars.return_value = {}
        feed = MarketDataFeed(client, "1Hour")
        bars = _hourly_bars()
        feed._cache["SPY"] = bars

        result = feed.update(["SPY"])

        assert result["SPY"] is bars
        # La richiesta incrementale parte dall'ultima barra in cache
        assert client.get_bars.call_args.kwargs["start"] == bars.index[-1].isoformat()


class TestApiErrorWithoutCode:

    def test_error_without_code_does_not_raise(self) -> None:
        """APIError senza campo 'code' nel JSON non deve sollevare KeyError."""
        exc = APIError('{"message":"end should not be before start"}')

        assert is_non_retryable_api_error(exc) is False   # non deve esplodere

    def test_error_with_non_retryable_code(self) -> None:
        exc = APIError('{"code":40310000,"message":"potential wash trade detected"}')

        assert is_non_retryable_api_error(exc) is True

    def test_error_matched_by_text(self) -> None:
        exc = APIError('{"message":"opposite side market/stop order exists"}')

        assert is_non_retryable_api_error(exc) is True
