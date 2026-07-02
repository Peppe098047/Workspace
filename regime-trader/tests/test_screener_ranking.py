"""
Test per LiquidityScreener (selezione titoli liquidi) e MomentumRanker
(selezione dei candidati BUY per momentum/relative strength).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from core.screener import LiquidityScreener
from core.ranking import MomentumRanker


# ──────────────────────────────────────────────────────────────────────────────
# HELPER
# ──────────────────────────────────────────────────────────────────────────────

def _daily_bars(price: float, volume: float, n: int = 25) -> list:
    """Lista di barre daily finte in stile alpaca-py (attributi close/volume)."""
    return [SimpleNamespace(close=price, volume=volume) for _ in range(n)]


def _trend_bars(n: int = 60, start: float = 100.0, total_return: float = 0.0,
                noise: float = 0.001, seed: int = 0) -> pd.DataFrame:
    """OHLCV orari sintetici con rendimento totale controllato."""
    rng = np.random.default_rng(seed)
    drift = np.log(1 + total_return) / n
    close = start * np.exp(np.cumsum(rng.normal(drift, noise, n)))
    return pd.DataFrame({
        "open":   close,
        "high":   close * (1 + np.abs(rng.normal(0, 0.002, n))),
        "low":    close * (1 - np.abs(rng.normal(0, 0.002, n))),
        "close":  close,
        "volume": np.full(n, 1_000_000.0),
    }, index=pd.date_range("2026-05-01", periods=n, freq="h"))


SCREENER_CONFIG = {
    "screener": {
        "enabled": True,
        "top_n": 3,
        "lookback_days": 20,
        "min_price": 10.0,
        "min_dollar_volume": 100_000_000,
        "always_include": ["SPY"],
        "universe": ["SPY", "BIG", "MID", "SMALL", "CHEAP"],
    }
}


def _screener_client() -> MagicMock:
    """Universo finto: BIG molto liquido, MID medio, SMALL illiquido, CHEAP sotto prezzo minimo."""
    client = MagicMock()
    client.get_bars.return_value = {
        "SPY":   _daily_bars(price=500.0, volume=50_000_000),   # $25B/g
        "BIG":   _daily_bars(price=200.0, volume=30_000_000),   # $6B/g
        "MID":   _daily_bars(price=100.0, volume=5_000_000),    # $500M/g
        "SMALL": _daily_bars(price=50.0,  volume=100_000),      # $5M/g → scartato
        "CHEAP": _daily_bars(price=4.0,   volume=100_000_000),  # prezzo < 10 → scartato
    }
    return client


# ──────────────────────────────────────────────────────────────────────────────
# SCREENER
# ──────────────────────────────────────────────────────────────────────────────

class TestLiquidityScreener:

    def test_ranks_by_dollar_volume(self) -> None:
        screener = LiquidityScreener(SCREENER_CONFIG)
        results = screener.screen(_screener_client())
        symbols = [r.symbol for r in results]
        assert symbols == ["SPY", "BIG", "MID"]   # ordinati per liquidità decrescente

    def test_filters_illiquid_and_cheap(self) -> None:
        screener = LiquidityScreener(SCREENER_CONFIG)
        symbols = {r.symbol for r in screener.screen(_screener_client())}
        assert "SMALL" not in symbols   # dollar volume sotto il minimo
        assert "CHEAP" not in symbols   # prezzo sotto il minimo

    def test_spy_always_first(self) -> None:
        screener = LiquidityScreener(SCREENER_CONFIG)
        chosen = screener.select_symbols(_screener_client(), fallback=["SPY"])
        assert chosen[0] == "SPY"
        assert len(chosen) == 3   # top_n

    def test_multiple_always_include_preserved_in_order(self) -> None:
        """Con always_include [SPY, SH] entrambi sono operativi, SPY primo."""
        config = {"screener": {**SCREENER_CONFIG["screener"],
                               "always_include": ["SPY", "SH"], "top_n": 4}}
        screener = LiquidityScreener(config)
        chosen = screener.select_symbols(_screener_client(), fallback=["SPY"])

        assert chosen[0] == "SPY"
        assert chosen[1] == "SH"   # l'ETF inverso è sempre tra i simboli operativi
        assert len(chosen) == 4

    def test_fallback_when_disabled(self) -> None:
        config = {"screener": {**SCREENER_CONFIG["screener"], "enabled": False}}
        screener = LiquidityScreener(config)
        assert screener.select_symbols(MagicMock(), fallback=["SPY", "QQQ"]) == ["SPY", "QQQ"]

    def test_fallback_on_api_error(self) -> None:
        screener = LiquidityScreener(SCREENER_CONFIG)
        client = MagicMock()
        client.get_bars.side_effect = RuntimeError("API down")
        assert screener.select_symbols(client, fallback=["SPY", "QQQ"]) == ["SPY", "QQQ"]

    def test_discards_symbols_with_too_few_days(self) -> None:
        screener = LiquidityScreener(SCREENER_CONFIG)
        client = MagicMock()
        client.get_bars.return_value = {
            "SPY": _daily_bars(price=500.0, volume=50_000_000),
            "BIG": _daily_bars(price=200.0, volume=30_000_000, n=3),  # IPO recente
        }
        symbols = {r.symbol for r in screener.screen(client)}
        assert "BIG" not in symbols


# ──────────────────────────────────────────────────────────────────────────────
# RANKER
# ──────────────────────────────────────────────────────────────────────────────

RANKING_CONFIG = {
    "ranking": {
        "enabled": True,
        "lookback_bars": 20,
        "top_k": 2,
        "benchmark": "SPY",
    }
}


def _ranking_bars() -> dict[str, pd.DataFrame]:
    """4 titoli: WIN forte rialzo, OK rialzo lieve, FLAT fermo, LOSE in calo."""
    return {
        "SPY":  _trend_bars(total_return=0.01, seed=1),
        "WIN":  _trend_bars(total_return=0.08, seed=2),
        "OK":   _trend_bars(total_return=0.02, seed=3),
        "FLAT": _trend_bars(total_return=0.00, seed=4),
        "LOSE": _trend_bars(total_return=-0.05, seed=5),
    }


class TestMomentumRanker:

    def test_orders_by_momentum(self) -> None:
        ranker = MomentumRanker(RANKING_CONFIG)
        ranked = ranker.rank(["WIN", "OK", "FLAT", "LOSE"], _ranking_bars())
        symbols = [r.symbol for r in ranked]
        assert symbols[0] == "WIN"
        assert symbols[-1] == "LOSE"

    def test_select_returns_top_k(self) -> None:
        ranker = MomentumRanker(RANKING_CONFIG)
        selected = ranker.select(["WIN", "OK", "FLAT", "LOSE"], _ranking_bars())
        assert "WIN" in selected
        assert "LOSE" not in selected
        assert len(selected) == 2

    def test_held_positions_never_excluded(self) -> None:
        """Un titolo in posizione resta selezionato anche se ultimo in classifica."""
        ranker = MomentumRanker(RANKING_CONFIG)
        selected = ranker.select(
            ["WIN", "OK", "FLAT", "LOSE"], _ranking_bars(), held={"LOSE"},
        )
        assert "LOSE" in selected

    def test_disabled_returns_all(self) -> None:
        config = {"ranking": {**RANKING_CONFIG["ranking"], "enabled": False}}
        ranker = MomentumRanker(config)
        selected = ranker.select(["WIN", "LOSE"], _ranking_bars())
        assert selected == {"WIN", "LOSE"}

    def test_insufficient_data_returns_all(self) -> None:
        """Con dati insufficienti il ranking non filtra nulla (fail-open)."""
        ranker = MomentumRanker(RANKING_CONFIG)
        short_bars = {sym: df.tail(5) for sym, df in _ranking_bars().items()}
        selected = ranker.select(["WIN", "OK", "FLAT", "LOSE"], short_bars)
        assert selected == {"WIN", "OK", "FLAT", "LOSE"}

    def test_inverse_pair_only_best_selected(self) -> None:
        """SPY e SH entrambi nel top → sopravvive solo il migliore, lo slot va al successivo."""
        config = {"ranking": {**RANKING_CONFIG["ranking"],
                              "inverse_pairs": {"SH": "SPY"}}}
        bars = {
            "SPY": _trend_bars(total_return=0.04, seed=11),   # benchmark forte
            "SH":  _trend_bars(total_return=0.06, seed=12),   # inverso ancora più forte
            "OK":  _trend_bars(total_return=0.01, seed=13),
            "LOSE": _trend_bars(total_return=-0.05, seed=14),
        }
        ranker = MomentumRanker(config)
        selected = ranker.select(["SPY", "SH", "OK", "LOSE"], bars)

        # SH (miglior punteggio) entra; SPY è escluso; lo slot libero va a OK
        assert "SH" in selected
        assert "SPY" not in selected
        assert "OK" in selected
        assert len(selected) == 2   # top_k

    def test_inverse_blocked_when_mate_held(self) -> None:
        """Se SPY è già in portafoglio, SH non può ricevere BUY (e viceversa)."""
        config = {"ranking": {**RANKING_CONFIG["ranking"],
                              "inverse_pairs": {"SH": "SPY"}}}
        bars = {
            "SPY": _trend_bars(total_return=0.01, seed=11),
            "SH":  _trend_bars(total_return=0.06, seed=12),   # SH primo in classifica
            "OK":  _trend_bars(total_return=0.02, seed=13),
            "LOSE": _trend_bars(total_return=-0.05, seed=14),
        }
        ranker = MomentumRanker(config)
        selected = ranker.select(["SH", "OK", "LOSE"], bars, held={"SPY"})

        assert "SH" not in selected      # bloccato: l'inverso del titolo posseduto
        assert "SPY" in selected         # le posizioni aperte restano gestite
        assert "OK" in selected

    def test_settings_yaml_pairs_complete(self) -> None:
        """La config reale ha le 4 coppie inverso↔benchmark, simmetriche."""
        import yaml
        cfg = yaml.safe_load(open("config/settings.yaml"))
        ranker = MomentumRanker(cfg)

        for inverse, benchmark in [("SH", "SPY"), ("PSQ", "QQQ"),
                                   ("RWM", "IWM"), ("DOG", "DIA")]:
            assert ranker.inverse_pairs[inverse] == benchmark
            assert ranker.inverse_pairs[benchmark] == inverse
        # E gli inversi sono garantiti tra i simboli operativi
        always = cfg["screener"]["always_include"]
        assert always[0] == "SPY"
        assert {"SH", "RWM", "DOG"}.issubset(set(always))

    def test_no_pairs_configured_no_effect(self) -> None:
        """Senza inverse_pairs il comportamento resta quello di prima."""
        ranker = MomentumRanker(RANKING_CONFIG)
        selected = ranker.select(["WIN", "OK", "FLAT", "LOSE"], _ranking_bars())
        assert len(selected) == 2
        assert "WIN" in selected

    def test_risk_adjustment_penalizes_volatile(self) -> None:
        """A parità di rendimento, il titolo più volatile ha punteggio più basso."""

        def _zigzag(amp: float, n: int = 61, total_return: float = 0.04) -> pd.DataFrame:
            # Rampa deterministica 100 → 104 + zigzag di ampiezza `amp`.
            # Il pattern vale 0 sia all'ultima barra sia a -21 barre (20 % 4 == 0),
            # quindi il rendimento di lookback è IDENTICO: cambia solo l'ATR.
            ramp = np.linspace(100.0, 100.0 * (1 + total_return), n)
            pattern = np.tile([0.0, amp, 0.0, -amp], n // 4 + 1)[:n]
            close = ramp + pattern
            return pd.DataFrame({
                "open":   close,
                "high":   close * 1.001,
                "low":    close * 0.999,
                "close":  close,
                "volume": np.full(n, 1_000_000.0),
            }, index=pd.date_range("2026-05-01", periods=n, freq="h"))

        bars = {
            "SPY":   _zigzag(amp=0.5, total_return=0.01),
            "CALM":  _zigzag(amp=0.0),
            "WILD":  _zigzag(amp=3.0),
            "OTHER": _zigzag(amp=0.5, total_return=0.00),
        }
        ranker = MomentumRanker(RANKING_CONFIG)
        ranked = {r.symbol: r.score for r in ranker.rank(["CALM", "WILD", "OTHER"], bars)}
        assert ranked["CALM"] > ranked["WILD"]
