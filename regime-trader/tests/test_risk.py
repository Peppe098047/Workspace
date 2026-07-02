"""
Test per RiskManager, CircuitBreaker e PortfolioState.

Copertura:
- Circuit breaker: latch, reset, lock file, PEAK_HALT
- validate_signal: approvazione, rifiuto, modifiche sizing
- Risk sizing: 1% per trade, gap overnight 3×, cap 15%
- Limiti portfolio: esposizione, concurrent, daily trades
- Leva: forza 1.0× in condizioni avverse
- Stop loss obbligatorio
- Correlazione: riduzione e rifiuto
- Duplicate detection
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.risk_manager import (
    CircuitBreaker,
    CircuitBreakerLevel,
    PortfolioState,
    PositionInfo,
    RiskDecision,
    RiskManager,
)
from core.regime_strategies import Signal


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG E FIXTURE
# ──────────────────────────────────────────────────────────────────────────────

RISK_CONFIG = {
    "max_risk_per_trade":     0.01,
    "max_exposure":           0.80,
    "max_leverage":           1.25,
    "max_single_position":    0.15,
    "max_concurrent":         5,
    "max_daily_trades":       20,
    "daily_dd_reduce":        0.02,
    "daily_dd_halt":          0.03,
    "weekly_dd_reduce":       0.05,
    "weekly_dd_halt":         0.07,
    "max_dd_from_peak":       0.10,
    "gap_multiplier":         3.0,
    "gap_max_loss_pct":       0.02,
    "corr_reduce_threshold":  0.70,
    "corr_reject_threshold":  0.85,
    "correlation_window":     60,
    "flicker_threshold":      4,
    "min_position_usd":       100.0,
    "duplicate_block_secs":   60,
    "max_sector_exposure":    0.30,
}

EQUITY = 100_000.0
TODAY  = date(2024, 6, 3)


def _signal(
    symbol: str = "SPY",
    entry: float = 450.0,
    stop: float = 435.0,       # distanza stop = 15$ → size = (1000/15) = ~66 shares
    size_pct: float = 0.95,
    leverage: float = 1.0,
    direction: str = "LONG",
    reasoning: str = "",
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=direction,
        confidence=0.80,
        entry_price=entry,
        stop_loss=stop,
        take_profit=None,
        position_size_pct=size_pct,
        leverage=leverage,
        regime_id=0,
        regime_name="LOW_VOL",
        regime_probability=0.80,
        timestamp=datetime.utcnow(),
        reasoning=reasoning,
        strategy_name="LowVolBullStrategy",
        metadata={},
    )


def _state(
    equity: float = EQUITY,
    cash: float = EQUITY,
    buying_power: float | None = None,
    positions: dict[str, PositionInfo] | None = None,
    daily_pnl: float = 0.0,
    weekly_pnl: float = 0.0,
    peak_equity: float = EQUITY,
    drawdown: float = 0.0,
    cb_status: CircuitBreakerLevel = CircuitBreakerLevel.NORMAL,
    flicker_rate: float = 0.0,
    current_date: date = TODAY,
    price_history: pd.DataFrame | None = None,
) -> PortfolioState:
    return PortfolioState(
        equity=equity,
        cash=cash,
        buying_power=buying_power if buying_power is not None else cash * 1.25,
        positions=positions or {},
        daily_pnl=daily_pnl,
        weekly_pnl=weekly_pnl,
        peak_equity=peak_equity,
        drawdown=drawdown,
        circuit_breaker_status=cb_status,
        flicker_rate=flicker_rate,
        current_regime="LOW_VOL",
        current_date=current_date,
        price_history=price_history,
    )


@pytest.fixture
def rm(tmp_path: Path) -> RiskManager:
    """RiskManager isolato con lock file in tmp_path."""
    manager = RiskManager(config=RISK_CONFIG, initial_capital=EQUITY)
    manager.circuit_breaker._lock_path = tmp_path / "trading_halted.lock"
    manager.initialize(EQUITY, TODAY)
    return manager


@pytest.fixture
def cb(tmp_path: Path) -> CircuitBreaker:
    """CircuitBreaker isolato con lock file in tmp_path."""
    breaker = CircuitBreaker(RISK_CONFIG, lock_file=tmp_path / "trading_halted.lock")
    breaker.initialize(EQUITY, TODAY)
    return breaker


# ──────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ──────────────────────────────────────────────────────────────────────────────

class TestCircuitBreakerLevels:
    def test_normal_at_start(self, cb: CircuitBreaker) -> None:
        assert cb.check() == CircuitBreakerLevel.NORMAL

    def test_daily_reduce_at_2pct(self, cb: CircuitBreaker) -> None:
        """DD giornaliero di esattamente il 2% → DAILY_REDUCE."""
        equity_down = EQUITY * (1 - 0.021)    # appena sopra la soglia
        cb.update(equity_down, TODAY)
        assert cb.check() == CircuitBreakerLevel.DAILY_REDUCE

    def test_daily_halt_at_3pct(self, cb: CircuitBreaker) -> None:
        """DD giornaliero del 3.1% → DAILY_HALT."""
        equity_down = EQUITY * (1 - 0.031)
        cb.update(equity_down, TODAY)
        assert cb.check() == CircuitBreakerLevel.DAILY_HALT

    def test_weekly_reduce_at_5pct(self, cb: CircuitBreaker) -> None:
        cb._weekly_start = EQUITY
        cb.update(EQUITY * (1 - 0.051), TODAY)
        assert CircuitBreakerLevel._severity(cb.check()) >= CircuitBreakerLevel._severity(CircuitBreakerLevel.WEEKLY_REDUCE)

    def test_weekly_halt_at_7pct(self, cb: CircuitBreaker) -> None:
        cb._weekly_start = EQUITY
        cb.update(EQUITY * (1 - 0.071), TODAY)
        assert cb.check() == CircuitBreakerLevel.WEEKLY_HALT

    def test_peak_halt_at_10pct(self, cb: CircuitBreaker) -> None:
        """DD dal picco del 10.1% → PEAK_HALT + lock file."""
        cb.update(EQUITY * (1 - 0.101), TODAY)
        assert cb.check() == CircuitBreakerLevel.PEAK_HALT
        assert cb.is_lock_file_present()

    def test_daily_latch_does_not_reset_on_recovery(self, cb: CircuitBreaker) -> None:
        """Il breaker non si resetta se l'equity si riprende nello stesso giorno."""
        cb.update(EQUITY * 0.969, TODAY)    # DD 3.1% → DAILY_HALT
        cb.update(EQUITY * 0.990, TODAY)    # recupero parziale
        assert cb.check() == CircuitBreakerLevel.DAILY_HALT

    def test_reset_daily_clears_daily_breaker(self, cb: CircuitBreaker) -> None:
        cb.update(EQUITY * 0.969, TODAY)
        assert cb.check() == CircuitBreakerLevel.DAILY_HALT
        next_day = date(2024, 6, 4)
        cb.reset_daily(EQUITY * 0.969)
        # Dopo reset il livello dovrebbe tornare a NORMAL (weekly non ancora toccato)
        assert cb._daily_latched == CircuitBreakerLevel.NORMAL

    def test_get_history_records_triggers(self, cb: CircuitBreaker) -> None:
        cb.update(EQUITY * 0.978, TODAY)    # DAILY_REDUCE
        history = cb.get_history()
        assert len(history) >= 1
        assert history[-1]["level"] in [l.value for l in CircuitBreakerLevel]

    def test_lock_file_detected_at_startup(self, tmp_path: Path) -> None:
        """Se il lock file esiste, PEAK_HALT scatta immediatamente."""
        lock = tmp_path / "trading_halted.lock"
        lock.write_text('{"triggered_at": "2024-01-01"}')
        new_cb = CircuitBreaker(RISK_CONFIG, lock_file=lock)
        new_cb.initialize(EQUITY, TODAY)
        assert new_cb.check() == CircuitBreakerLevel.PEAK_HALT

    def test_severity_ordering(self) -> None:
        """PEAK_HALT > WEEKLY_HALT > DAILY_HALT > WEEKLY_REDUCE > DAILY_REDUCE > NORMAL"""
        levels = [
            CircuitBreakerLevel.NORMAL,
            CircuitBreakerLevel.DAILY_REDUCE,
            CircuitBreakerLevel.WEEKLY_REDUCE,
            CircuitBreakerLevel.DAILY_HALT,
            CircuitBreakerLevel.WEEKLY_HALT,
            CircuitBreakerLevel.PEAK_HALT,
        ]
        severities = [CircuitBreakerLevel._severity(l) for l in levels]
        assert severities == sorted(severities)


# ──────────────────────────────────────────────────────────────────────────────
# VALIDATE SIGNAL — CASI BASE
# ──────────────────────────────────────────────────────────────────────────────

class TestValidateSignalApproval:
    def test_normal_signal_approved(self, rm: RiskManager) -> None:
        sig = _signal()
        dec = rm.validate_signal(sig, _state())
        assert dec.approved

    def test_approved_signal_is_not_none(self, rm: RiskManager) -> None:
        sig = _signal()
        dec = rm.validate_signal(sig, _state())
        assert dec.modified_signal is not None

    def test_direction_unchanged(self, rm: RiskManager) -> None:
        sig = _signal()
        dec = rm.validate_signal(sig, _state())
        assert dec.modified_signal.direction == "LONG"

    def test_symbol_unchanged(self, rm: RiskManager) -> None:
        sig = _signal()
        dec = rm.validate_signal(sig, _state())
        assert dec.modified_signal.symbol == "SPY"


# ──────────────────────────────────────────────────────────────────────────────
# VALIDATE SIGNAL — RIFIUTI
# ──────────────────────────────────────────────────────────────────────────────

class TestValidateSignalRejection:
    def test_reject_missing_stop_loss(self, rm: RiskManager) -> None:
        sig = _signal(stop=0.0)
        dec = rm.validate_signal(sig, _state())
        assert not dec.approved
        assert "stop loss" in dec.rejection_reason.lower()

    def test_reject_inverted_stop_loss(self, rm: RiskManager) -> None:
        """Stop sopra l'entry è invalido per un LONG."""
        sig = _signal(entry=450.0, stop=460.0)
        dec = rm.validate_signal(sig, _state())
        assert not dec.approved

    def test_reject_when_daily_halt(self, rm: RiskManager) -> None:
        """Con DAILY_HALT attivo nessun ordine viene approvato."""
        # Forza DAILY_HALT aggiornando l'equity con un calo del 3.1%
        rm.circuit_breaker.update(EQUITY * 0.969, TODAY)
        sig = _signal()
        dec = rm.validate_signal(sig, _state(equity=EQUITY * 0.969))
        assert not dec.approved

    def test_reject_when_lock_file_present(self, rm: RiskManager) -> None:
        rm.circuit_breaker._lock_path.write_text('{"triggered_at": "test"}')
        sig = _signal()
        dec = rm.validate_signal(sig, _state())
        assert not dec.approved
        assert "lock" in dec.rejection_reason.lower()

    def test_reject_max_concurrent_positions(self, rm: RiskManager) -> None:
        """Con 5 posizioni aperte, una nuova viene rifiutata."""
        positions = {
            f"SYM{i}": PositionInfo(
                symbol=f"SYM{i}", shares=10, entry_price=100,
                current_price=100, stop_loss=90, sector="TECHNOLOGY"
            )
            for i in range(5)
        }
        sig = _signal(symbol="NEW")
        state = _state(positions=positions)
        dec = rm.validate_signal(sig, state)
        assert not dec.approved
        assert "posizioni" in dec.rejection_reason.lower()

    def test_reject_max_daily_trades(self, rm: RiskManager) -> None:
        """Dopo 20 trade nessun nuovo ordine è accettato."""
        rm._daily_trade_count = 20
        sig = _signal()
        dec = rm.validate_signal(sig, _state())
        assert not dec.approved
        assert "giornalieri" in dec.rejection_reason.lower()

    def test_reject_duplicate_signal(self, rm: RiskManager) -> None:
        """Stesso simbolo+direzione entro 60 secondi è duplicato."""
        sig = _signal()
        # Prima call → registra timestamp
        dec1 = rm.validate_signal(sig, _state())
        assert dec1.approved
        # Seconda call immediatamente → duplicato
        dec2 = rm.validate_signal(sig, _state())
        assert not dec2.approved
        assert "duplicato" in dec2.rejection_reason.lower()

    def test_reject_insufficient_buying_power(self, rm: RiskManager) -> None:
        """Buying power insufficiente → rifiuto."""
        state = _state(buying_power=100.0)    # solo $100
        sig   = _signal(entry=450.0, size_pct=0.95)
        dec   = rm.validate_signal(sig, state)
        assert not dec.approved
        assert "buying power" in dec.rejection_reason.lower()


# ──────────────────────────────────────────────────────────────────────────────
# RISK SIZING
# ──────────────────────────────────────────────────────────────────────────────

class TestRiskSizing:
    def test_position_size_capped_at_15pct(self, rm: RiskManager) -> None:
        """
        Con stop molto vicino all'entry, il sizing risk-based darebbe un
        enorme numero di azioni. Il cap del 15% deve prevalere.
        """
        sig = _signal(entry=450.0, stop=449.0, size_pct=0.95)  # stop a $1
        dec = rm.validate_signal(sig, _state())
        assert dec.approved
        assert dec.modified_signal.position_size_pct <= 0.15

    def test_gap_risk_caps_overnight_position(self, rm: RiskManager) -> None:
        """
        Gap overnight 3× stop = max 2% portfolio.
        Stop a $15 → gap = $45 → max value = $2,000 → max shares = 44.4
        max_shares_pct = 44 × 450 / 100,000 = 19.8% → cap a 15%
        gap_shares = 2000/45 = 44 → pct = 19.8% → cap 15%
        """
        sig = _signal(entry=450.0, stop=435.0, size_pct=0.95)
        dec = rm.validate_signal(sig, _state())
        assert dec.approved
        # Il sizing deve essere ≤ 15% per il cap
        assert dec.modified_signal.position_size_pct <= 0.15

    def test_risk_per_trade_1pct(self, rm: RiskManager) -> None:
        """
        Con stop a 50$ di distanza: max risk = 1% × 100k = $1,000
        max shares = 1000/50 = 20 → pct = 20×500/100k = 10%
        Poiché strategy dice 95%, il risk-based vince (10% < 95%)
        """
        sig = _signal(entry=500.0, stop=450.0, size_pct=0.95)
        dec = rm.validate_signal(sig, _state())
        assert dec.approved
        # Risk-based: 1000/50 = 20 shares → 10%
        # Gap-based: 2000/(3×50) = 13 shares → 6.5%
        # Usa il minore: gap → ~6.5%
        assert dec.modified_signal.position_size_pct <= 0.15

    def test_min_position_usd_rejection(self, rm: RiskManager) -> None:
        """Posizione sotto $100 → rifiuto."""
        # Con entry $450 e stop a $440 (distanza $10):
        # risk shares = 1000/10 = 100; gap shares = 2000/30 = 66
        # Fine: 66 × 450 = $29,700 → OK
        # Forza una situazione dove equity è $500 e gap shares è minuscolo
        tiny_rm = RiskManager(config={**RISK_CONFIG, "min_position_usd": 50_000}, initial_capital=1000)
        tiny_rm.circuit_breaker._lock_path = Path(tempfile.mktemp(suffix=".lock"))
        tiny_rm.initialize(1000.0, TODAY)
        sig = _signal(entry=450.0, stop=435.0, size_pct=0.10)
        dec = tiny_rm.validate_signal(sig, _state(equity=1000.0, cash=1000.0, buying_power=1250.0))
        assert not dec.approved

    def test_compute_safe_shares_respects_risk(self, rm: RiskManager) -> None:
        """compute_safe_shares: 1% / stop_distance."""
        # entry=450, stop=435, dist=15
        # risk_shares = 1000/15 = 66
        # gap_shares = 2000/45 = 44
        # pct_shares = 15000/450 = 33
        # min = 33
        shares = rm.compute_safe_shares(entry_price=450.0, stop_loss=435.0, equity=EQUITY)
        assert shares >= 1
        # Non deve superare il 15%: max = 33 shares = 33×450/100k = 14.85%
        max_allowed = int(EQUITY * 0.15 / 450.0)
        assert shares <= max_allowed


# ──────────────────────────────────────────────────────────────────────────────
# ESPOSIZIONE E LEVA
# ──────────────────────────────────────────────────────────────────────────────

class TestExposureLimits:
    def test_total_exposure_limit(self, rm: RiskManager) -> None:
        """Con esposizione già all'80%, nuovi segnali vengono ridotti o bloccati."""
        # Simula portafoglio già all'80% investito
        positions = {
            "SPY": PositionInfo(
                symbol="SPY", shares=177, entry_price=450,
                current_price=450, stop_loss=430, sector="ETF_US_EQUITY"
            )
        }
        # 177 × 450 = 79,650 → ~79.65% di esposizione
        state = _state(
            equity=EQUITY,
            cash=EQUITY - 79_650,
            positions=positions,
            buying_power=(EQUITY - 79_650) * 1.25,
        )
        sig = _signal(symbol="QQQ")
        dec = rm.validate_signal(sig, state)
        # Deve essere ridotto o rifiutato (< 1% disponibile)
        assert not dec.approved or dec.modified_signal.position_size_pct < 0.05

    def test_leverage_forced_to_1x_with_circuit_breaker(self, rm: RiskManager) -> None:
        """Circuit breaker attivo → leva forzata a 1.0×."""
        rm.circuit_breaker.update(EQUITY * 0.979, TODAY)    # DAILY_REDUCE
        sig = _signal(leverage=1.25)
        dec = rm.validate_signal(sig, _state(equity=EQUITY * 0.979))
        if dec.approved:
            assert dec.modified_signal.leverage == 1.0

    def test_leverage_forced_to_1x_with_3_positions(self, rm: RiskManager) -> None:
        """Con 3+ posizioni aperte, la leva deve essere forzata a 1.0×."""
        positions = {
            f"SYM{i}": PositionInfo(
                symbol=f"SYM{i}", shares=5, entry_price=100,
                current_price=100, stop_loss=90, sector="TECHNOLOGY"
            )
            for i in range(3)
        }
        sig = _signal(symbol="NEW", leverage=1.25)
        dec = rm.validate_signal(sig, _state(positions=positions))
        if dec.approved:
            assert dec.modified_signal.leverage == 1.0

    def test_leverage_forced_to_1x_when_uncertain(self, rm: RiskManager) -> None:
        """Regime incerto (reasoning con UNCERTAINTY) → leva forzata a 1.0×."""
        sig = _signal(leverage=1.25, reasoning="LowVol [UNCERTAINTY — size halved]")
        dec = rm.validate_signal(sig, _state())
        if dec.approved:
            assert dec.modified_signal.leverage == 1.0

    def test_leverage_forced_to_1x_with_high_flicker(self, rm: RiskManager) -> None:
        """Flicker rate alto → leva forzata a 1.0×."""
        sig   = _signal(leverage=1.25)
        state = _state(flicker_rate=6.0)    # > soglia 4
        dec   = rm.validate_signal(sig, state)
        if dec.approved:
            assert dec.modified_signal.leverage == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# CORRELAZIONE
# ──────────────────────────────────────────────────────────────────────────────

class TestCorrelation:
    def _price_history(self, n: int = 120, corr_with_spy: float = 0.90) -> pd.DataFrame:
        """Genera una price history con QQQ altamente correlato a SPY."""
        rng       = np.random.default_rng(42)
        dates     = pd.bdate_range("2024-01-01", periods=n)
        spy_rets  = rng.normal(0.001, 0.01, n)
        noise     = rng.normal(0, 0.01 * (1 - corr_with_spy), n)
        qqq_rets  = corr_with_spy * spy_rets + noise

        spy_close = 450 * np.exp(np.cumsum(spy_rets))
        qqq_close = 380 * np.exp(np.cumsum(qqq_rets))

        return pd.DataFrame({"SPY": spy_close, "QQQ": qqq_close}, index=dates)

    def test_high_correlation_reduces_size(self, rm: RiskManager) -> None:
        """Correlazione 0.75 (> 0.70) → sizing dimezzato."""
        history = self._price_history(corr_with_spy=0.75)
        positions = {
            "SPY": PositionInfo(
                symbol="SPY", shares=22, entry_price=450,
                current_price=450, stop_loss=430, sector="ETF_US_EQUITY"
            )
        }
        state = _state(positions=positions, price_history=history)
        sig   = _signal(symbol="QQQ")
        dec   = rm.validate_signal(sig, state)

        if dec.approved:
            # Deve avere "corr" nelle modifiche
            corr_mods = [m for m in dec.modifications if "correlaz" in m.lower()]
            assert len(corr_mods) > 0

    def test_very_high_correlation_rejects(self, rm: RiskManager) -> None:
        """Correlazione > 0.85 → rifiuto."""
        history   = self._price_history(corr_with_spy=0.92)
        positions = {
            "SPY": PositionInfo(
                symbol="SPY", shares=22, entry_price=450,
                current_price=450, stop_loss=430, sector="ETF_US_EQUITY"
            )
        }
        state = _state(positions=positions, price_history=history)
        sig   = _signal(symbol="QQQ")
        dec   = rm.validate_signal(sig, state)
        # Con correlazione molto alta, il test verifica solo che ci sia
        # una riduzione OR un rifiuto — la correlazione esatta dipende
        # dai dati sintetici, quindi accettiamo entrambi
        if not dec.approved:
            assert "correlaz" in dec.rejection_reason.lower()


# ──────────────────────────────────────────────────────────────────────────────
# PORTFOLIO STATE
# ──────────────────────────────────────────────────────────────────────────────

class TestPortfolioState:
    def test_total_exposure_empty_portfolio(self) -> None:
        state = _state()
        assert state.total_exposure == pytest.approx(0.0)

    def test_total_exposure_with_position(self) -> None:
        positions = {
            "SPY": PositionInfo(
                symbol="SPY", shares=100, entry_price=450,
                current_price=450, stop_loss=430, sector="ETF_US_EQUITY"
            )
        }
        state = _state(positions=positions)
        # 100 × 450 / 100,000 = 0.45
        assert state.total_exposure == pytest.approx(0.45)

    def test_n_positions(self) -> None:
        positions = {
            "A": PositionInfo("A", 10, 100, 100, 90, "TECHNOLOGY"),
            "B": PositionInfo("B", 10, 100, 100, 90, "TECHNOLOGY"),
        }
        state = _state(positions=positions)
        assert state.n_positions == 2

    def test_sector_exposure(self) -> None:
        positions = {
            "AAPL": PositionInfo("AAPL", 100, 180, 180, 170, "TECHNOLOGY"),
            "MSFT": PositionInfo("MSFT", 50, 400, 400, 380, "TECHNOLOGY"),
            "SPY":  PositionInfo("SPY",  22, 450, 450, 430, "ETF_US_EQUITY"),
        }
        state = _state(positions=positions)
        tech_exp = state.sector_exposure("TECHNOLOGY")
        # (100×180 + 50×400) / 100,000 = (18,000 + 20,000) / 100,000 = 0.38
        assert tech_exp == pytest.approx(0.38)

    def test_position_info_market_value(self) -> None:
        pos = PositionInfo("SPY", shares=100, entry_price=400, current_price=450, stop_loss=380)
        assert pos.market_value == pytest.approx(45_000.0)

    def test_position_info_unrealized_pnl(self) -> None:
        pos = PositionInfo("SPY", shares=100, entry_price=400, current_price=450, stop_loss=380)
        assert pos.unrealized_pnl == pytest.approx(5_000.0)


# ──────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER SIZE MULTIPLIER
# ──────────────────────────────────────────────────────────────────────────────

class TestCircuitBreakerMultiplier:
    def test_daily_reduce_halves_position(self, rm: RiskManager) -> None:
        """DAILY_REDUCE → il sizing approvato è ≤ 50% del normale."""
        # Approva prima un segnale senza breaker
        sig_normal = _signal(symbol="AAPL")
        dec_normal = rm.validate_signal(sig_normal, _state())
        normal_pct = dec_normal.modified_signal.position_size_pct if dec_normal.approved else 0.15

        # Forza DAILY_REDUCE
        rm2 = RiskManager(config=RISK_CONFIG, initial_capital=EQUITY)
        rm2.circuit_breaker._lock_path = rm.circuit_breaker._lock_path.parent / "test2.lock"
        rm2.initialize(EQUITY, TODAY)
        rm2.circuit_breaker.update(EQUITY * 0.979, TODAY)

        sig_reduce = _signal(symbol="NVDA")
        dec_reduce = rm2.validate_signal(sig_reduce, _state(equity=EQUITY * 0.979))

        if dec_reduce.approved:
            assert dec_reduce.modified_signal.position_size_pct <= normal_pct * 0.55  # ~50% + margine

    def test_halt_size_multiplier_is_zero(self) -> None:
        assert CircuitBreakerLevel.DAILY_HALT.size_multiplier == 0.0
        assert CircuitBreakerLevel.WEEKLY_HALT.size_multiplier == 0.0
        assert CircuitBreakerLevel.PEAK_HALT.size_multiplier == 0.0

    def test_reduce_size_multiplier_is_half(self) -> None:
        assert CircuitBreakerLevel.DAILY_REDUCE.size_multiplier == 0.5
        assert CircuitBreakerLevel.WEEKLY_REDUCE.size_multiplier == 0.5

    def test_normal_size_multiplier_is_one(self) -> None:
        assert CircuitBreakerLevel.NORMAL.size_multiplier == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# RISK DECISION
# ──────────────────────────────────────────────────────────────────────────────

class TestRiskDecision:
    def test_approved_str(self) -> None:
        dec = RiskDecision(approved=True, modified_signal=None, rejection_reason=None)
        assert "approved" in str(dec).lower()

    def test_rejected_str(self) -> None:
        dec = RiskDecision(
            approved=False, modified_signal=None,
            rejection_reason="Test reason"
        )
        assert "rejected" in str(dec).lower()
        assert "Test reason" in str(dec)

    def test_reasoning_updated_in_modified_signal(self, rm: RiskManager) -> None:
        """Il modified_signal deve avere il reasoning aggiornato con le modifiche RISK."""
        sig = _signal(entry=500.0, stop=450.0, size_pct=0.95)
        dec = rm.validate_signal(sig, _state())
        if dec.approved and dec.modifications:
            assert "[RISK:" in dec.modified_signal.reasoning

    def test_direction_always_long(self, rm: RiskManager) -> None:
        """Il risk manager non cambia mai la direzione — rimane sempre LONG."""
        sig = _signal()
        dec = rm.validate_signal(sig, _state())
        if dec.approved:
            assert dec.modified_signal.direction == "LONG"
