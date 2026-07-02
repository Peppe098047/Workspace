"""
Test per HMMEngine — BIC selection, predizione, stability filter, persistence.
"""
import pickle
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.hmm_engine import HMMEngine, RegimeInfo, RegimeState, REGIME_LABELS
from data.feature_engineering import FeatureEngineer, FEATURE_COLUMNS


# ──────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ──────────────────────────────────────────────────────────────────────────────

HMM_CONFIG = {
    "n_candidates":    [2, 3],   # ridotto per velocità nei test
    "n_init":          3,
    "covariance_type": "full",
    "stability_bars":  3,
    "flicker_window":  20,
    "flicker_threshold": 4,
}


def _make_features(n: int = 700, seed: int = 0) -> pd.DataFrame:
    """Feature sintetici con due regimi evidenti (low vol / high vol)."""
    rng   = np.random.default_rng(seed)
    dates = pd.date_range("2018-01-01", periods=n, freq="B")

    # Alterna low-vol / high-vol ogni 100 barre
    sigmas  = np.where((np.arange(n) // 100) % 2 == 0, 0.004, 0.018)
    log_ret = rng.normal(0.0002, sigmas)
    close   = 100.0 * np.exp(np.cumsum(log_ret))

    ohlcv = pd.DataFrame({
        "open":   close * (1 + rng.normal(0, 0.001, n)),
        "high":   close * (1 + np.abs(rng.normal(0, 0.004, n))),
        "low":    close * (1 - np.abs(rng.normal(0, 0.004, n))),
        "close":  close,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    }, index=dates)

    return FeatureEngineer().compute(ohlcv)


@pytest.fixture(scope="module")
def trained_engine():
    """Engine addestrata una sola volta per tutto il modulo."""
    features = _make_features(n=900, seed=1)
    engine   = HMMEngine(config=HMM_CONFIG)
    engine.fit(features)
    return engine, features


# ──────────────────────────────────────────────────────────────────────────────
# STATO INIZIALE
# ──────────────────────────────────────────────────────────────────────────────

class TestInit:
    def test_not_fitted_initially(self) -> None:
        engine = HMMEngine(config=HMM_CONFIG)
        assert not engine.is_fitted()

    def test_transition_matrix_none_before_fit(self) -> None:
        engine = HMMEngine(config=HMM_CONFIG)
        assert engine.get_transition_matrix() is None

    def test_predict_raises_before_fit(self) -> None:
        engine   = HMMEngine(config=HMM_CONFIG)
        features = _make_features(n=700)
        with pytest.raises(RuntimeError, match="fit\\(\\)"):
            engine.predict_regime_filtered(features)


# ──────────────────────────────────────────────────────────────────────────────
# TRAINING E BIC SELECTION
# ──────────────────────────────────────────────────────────────────────────────

class TestFit:
    def test_fit_sets_model(self, trained_engine) -> None:
        engine, _ = trained_engine
        assert engine.is_fitted()
        assert engine.model is not None

    def test_n_states_within_candidates(self, trained_engine) -> None:
        engine, _ = trained_engine
        assert engine.n_states in HMM_CONFIG["n_candidates"]

    def test_regime_info_populated(self, trained_engine) -> None:
        engine, _ = trained_engine
        assert len(engine._regime_info) == engine.n_states

    def test_regime_info_contains_all_states(self, trained_engine) -> None:
        engine, _ = trained_engine
        expected_ids = set(range(engine.n_states))
        assert set(engine._regime_info.keys()) == expected_ids

    def test_labels_match_n_states(self, trained_engine) -> None:
        engine, _ = trained_engine
        labels = [info.regime_name for info in engine._regime_info.values()]
        valid  = REGIME_LABELS[engine.n_states]
        for label in labels:
            assert label in valid

    def test_fit_fails_with_insufficient_data(self) -> None:
        engine   = HMMEngine(config=HMM_CONFIG)
        features = _make_features(n=400)  # sotto MIN_TRAIN_BARS (504)
        with pytest.raises(ValueError, match="504"):
            engine.fit(features)

    def test_bic_is_finite(self, trained_engine) -> None:
        engine, _ = trained_engine
        assert np.isfinite(engine._best_bic)

    def test_transition_matrix_rows_sum_to_one(self, trained_engine) -> None:
        engine, _ = trained_engine
        transmat = engine.get_transition_matrix()
        assert transmat.shape == (engine.n_states, engine.n_states)
        np.testing.assert_allclose(transmat.sum(axis=1), 1.0, atol=1e-6)


# ──────────────────────────────────────────────────────────────────────────────
# PREDIZIONE BATCH
# ──────────────────────────────────────────────────────────────────────────────

class TestPredictFiltered:
    def test_output_length_matches_input(self, trained_engine) -> None:
        engine, features = trained_engine
        states = engine.predict_regime_filtered(features)
        assert len(states) == len(features)

    def test_states_within_valid_range(self, trained_engine) -> None:
        engine, features = trained_engine
        states = engine.predict_regime_filtered(features)
        assert states.min() >= 0
        assert states.max() < engine.n_states

    def test_proba_rows_sum_to_one(self, trained_engine) -> None:
        engine, features = trained_engine
        proba = engine.predict_regime_proba(features)
        assert proba.shape == (len(features), engine.n_states)
        np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-6)

    def test_proba_non_negative(self, trained_engine) -> None:
        engine, features = trained_engine
        proba = engine.predict_regime_proba(features)
        assert (proba >= 0).all()


# ──────────────────────────────────────────────────────────────────────────────
# STABILITY FILTER
# ──────────────────────────────────────────────────────────────────────────────

class TestStabilityFilter:
    def test_first_regime_is_confirmed(self, trained_engine) -> None:
        engine, features = trained_engine
        # Resetta il filtro per test pulito
        engine._reset_filter()
        state = engine.predict_current_regime(features.iloc[:600])
        assert state.is_confirmed

    def test_regime_state_has_valid_probability(self, trained_engine) -> None:
        engine, features = trained_engine
        engine._reset_filter()
        state = engine.predict_current_regime(features)
        assert 0.0 <= state.probability <= 1.0

    def test_state_probabilities_sum_to_one(self, trained_engine) -> None:
        engine, features = trained_engine
        engine._reset_filter()
        state = engine.predict_current_regime(features)
        total = sum(state.state_probabilities)
        assert abs(total - 1.0) < 1e-6

    def test_size_multiplier_is_one_when_confirmed(self, trained_engine) -> None:
        engine, features = trained_engine
        engine._reset_filter()
        state = engine.predict_current_regime(features)
        if state.is_confirmed:
            assert state.size_multiplier == 1.0

    def test_size_multiplier_is_075_when_unconfirmed(self, trained_engine) -> None:
        """Forza una transizione non confermata e verifica il moltiplicatore."""
        engine, features = trained_engine
        engine._reset_filter()

        # Inizializza con stato 0 confermato
        engine._confirmed_state          = 0
        engine._consecutive_in_confirmed = 10

        # Simula un raw_state diverso che non raggiunge stability_bars
        log_alpha = np.zeros(engine.n_states)
        log_alpha[1] = 5.0  # stato 1 dominante

        engine._pending_state = 1
        engine._pending_bars  = 1  # solo 1 barra → sotto stability_bars=3
        engine._raw_state_history.append(1)

        state = engine._apply_stability_filter(
            raw_state=1, log_alpha=log_alpha, timestamp=None
        )

        assert not state.is_confirmed
        assert state.size_multiplier == pytest.approx(0.75)
        assert state.state_id == 0  # riporta regime vecchio

    def test_confirm_after_stability_bars(self, trained_engine) -> None:
        """Dopo stability_bars barre consecutive il cambio viene confermato."""
        engine, features = trained_engine
        engine._reset_filter()
        engine._confirmed_state          = 0
        engine._consecutive_in_confirmed = 10

        log_alpha = np.zeros(engine.n_states)
        log_alpha[1] = 5.0

        for i in range(HMM_CONFIG["stability_bars"]):
            engine._raw_state_history.append(1)
            state = engine._apply_stability_filter(
                raw_state=1, log_alpha=log_alpha, timestamp=None
            )

        assert state.is_confirmed
        assert state.state_id == 1
        assert engine._confirmed_state == 1


# ──────────────────────────────────────────────────────────────────────────────
# FLICKER
# ──────────────────────────────────────────────────────────────────────────────

class TestFlicker:
    def test_no_flicker_with_stable_regime(self, trained_engine) -> None:
        engine, _ = trained_engine
        engine._reset_filter()
        for _ in range(15):
            engine._raw_state_history.append(0)
        assert engine.get_regime_flicker_rate() == 0
        assert not engine.is_flickering()

    def test_flicker_detected_above_threshold(self, trained_engine) -> None:
        engine, _ = trained_engine
        engine._reset_filter()
        # Alterna stati 0/1 → molti cambi
        for i in range(20):
            engine._raw_state_history.append(i % 2)
        rate = engine.get_regime_flicker_rate()
        assert rate > HMM_CONFIG["flicker_threshold"]
        assert engine.is_flickering()


# ──────────────────────────────────────────────────────────────────────────────
# PERSISTENZA
# ──────────────────────────────────────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load(self, trained_engine) -> None:
        engine, features = trained_engine

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.pkl"
            engine.save(path)
            assert path.exists()

            loaded = HMMEngine.load(path)

        assert loaded.n_states == engine.n_states
        assert loaded.is_fitted()

        # Le predizioni devono essere identiche
        states_orig   = engine.predict_regime_filtered(features)
        states_loaded = loaded.predict_regime_filtered(features)
        np.testing.assert_array_equal(states_orig, states_loaded)

    def test_saved_payload_contains_metadata(self, trained_engine) -> None:
        engine, _ = trained_engine

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.pkl"
            engine.save(path)
            with open(path, "rb") as f:
                payload = pickle.load(f)

        required_keys = {
            "model", "n_regimes", "bic", "training_date",
            "labels", "regime_info", "feature_columns", "config"
        }
        assert required_keys.issubset(set(payload.keys()))
        assert payload["feature_columns"] == FEATURE_COLUMNS
