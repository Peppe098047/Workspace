"""
Test per le strategie di regime e StrategyOrchestrator.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from core.hmm_engine import RegimeInfo, RegimeState
from core.regime_strategies import (
    Signal,
    AllocationTarget,
    LowVolBullStrategy,
    MidVolCautiousStrategy,
    HighVolDefensiveStrategy,
    StrategyOrchestrator,
    RegimeStrategy,
    LABEL_TO_STRATEGY,
    _apply_uncertainty,
    _atr,
    _ema,
    _MIN_BARS,
)


# ──────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ──────────────────────────────────────────────────────────────────────────────

STRATEGY_CONFIG = {
    "min_confidence":    0.55,
    "rebalance_threshold": 0.10,
}

SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "AMZN"]


def _make_bars(n: int = 120, trend: str = "up", seed: int = 0) -> pd.DataFrame:
    """Genera OHLCV sintetici con trend controllato."""
    rng   = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n, freq="B")

    drift = 0.001 if trend == "up" else -0.001
    log_ret = rng.normal(drift, 0.01, n)
    close   = 100.0 * np.exp(np.cumsum(log_ret))

    return pd.DataFrame({
        "open":   close * (1 + rng.normal(0, 0.002, n)),
        "high":   close * (1 + np.abs(rng.normal(0, 0.005, n))),
        "low":    close * (1 - np.abs(rng.normal(0, 0.005, n))),
        "close":  close,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    }, index=dates)


def _make_regime_state(
    state_id: int = 0,
    label: str = "NEUTRAL",
    probability: float = 0.80,
    is_confirmed: bool = True,
    n_states: int = 3,
) -> RegimeState:
    probs = [0.0] * n_states
    probs[state_id] = probability
    remaining = (1.0 - probability) / max(n_states - 1, 1)
    for i in range(n_states):
        if i != state_id:
            probs[i] = remaining
    return RegimeState(
        label=label,
        state_id=state_id,
        probability=probability,
        state_probabilities=probs,
        timestamp=datetime.now(),
        is_confirmed=is_confirmed,
        consecutive_bars=10,
        size_multiplier=1.0 if is_confirmed else 0.75,
    )


def _make_regime_infos(
    n: int = 3,
    vols: list[float] | None = None,
) -> dict[int, RegimeInfo]:
    """Crea RegimeInfo con volatilità crescente per testare il vol-rank mapping."""
    if vols is None:
        vols = [0.01 * (i + 1) for i in range(n)]

    labels_map = {3: ["BEAR", "NEUTRAL", "BULL"], 4: ["CRASH", "BEAR", "BULL", "EUPHORIA"]}
    labels = labels_map.get(n, [f"REGIME_{i}" for i in range(n)])
    from core.hmm_engine import _LABEL_DEFAULTS

    infos = {}
    for i in range(n):
        label = labels[i] if i < len(labels) else f"REGIME_{i}"
        strat, max_lev, max_pos, min_conf = _LABEL_DEFAULTS.get(
            label, ("balanced", 1.0, 0.15, 0.55)
        )
        infos[i] = RegimeInfo(
            regime_id=i,
            regime_name=label,
            expected_return=float(i) * 0.0005,
            expected_volatility=vols[i],
            recommended_strategy_type=strat,
            max_leverage_allowed=max_lev,
            max_position_size_pct=max_pos,
            min_confidence_to_act=min_conf,
        )
    return infos


# ──────────────────────────────────────────────────────────────────────────────
# TEST INDICATORI TECNICI
# ──────────────────────────────────────────────────────────────────────────────

class TestTechnicalIndicators:
    def test_atr_positive(self) -> None:
        bars = _make_bars(n=100)
        assert _atr(bars) > 0.0

    def test_atr_increases_with_volatility(self) -> None:
        bars_low  = _make_bars(n=100, seed=0)
        # Simula alta volatilità: H-L range amplificato
        bars_high = bars_low.copy()
        bars_high["high"] = bars_low["high"] * 1.1
        bars_high["low"]  = bars_low["low"]  * 0.9
        assert _atr(bars_high) > _atr(bars_low)

    def test_ema_within_price_range(self) -> None:
        bars  = _make_bars(n=120)
        close = bars["close"]
        ema   = _ema(close, period=50)
        assert close.min() * 0.8 < ema < close.max() * 1.2


# ──────────────────────────────────────────────────────────────────────────────
# TEST LOW VOL BULL STRATEGY
# ──────────────────────────────────────────────────────────────────────────────

class TestLowVolBullStrategy:
    @pytest.fixture
    def strategy(self) -> LowVolBullStrategy:
        return LowVolBullStrategy(STRATEGY_CONFIG)

    @pytest.fixture
    def regime_state(self) -> RegimeState:
        return _make_regime_state(label="BULL", probability=0.85)

    def test_direction_always_long(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig is not None
        assert sig.direction == "LONG"

    def test_allocation_is_95pct(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.position_size_pct == pytest.approx(0.95)

    def test_leverage_is_125(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.leverage == pytest.approx(1.25)

    def test_stop_below_price(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.stop_loss < sig.entry_price

    def test_stop_is_max_of_two_anchors(self, strategy, regime_state) -> None:
        """max() significa lo stop più stretto (più vicino al prezzo)."""
        bars  = _make_bars(n=120)
        sig   = strategy.generate_signal("SPY", bars, regime_state)
        price = sig.entry_price
        atr   = sig.metadata["atr"]
        ema50 = sig.metadata["ema50"]

        expected = max(price - 3 * atr, ema50 - 0.5 * atr)
        assert sig.stop_loss == pytest.approx(expected, rel=1e-6)

    def test_returns_none_with_insufficient_bars(self, strategy, regime_state) -> None:
        bars = _make_bars(n=_MIN_BARS - 1)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig is None

    def test_strategy_name(self, strategy) -> None:
        assert strategy.name == "LowVolBullStrategy"

    def test_signal_has_regime_info(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.regime_id    == regime_state.state_id
        assert sig.regime_name  == regime_state.label
        assert sig.regime_probability == pytest.approx(regime_state.probability)


# ──────────────────────────────────────────────────────────────────────────────
# TEST MID VOL CAUTIOUS STRATEGY
# ──────────────────────────────────────────────────────────────────────────────

class TestMidVolCautiousStrategy:
    @pytest.fixture
    def strategy(self) -> MidVolCautiousStrategy:
        return MidVolCautiousStrategy(STRATEGY_CONFIG)

    @pytest.fixture
    def regime_state(self) -> RegimeState:
        return _make_regime_state(label="NEUTRAL", probability=0.75)

    def test_direction_always_long(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120, trend="up")
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.direction == "LONG"

    def test_95pct_when_trend_intact(self, strategy, regime_state) -> None:
        """Close sopra EMA50 → 95% allocation."""
        bars = _make_bars(n=120, trend="up")
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        # Verifica che has_trend sia True e allocation sia 95%
        if sig.metadata["has_trend"]:
            assert sig.position_size_pct == pytest.approx(0.95)

    def test_60pct_when_trend_broken(self, strategy, regime_state) -> None:
        """Close sotto EMA50 → 60% allocation."""
        bars = _make_bars(n=120, trend="down")
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        if not sig.metadata["has_trend"]:
            assert sig.position_size_pct == pytest.approx(0.60)

    def test_leverage_always_1x(self, strategy, regime_state) -> None:
        for trend in ["up", "down"]:
            bars = _make_bars(n=120, trend=trend)
            sig  = strategy.generate_signal("SPY", bars, regime_state)
            assert sig.leverage == pytest.approx(1.0)

    def test_stop_is_ema_minus_half_atr(self, strategy, regime_state) -> None:
        bars  = _make_bars(n=120)
        sig   = strategy.generate_signal("SPY", bars, regime_state)
        atr   = sig.metadata["atr"]
        ema50 = sig.metadata["ema50"]
        assert sig.stop_loss == pytest.approx(ema50 - 0.5 * atr, rel=1e-6)

    def test_stop_below_ema(self, strategy, regime_state) -> None:
        bars  = _make_bars(n=120)
        sig   = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.stop_loss < sig.metadata["ema50"]


# ──────────────────────────────────────────────────────────────────────────────
# TEST HIGH VOL DEFENSIVE STRATEGY
# ──────────────────────────────────────────────────────────────────────────────

class TestHighVolDefensiveStrategy:
    @pytest.fixture
    def strategy(self) -> HighVolDefensiveStrategy:
        return HighVolDefensiveStrategy(STRATEGY_CONFIG)

    @pytest.fixture
    def regime_state(self) -> RegimeState:
        return _make_regime_state(label="CRASH", probability=0.70)

    def test_direction_is_long_not_short(self, strategy, regime_state) -> None:
        """Verifica il principio fondamentale: SEMPRE LONG, MAI SHORT."""
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.direction == "LONG"
        assert sig.direction != "SHORT"
        assert sig.direction != "FLAT"

    def test_allocation_is_60pct(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.position_size_pct == pytest.approx(0.60)

    def test_leverage_is_1x(self, strategy, regime_state) -> None:
        bars = _make_bars(n=120)
        sig  = strategy.generate_signal("SPY", bars, regime_state)
        assert sig.leverage == pytest.approx(1.0)

    def test_stop_is_ema_minus_1atr(self, strategy, regime_state) -> None:
        """Stop più ampio rispetto a MidVol per tollerare la volatilità."""
        bars  = _make_bars(n=120)
        sig   = strategy.generate_signal("SPY", bars, regime_state)
        atr   = sig.metadata["atr"]
        ema50 = sig.metadata["ema50"]
        assert sig.stop_loss == pytest.approx(ema50 - 1.0 * atr, rel=1e-6)

    def test_wider_stop_than_mid_vol(self) -> None:
        """HighVol deve avere stop più basso (più ampio) di MidVol."""
        bars = _make_bars(n=120)
        rs   = _make_regime_state()

        high_sig = HighVolDefensiveStrategy(STRATEGY_CONFIG).generate_signal("SPY", bars, rs)
        mid_sig  = MidVolCautiousStrategy(STRATEGY_CONFIG).generate_signal("SPY", bars, rs)

        assert high_sig.stop_loss < mid_sig.stop_loss


# ──────────────────────────────────────────────────────────────────────────────
# TEST UNCERTAINTY MODE
# ──────────────────────────────────────────────────────────────────────────────

class TestUncertaintyMode:
    def test_halves_position_size(self) -> None:
        bars = _make_bars(n=120)
        rs   = _make_regime_state(probability=0.85)
        sig  = LowVolBullStrategy(STRATEGY_CONFIG).generate_signal("SPY", bars, rs)
        original_size = sig.position_size_pct

        uncertain_sig = _apply_uncertainty(sig)
        assert uncertain_sig.position_size_pct == pytest.approx(original_size / 2.0)

    def test_forces_leverage_to_1x(self) -> None:
        bars = _make_bars(n=120)
        rs   = _make_regime_state(probability=0.85)
        sig  = LowVolBullStrategy(STRATEGY_CONFIG).generate_signal("SPY", bars, rs)
        assert sig.leverage > 1.0  # originale è 1.25

        uncertain_sig = _apply_uncertainty(sig)
        assert uncertain_sig.leverage == pytest.approx(1.0)

    def test_appends_uncertainty_to_reasoning(self) -> None:
        bars = _make_bars(n=120)
        rs   = _make_regime_state(probability=0.85)
        sig  = LowVolBullStrategy(STRATEGY_CONFIG).generate_signal("SPY", bars, rs)

        uncertain_sig = _apply_uncertainty(sig)
        assert "[UNCERTAINTY — size halved]" in uncertain_sig.reasoning

    def test_original_signal_unchanged(self) -> None:
        """_apply_uncertainty deve usare replace() — non mutare l'originale."""
        bars = _make_bars(n=120)
        rs   = _make_regime_state(probability=0.85)
        sig  = LowVolBullStrategy(STRATEGY_CONFIG).generate_signal("SPY", bars, rs)
        original_size = sig.position_size_pct

        _apply_uncertainty(sig)  # ritorna una copia
        assert sig.position_size_pct == pytest.approx(original_size)


# ──────────────────────────────────────────────────────────────────────────────
# TEST STRATEGY ORCHESTRATOR — VOL RANK MAPPING
# ──────────────────────────────────────────────────────────────────────────────

class TestStrategyOrchestratorVolRank:
    """
    Il mapping vol → strategia deve dipendere da expected_volatility,
    NON dalle etichette HMM.
    """

    def test_lowest_vol_gets_low_vol_strategy(self) -> None:
        # 3 regimi: il regime con vol più bassa → LowVolBull
        infos = _make_regime_infos(n=3, vols=[0.01, 0.02, 0.03])
        orc   = StrategyOrchestrator(STRATEGY_CONFIG, infos)
        # regime_id=0 ha vol=0.01 → posizione 0.0 → LowVol
        assert isinstance(orc.get_strategy(0), LowVolBullStrategy)

    def test_highest_vol_gets_high_vol_strategy(self) -> None:
        infos = _make_regime_infos(n=3, vols=[0.01, 0.02, 0.03])
        orc   = StrategyOrchestrator(STRATEGY_CONFIG, infos)
        # regime_id=2 ha vol=0.03 → posizione 1.0 → HighVol
        assert isinstance(orc.get_strategy(2), HighVolDefensiveStrategy)

    def test_middle_vol_gets_mid_vol_strategy(self) -> None:
        infos = _make_regime_infos(n=3, vols=[0.01, 0.02, 0.03])
        orc   = StrategyOrchestrator(STRATEGY_CONFIG, infos)
        # regime_id=1 ha vol=0.02 → posizione 0.5 → MidVol
        assert isinstance(orc.get_strategy(1), MidVolCautiousStrategy)

    def test_labels_are_ignored(self) -> None:
        """
        Un regime etichettato "BULL" ma con alta volatilità deve ricevere
        HighVolDefensiveStrategy, non LowVolBullStrategy.
        """
        # Crea un regime con label BULL ma volatilità alta
        from core.hmm_engine import _LABEL_DEFAULTS
        infos = {
            0: RegimeInfo(0, "BEAR",    0.0001, 0.01, "defensive",  0.75, 0.10, 0.65),
            1: RegimeInfo(1, "NEUTRAL", 0.0003, 0.02, "balanced",   1.00, 0.15, 0.55),
            2: RegimeInfo(2, "BULL",    0.0008, 0.05, "growth",     1.15, 0.20, 0.55),
        }
        orc = StrategyOrchestrator(STRATEGY_CONFIG, infos)
        # "BULL" con vol=0.05 (più alta) → HighVolDefensive, NON LowVolBull
        assert isinstance(orc.get_strategy(2), HighVolDefensiveStrategy)

    def test_vol_rank_range(self) -> None:
        infos = _make_regime_infos(n=5, vols=[0.01, 0.02, 0.03, 0.04, 0.05])
        orc   = StrategyOrchestrator(STRATEGY_CONFIG, infos)
        for rid in range(5):
            rank = orc.get_vol_rank(rid)
            assert 0.0 <= rank <= 1.0

    def test_5_regimes_mapping(self) -> None:
        """Con 5 regimi: rank=[0, .25, .5, .75, 1.0] → [Low, Low, Mid, High, High]."""
        infos = _make_regime_infos(n=5, vols=[0.01, 0.02, 0.03, 0.04, 0.05])
        orc   = StrategyOrchestrator(STRATEGY_CONFIG, infos)

        assert isinstance(orc.get_strategy(0), LowVolBullStrategy)        # 0.00 ≤ 0.33 → Low
        assert isinstance(orc.get_strategy(1), LowVolBullStrategy)        # 0.25 ≤ 0.33 → Low
        assert isinstance(orc.get_strategy(2), MidVolCautiousStrategy)    # 0.50 → Mid
        assert isinstance(orc.get_strategy(3), HighVolDefensiveStrategy)  # 0.75 ≥ 0.67 → High
        assert isinstance(orc.get_strategy(4), HighVolDefensiveStrategy)  # 1.00 ≥ 0.67 → High

    def test_update_regime_infos_rebuilds_map(self) -> None:
        infos = _make_regime_infos(n=3, vols=[0.01, 0.02, 0.03])
        orc   = StrategyOrchestrator(STRATEGY_CONFIG, infos)

        # Inizialmente regime_id=0 → LowVol
        assert isinstance(orc.get_strategy(0), LowVolBullStrategy)

        # Dopo update con nuovi infos (stesso n, volatilità invertite)
        new_infos = _make_regime_infos(n=3, vols=[0.05, 0.03, 0.01])
        orc.update_regime_infos(new_infos)

        # Ora regime_id=2 ha la vol più bassa → LowVol
        # regime_id=0 ha la vol più alta → HighVol
        assert isinstance(orc.get_strategy(2), LowVolBullStrategy)
        assert isinstance(orc.get_strategy(0), HighVolDefensiveStrategy)


# ──────────────────────────────────────────────────────────────────────────────
# TEST ORCHESTRATOR — GENERATE SIGNALS
# ──────────────────────────────────────────────────────────────────────────────

class TestOrchestratorGenerateSignals:
    @pytest.fixture
    def orchestrator(self) -> StrategyOrchestrator:
        infos = _make_regime_infos(n=3, vols=[0.01, 0.02, 0.03])
        return StrategyOrchestrator(STRATEGY_CONFIG, infos)

    @pytest.fixture
    def bars(self) -> dict[str, pd.DataFrame]:
        return {sym: _make_bars(n=120, seed=i) for i, sym in enumerate(SYMBOLS)}

    def test_generates_signal_per_symbol(self, orchestrator, bars) -> None:
        rs      = _make_regime_state(state_id=0)
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs)
        assert len(signals) == len(SYMBOLS)

    def test_all_signals_are_long(self, orchestrator, bars) -> None:
        for state_id in range(3):
            rs      = _make_regime_state(state_id=state_id)
            signals = orchestrator.generate_signals(SYMBOLS, bars, rs)
            for sig in signals:
                assert sig.direction == "LONG", (
                    f"Segnale SHORT rilevato per stato {state_id} — "
                    f"VIOLAZIONE del principio ALWAYS LONG"
                )

    def test_uncertainty_triggered_by_low_confidence(self, orchestrator, bars) -> None:
        rs = _make_regime_state(state_id=0, probability=0.40)  # < min_confidence=0.55
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs)
        for sig in signals:
            assert "[UNCERTAINTY" in sig.reasoning
            assert sig.leverage == pytest.approx(1.0)

    def test_uncertainty_triggered_by_flickering(self, orchestrator, bars) -> None:
        rs = _make_regime_state(state_id=0, probability=0.80)
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs, is_flickering=True)
        for sig in signals:
            assert "[UNCERTAINTY" in sig.reasoning

    def test_uncertainty_triggered_by_unconfirmed(self, orchestrator, bars) -> None:
        rs = _make_regime_state(state_id=0, probability=0.80, is_confirmed=False)
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs)
        for sig in signals:
            assert "[UNCERTAINTY" in sig.reasoning

    def test_no_uncertainty_when_all_ok(self, orchestrator, bars) -> None:
        rs = _make_regime_state(state_id=0, probability=0.80, is_confirmed=True)
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs, is_flickering=False)
        for sig in signals:
            assert "[UNCERTAINTY" not in sig.reasoning

    def test_skips_symbols_without_bars(self, orchestrator) -> None:
        rs   = _make_regime_state(state_id=0)
        bars = {"SPY": _make_bars(n=120)}
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs)
        assert len(signals) == 1
        assert signals[0].symbol == "SPY"

    def test_skips_symbols_with_insufficient_bars(self, orchestrator) -> None:
        rs   = _make_regime_state(state_id=0)
        bars = {sym: _make_bars(n=_MIN_BARS - 1) for sym in SYMBOLS}
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs)
        assert len(signals) == 0

    def test_returns_empty_for_unknown_regime_id(self, orchestrator, bars) -> None:
        # orchestrator ha 3 stati (0,1,2) — state_id=3 è sconosciuto
        rs      = _make_regime_state(state_id=3, n_states=4)
        signals = orchestrator.generate_signals(SYMBOLS, bars, rs)
        assert signals == []


# ──────────────────────────────────────────────────────────────────────────────
# TEST NEEDS_REBALANCE
# ──────────────────────────────────────────────────────────────────────────────

class TestNeedsRebalance:
    @pytest.fixture
    def orchestrator(self) -> StrategyOrchestrator:
        return StrategyOrchestrator(
            STRATEGY_CONFIG, _make_regime_infos(n=3)
        )

    def test_no_rebalance_within_threshold(self, orchestrator) -> None:
        current = {"SPY": 0.20, "QQQ": 0.20}
        target  = {"SPY": 0.25, "QQQ": 0.22}  # scarto max = 0.05 < 0.10
        assert not orchestrator.needs_rebalance(current, target)

    def test_rebalance_above_threshold(self, orchestrator) -> None:
        current = {"SPY": 0.05, "QQQ": 0.20}
        target  = {"SPY": 0.20, "QQQ": 0.20}  # scarto SPY = 0.15 > 0.10
        assert orchestrator.needs_rebalance(current, target)

    def test_rebalance_new_symbol_in_target(self, orchestrator) -> None:
        current = {"SPY": 0.20}
        target  = {"SPY": 0.20, "QQQ": 0.20}  # QQQ assente → scarto 0.20 > 0.10
        assert orchestrator.needs_rebalance(current, target)

    def test_no_rebalance_identical_weights(self, orchestrator) -> None:
        weights = {"SPY": 0.20, "QQQ": 0.20, "AAPL": 0.20}
        assert not orchestrator.needs_rebalance(weights, weights)


# ──────────────────────────────────────────────────────────────────────────────
# TEST REGIME STRATEGY WRAPPER (backward compat)
# ──────────────────────────────────────────────────────────────────────────────

class TestRegimeStrategyWrapper:
    @pytest.fixture
    def strategy(self) -> RegimeStrategy:
        infos = _make_regime_infos(n=3, vols=[0.01, 0.02, 0.03])
        return RegimeStrategy(STRATEGY_CONFIG, SYMBOLS, regime_infos=infos)

    @pytest.fixture
    def bars(self) -> dict[str, pd.DataFrame]:
        return {sym: _make_bars(n=120) for sym in SYMBOLS}

    def test_compute_target_returns_allocation(self, strategy, bars) -> None:
        rs     = _make_regime_state(state_id=0, probability=0.80)
        target = strategy.compute_target(rs, bars)
        assert isinstance(target, AllocationTarget)
        assert len(target.weights) > 0

    def test_weights_are_non_negative(self, strategy, bars) -> None:
        rs     = _make_regime_state(state_id=0)
        target = strategy.compute_target(rs, bars)
        for w in target.weights.values():
            assert w >= 0.0

    def test_gross_exposure_consistent(self, strategy, bars) -> None:
        rs     = _make_regime_state(state_id=0)
        target = strategy.compute_target(rs, bars)
        assert target.gross_exposure == pytest.approx(sum(target.weights.values()))

    def test_raises_without_regime_infos(self) -> None:
        strategy = RegimeStrategy(STRATEGY_CONFIG, SYMBOLS)  # no regime_infos
        rs   = _make_regime_state(state_id=0)
        bars = {sym: _make_bars(n=120) for sym in SYMBOLS}
        with pytest.raises(RuntimeError, match="bind_regime_infos"):
            strategy.compute_target(rs, bars)

    def test_bind_regime_infos(self) -> None:
        strategy = RegimeStrategy(STRATEGY_CONFIG, SYMBOLS)
        infos    = _make_regime_infos(n=3, vols=[0.01, 0.02, 0.03])
        strategy.bind_regime_infos(infos)
        rs   = _make_regime_state(state_id=0)
        bars = {sym: _make_bars(n=120) for sym in SYMBOLS}
        target = strategy.compute_target(rs, bars)
        assert isinstance(target, AllocationTarget)

    def test_needs_rebalance_delegates_correctly(self, strategy) -> None:
        current = {"SPY": 0.05}
        target  = {"SPY": 0.20}
        assert strategy.needs_rebalance(current, target)

        current = target = {"SPY": 0.20}
        assert not strategy.needs_rebalance(current, target)


# ──────────────────────────────────────────────────────────────────────────────
# TEST LABEL_TO_STRATEGY
# ──────────────────────────────────────────────────────────────────────────────

class TestLabelToStrategy:
    def test_all_labels_covered(self) -> None:
        from core.hmm_engine import REGIME_LABELS
        all_labels = set()
        for labels in REGIME_LABELS.values():
            all_labels.update(labels)
        for label in all_labels:
            assert label in LABEL_TO_STRATEGY, f"Etichetta '{label}' mancante in LABEL_TO_STRATEGY"

    def test_crash_maps_to_high_vol(self) -> None:
        assert LABEL_TO_STRATEGY["CRASH"] is HighVolDefensiveStrategy

    def test_bear_maps_to_high_vol(self) -> None:
        assert LABEL_TO_STRATEGY["BEAR"] is HighVolDefensiveStrategy

    def test_neutral_maps_to_mid_vol(self) -> None:
        assert LABEL_TO_STRATEGY["NEUTRAL"] is MidVolCautiousStrategy

    def test_bull_maps_to_low_vol(self) -> None:
        assert LABEL_TO_STRATEGY["BULL"] is LowVolBullStrategy

    def test_euphoria_maps_to_low_vol(self) -> None:
        assert LABEL_TO_STRATEGY["EUPHORIA"] is LowVolBullStrategy
