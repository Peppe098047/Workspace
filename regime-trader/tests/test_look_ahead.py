"""
Test look-ahead bias — verifica che ogni predizione al tempo T dipenda
ESCLUSIVAMENTE da obs_0:T, mai da dati futuri.

Il test chiave confronta:
  predict_regime_filtered(data[0:T])[-1]          # storia corta
  predict_regime_filtered(data[0:T+100])[T-1]     # storia più lunga

Se il forward algorithm è implementato correttamente (no Viterbi),
i due valori devono essere identici: aggiungere osservazioni future
non deve modificare la stima filtrata passata.
"""
import numpy as np
import pandas as pd
import pytest

from data.feature_engineering import FeatureEngineer, FEATURE_COLUMNS
from core.hmm_engine import HMMEngine


# ──────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ──────────────────────────────────────────────────────────────────────────────

HMM_CONFIG = {
    "n_candidates":    [2, 3],
    "n_init":          3,
    "covariance_type": "full",
    "stability_bars":  3,
    "flicker_window":  20,
    "flicker_threshold": 4,
}


def _make_ohlcv(n: int, seed: int = 0) -> pd.DataFrame:
    """Genera OHLCV sintetici con due regimi alternati ogni 100 barre."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2018-01-01", periods=n, freq="B")

    # Alterna regime low-vol / high-vol ogni 100 barre
    sigmas = np.where((np.arange(n) // 100) % 2 == 0, 0.005, 0.020)
    log_ret = rng.normal(0.0003, sigmas)
    close = 100.0 * np.exp(np.cumsum(log_ret))

    return pd.DataFrame({
        "open":   close * (1 + rng.normal(0, 0.002, n)),
        "high":   close * (1 + np.abs(rng.normal(0, 0.005, n))),
        "low":    close * (1 - np.abs(rng.normal(0, 0.005, n))),
        "close":  close,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    }, index=dates)


def _build_features(n_bars: int, seed: int = 0) -> pd.DataFrame:
    ohlcv = _make_ohlcv(n_bars, seed=seed)
    fe    = FeatureEngineer()
    return fe.compute(ohlcv)


def _train_engine(features: pd.DataFrame) -> HMMEngine:
    engine = HMMEngine(config=HMM_CONFIG)
    engine.fit(features)
    return engine


# ──────────────────────────────────────────────────────────────────────────────
# TEST LOOK-AHEAD — OBBLIGATORIO
# ──────────────────────────────────────────────────────────────────────────────

class TestForwardAlgorithmNoLookAhead:
    """
    Il forward algorithm filtrato deve soddisfare la proprietà di causalità:
    la predizione a T non cambia se si aggiungono osservazioni future.
    """

    def test_no_look_ahead_bias(self) -> None:
        """
        Regime at T must be identical with data[0:T] vs data[0:T+100].

        Questo test fallisce se si usa model.predict() (Viterbi) invece
        del forward algorithm, perché Viterbi rivede gli stati passati
        usando osservazioni future.
        """
        # Genera abbastanza dati per training + holdout
        all_features = _build_features(n_bars=1400, seed=42)
        assert len(all_features) >= 1000, "Dati insufficienti per il test"

        # Training su tutta la storia disponibile (1 solo modello, stesso modello)
        engine = _train_engine(all_features)

        # Punto di confronto: T = 700 (all'interno della storia)
        T = 700

        short_features = all_features.iloc[:T]
        long_features  = all_features.iloc[:T + 100]

        states_short = engine.predict_regime_filtered(short_features)
        states_long  = engine.predict_regime_filtered(long_features)

        regime_at_T_short = states_short[-1]        # ultimo elemento di data[0:T]
        regime_at_T_long  = states_long[T - 1]      # elemento T-1 di data[0:T+100]

        assert regime_at_T_short == regime_at_T_long, (
            f"LOOK-AHEAD BIAS RILEVATO: "
            f"regime con data[0:{T}] = {regime_at_T_short}, "
            f"ma con data[0:{T + 100}] all'indice {T - 1} = {regime_at_T_long}. "
            f"Il modello sta usando dati futuri per rivedere stime passate."
        )

    def test_incremental_update_matches_batch(self) -> None:
        """
        update() incrementale deve produrre gli stessi stati di
        predict_regime_filtered() batch sulla stessa sequenza.

        Verifica la coerenza tra le due modalità di inferenza.
        """
        all_features = _build_features(n_bars=1000, seed=7)
        engine_batch = _train_engine(all_features)

        # Usa lo stesso modello per entrambe le modalità
        engine_incr = HMMEngine(config=HMM_CONFIG)
        engine_incr.model        = engine_batch.model
        engine_incr.n_states     = engine_batch.n_states
        engine_incr._regime_info = engine_batch._regime_info

        batch_states = engine_batch.predict_regime_filtered(all_features)

        incr_states = []
        for i in range(len(all_features)):
            rs = engine_incr.update(all_features.iloc[i])
            # Confrontiamo il raw state interno (prima del stability filter)
            # accedendo all'ultimo elemento di _raw_state_history
            incr_states.append(list(engine_incr._raw_state_history)[-1])

        np.testing.assert_array_equal(
            batch_states,
            np.array(incr_states),
            err_msg=(
                "update() e predict_regime_filtered() producono stati diversi. "
                "L'implementazione del forward algorithm è incoerente."
            ),
        )


# ──────────────────────────────────────────────────────────────────────────────
# TEST FEATURE ENGINEERING — NO LOOK-AHEAD
# ──────────────────────────────────────────────────────────────────────────────

class TestFeatureEngineeringCausality:
    """Verifica che tutti i feature usino solo dati storici."""

    def test_no_nan_injection_by_forward_fill(self) -> None:
        """I feature non devono usare forward fill o bfill che introduce look-ahead."""
        ohlcv    = _make_ohlcv(600)
        fe       = FeatureEngineer()
        features = fe.compute(ohlcv)
        # Se ci sono NaN residui (post dropna) c'è un problema nella pipeline
        assert not features.isna().any().any(), (
            "Feature contengono NaN dopo dropna — la pipeline ha un bug"
        )

    def test_nan_prefix_before_dropna(self) -> None:
        """
        Prima di dropna, le prime righe devono avere NaN (warm-up period).
        Se tutte le righe hanno valori dal giorno 1, il feature sta usando
        dati che non esistono (es. riempimento artificiale).
        """
        ohlcv = _make_ohlcv(600)
        fe    = FeatureEngineer()

        close = ohlcv["close"]
        high  = ohlcv["high"]
        low   = ohlcv["low"]
        vol   = ohlcv["volume"]

        from data.feature_engineering import (
            log_returns, realized_vol, dist_from_sma, adx
        )

        # SMA 200 richiede almeno 200 barre → le prime 199 devono essere NaN
        dist200 = dist_from_sma(close, window=200)
        assert dist200.iloc[:199].isna().all(), (
            "dist_sma200 non ha NaN nelle prime 199 barre. "
            "Possibile look-ahead o forward fill."
        )

        # Realized vol 20 richiede almeno 20 barre → le prime 19 NaN
        lr    = log_returns(close)
        rvol  = realized_vol(lr, window=20)
        assert rvol.iloc[:19].isna().all(), (
            "realized_vol_20 non ha NaN nelle prime 19 barre."
        )

    def test_feature_value_unchanged_by_future_bars(self) -> None:
        """
        Il valore di ogni feature alla barra T non deve cambiare
        quando si aggiungono barre future (da T+1 in poi).
        """
        from data.feature_engineering import (
            log_returns, realized_vol, rsi, normalized_atr
        )

        ohlcv_short = _make_ohlcv(300)
        ohlcv_long  = _make_ohlcv(400)  # stessa storia fino a barra 299

        # Stesso seed → stessa storia nelle prime 300 barre
        ohlcv_short = _make_ohlcv(300, seed=99)
        ohlcv_long  = _make_ohlcv(400, seed=99)

        close_s = ohlcv_short["close"]
        close_l = ohlcv_long["close"]

        rsi_s = rsi(close_s, period=14)
        rsi_l = rsi(close_l, period=14)

        # RSI alla barra 200 deve essere uguale in entrambe le serie
        pd.testing.assert_series_equal(
            rsi_s.iloc[200:300].reset_index(drop=True),
            rsi_l.iloc[200:300].reset_index(drop=True),
            check_names=False,
            rtol=1e-10,
        )
