"""
Risk Manager — veto assoluto su ogni segnale di trading.

Opera INDIPENDENTEMENTE dall'HMM: anche se il modello fallisce completamente,
i circuit breaker rilevano i drawdown sul P&L reale e agiscono.

Gerarchia delle protezioni (priorità decrescente):
  1. Lock file  (trading_halted.lock — rimozione manuale)
  2. PEAK_HALT  (DD dal picco > 10% → scrive il lock file)
  3. WEEKLY_HALT (DD settimanale > 7% → chiude tutto, nessun trade)
  4. DAILY_HALT  (DD giornaliero > 3% → chiude tutto, nessun trade oggi)
  5. WEEKLY_REDUCE (DD settimanale > 5% → sizing ×0.5)
  6. DAILY_REDUCE  (DD giornaliero > 2% → sizing ×0.5)
  7. Stop loss obbligatorio su ogni ordine
  8. Risk sizing: 1% per trade, gap overnight ×3
  9. Limite singola posizione: 15%
 10. Limite esposizione totale: 80%
 11. Limite settore correlato: 30%
 12. Correlazione >0.85 → rifiuto; >0.7 → sizing ×0.5
 13. Leva: solo low-vol, forza 1.0× con qualsiasi breaker attivo
 14. Duplicate: stesso simbolo+direzione entro 60 secondi → rifiuto
 15. Limiti operativi: max 5 posizioni aperte, 20 trade/giorno
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from core.regime_strategies import Signal

logger = logging.getLogger("regime-trader")

# Mappatura settore per i ticker comuni (estendibile via config)
_SECTOR_MAP: dict[str, str] = {
    "SPY":  "ETF_US_EQUITY",
    "QQQ":  "ETF_NASDAQ",
    "IWM":  "ETF_SMALL_CAP",
    "GLD":  "COMMODITY",
    "TLT":  "FIXED_INCOME",
    "AAPL": "TECHNOLOGY",
    "MSFT": "TECHNOLOGY",
    "GOOGL": "TECHNOLOGY",
    "META": "TECHNOLOGY",
    "AMZN": "CONSUMER_TECHNOLOGY",
    "NVDA": "SEMICONDUCTOR",
    "AMD":  "SEMICONDUCTOR",
    "TSLA": "CONSUMER_DISCRETIONARY",
    "NFLX": "CONSUMER_TECHNOLOGY",
    "BABA": "CONSUMER_TECHNOLOGY",
}


# ──────────────────────────────────────────────────────────────────────────────
# ENUMS
# ──────────────────────────────────────────────────────────────────────────────

class CircuitBreakerLevel(str, Enum):
    NORMAL         = "NORMAL"
    DAILY_REDUCE   = "DAILY_REDUCE"    # DD > 2% oggi → sizing ×0.5
    DAILY_HALT     = "DAILY_HALT"      # DD > 3% oggi → nessun nuovo trade
    WEEKLY_REDUCE  = "WEEKLY_REDUCE"   # DD > 5% questa settimana → sizing ×0.5
    WEEKLY_HALT    = "WEEKLY_HALT"     # DD > 7% questa settimana → nessun trade
    PEAK_HALT      = "PEAK_HALT"       # DD > 10% dal picco → lock file permanente

    @property
    def is_halt(self) -> bool:
        return self in (
            CircuitBreakerLevel.DAILY_HALT,
            CircuitBreakerLevel.WEEKLY_HALT,
            CircuitBreakerLevel.PEAK_HALT,
        )

    @property
    def is_reduce(self) -> bool:
        return self in (
            CircuitBreakerLevel.DAILY_REDUCE,
            CircuitBreakerLevel.WEEKLY_REDUCE,
        )

    @property
    def size_multiplier(self) -> float:
        """Moltiplicatore di sizing associato al livello."""
        if self.is_halt:
            return 0.0
        if self.is_reduce:
            return 0.5
        return 1.0

    @classmethod
    def _severity(cls, level: "CircuitBreakerLevel") -> int:
        _order = {
            cls.NORMAL: 0, cls.DAILY_REDUCE: 1, cls.WEEKLY_REDUCE: 2,
            cls.DAILY_HALT: 3, cls.WEEKLY_HALT: 4, cls.PEAK_HALT: 5,
        }
        return _order[level]

    @classmethod
    def worst(cls, a: "CircuitBreakerLevel", b: "CircuitBreakerLevel") -> "CircuitBreakerLevel":
        return a if cls._severity(a) >= cls._severity(b) else b


# ──────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionInfo:
    """Snapshot di una posizione aperta nel portafoglio."""
    symbol: str
    shares: float
    entry_price: float
    current_price: float
    stop_loss: float
    sector: str = "UNKNOWN"

    @property
    def market_value(self) -> float:
        return self.shares * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.shares


@dataclass
class PortfolioState:
    """
    Snapshot completo dello stato del portafoglio.
    Passato a validate_signal() come input read-only.
    """
    equity: float                                # NAV corrente
    cash: float                                  # Cash disponibile
    buying_power: float                          # Potere d'acquisto Alpaca
    positions: dict[str, PositionInfo] = field(default_factory=dict)
    daily_pnl: float = 0.0                       # P&L dal giorno corrente
    weekly_pnl: float = 0.0                      # P&L da lunedì
    peak_equity: float = 0.0                     # High-water mark
    drawdown: float = 0.0                        # DD attuale dal picco
    circuit_breaker_status: CircuitBreakerLevel = CircuitBreakerLevel.NORMAL
    flicker_rate: float = 0.0                    # Tasso di flicker HMM
    current_regime: str = "UNKNOWN"              # Regime rilevato (per logging)
    current_date: Optional[date] = None
    # Storico prezzi close per correlazioni (colonne = simboli)
    price_history: Optional[pd.DataFrame] = None

    @property
    def total_exposure(self) -> float:
        """Esposizione totale come frazione dell'equity."""
        total_mkt = sum(p.market_value for p in self.positions.values())
        return total_mkt / self.equity if self.equity > 0 else 0.0

    @property
    def n_positions(self) -> int:
        return len(self.positions)

    def sector_exposure(self, sector: str) -> float:
        """Esposizione settoriale come frazione dell'equity."""
        total = sum(
            p.market_value for p in self.positions.values()
            if p.sector == sector
        )
        return total / self.equity if self.equity > 0 else 0.0


@dataclass
class RiskDecision:
    """Risultato della validazione del rischio per un singolo segnale."""
    approved: bool
    modified_signal: Optional[Signal]           # None se rifiutato
    rejection_reason: Optional[str]             # Motivo se non approvato
    modifications: list[str] = field(default_factory=list)
    size_multiplier: float = 1.0               # Moltiplicatore finale applicato

    def __str__(self) -> str:
        if self.approved:
            mods = f" [{', '.join(self.modifications)}]" if self.modifications else ""
            return f"RiskDecision(approved, ×{self.size_multiplier:.2f}{mods})"
        return f"RiskDecision(rejected: {self.rejection_reason})"


# ──────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ──────────────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Monitora i drawdown giornalieri, settimanali e dal picco.

    Caratteristica chiave: i breaker si LATCH — una volta scattati per la
    giornata/settimana non si resettano se l'equity si riprende.
    Solo reset_daily() / reset_weekly() possono azzerarli.

    PEAK_HALT: permanente fino a rimozione manuale del lock file.
    """

    LOCK_FILE = Path("trading_halted.lock")

    def __init__(self, config: dict, lock_file: Optional[Path] = None) -> None:
        # Soglie da settings.yaml → sezione 'risk'
        self._daily_reduce  = float(config.get("daily_dd_reduce",  0.02))
        self._daily_halt    = float(config.get("daily_dd_halt",    0.03))
        self._weekly_reduce = float(config.get("weekly_dd_reduce", 0.05))
        self._weekly_halt   = float(config.get("weekly_dd_halt",   0.07))
        self._peak_halt     = float(config.get("max_dd_from_peak", 0.10))

        self._lock_path = lock_file or self.LOCK_FILE

        # Tracking interno
        self._daily_start:  float = 0.0
        self._weekly_start: float = 0.0
        self._peak:         float = 0.0
        self._current_date: Optional[date]  = None
        self._current_week: Optional[int]   = None

        # Breaker latched per periodo corrente
        self._daily_latched:  CircuitBreakerLevel = CircuitBreakerLevel.NORMAL
        self._weekly_latched: CircuitBreakerLevel = CircuitBreakerLevel.NORMAL
        self._peak_fired:     bool = False

        self._history: list[dict] = []

    def initialize(self, equity: float, current_date: date) -> None:
        """Chiamare UNA VOLTA all'avvio con l'equity iniziale."""
        self._daily_start  = equity
        self._weekly_start = equity
        self._peak         = equity
        self._current_date = current_date
        self._current_week = current_date.isocalendar()[1]

        # Controlla se il lock file esiste (da run precedente)
        if self._lock_path.exists():
            self._peak_fired = True
            logger.critical(
                f"PEAK_HALT ATTIVO: lock file trovato a '{self._lock_path}'. "
                "Rimuovere manualmente per riprendere il trading."
            )

    def update(
        self,
        current_equity: float,
        current_date: date,
        regime: str = "",
    ) -> CircuitBreakerLevel:
        """
        Aggiorna lo stato con l'equity corrente e restituisce il livello attivo.

        Chiamare ad ogni barra / aggiornamento di portfolio.
        """
        if self._peak == 0.0:
            # Prima chiamata senza initialize()
            self.initialize(current_equity, current_date)

        # Peak: non si azzera mai
        self._peak = max(self._peak, current_equity)

        # Auto-reset giornaliero al cambio di data
        if self._current_date != current_date:
            self.reset_daily(current_equity)
            self._current_date = current_date

        # Auto-reset settimanale al cambio di settimana
        week_num = current_date.isocalendar()[1]
        if self._current_week != week_num:
            self.reset_weekly(current_equity)
            self._current_week = week_num

        # Calcola drawdown correnti (tutti negativi quando in perdita)
        daily_dd  = _pct_change(current_equity, self._daily_start)
        weekly_dd = _pct_change(current_equity, self._weekly_start)
        peak_dd   = _pct_change(current_equity, self._peak)

        # Determina i nuovi livelli da latch
        new_daily = CircuitBreakerLevel.NORMAL
        if daily_dd <= -self._daily_halt:
            new_daily = CircuitBreakerLevel.DAILY_HALT
        elif daily_dd <= -self._daily_reduce:
            new_daily = CircuitBreakerLevel.DAILY_REDUCE

        new_weekly = CircuitBreakerLevel.NORMAL
        if weekly_dd <= -self._weekly_halt:
            new_weekly = CircuitBreakerLevel.WEEKLY_HALT
        elif weekly_dd <= -self._weekly_reduce:
            new_weekly = CircuitBreakerLevel.WEEKLY_REDUCE

        # Latch: il livello può solo peggiorare durante il periodo
        self._daily_latched  = CircuitBreakerLevel.worst(self._daily_latched, new_daily)
        self._weekly_latched = CircuitBreakerLevel.worst(self._weekly_latched, new_weekly)

        # Peak halt: permanente
        if peak_dd <= -self._peak_halt and not self._peak_fired:
            self._peak_fired = True
            self._trigger(
                CircuitBreakerLevel.PEAK_HALT,
                daily_dd, weekly_dd, peak_dd, current_equity, regime,
            )
            self._write_lock_file()

        # Livello finale
        level = self.check()

        # Log prima attivazione di ogni livello (evita spam)
        if level != CircuitBreakerLevel.NORMAL:
            last_in_history = (
                self._history[-1]["level"] if self._history else None
            )
            if last_in_history != level.value:
                self._trigger(level, daily_dd, weekly_dd, peak_dd, current_equity, regime)

        return level

    def check(self) -> CircuitBreakerLevel:
        """Restituisce il livello di breaker più severo attualmente attivo."""
        if self._peak_fired or self._lock_path.exists():
            return CircuitBreakerLevel.PEAK_HALT

        return CircuitBreakerLevel.worst(
            CircuitBreakerLevel.worst(self._daily_latched, self._weekly_latched),
            CircuitBreakerLevel.NORMAL,
        )

    def reset_daily(self, current_equity: float) -> None:
        """Chiama all'inizio di ogni giornata di mercato."""
        self._daily_start  = current_equity
        self._daily_latched = CircuitBreakerLevel.NORMAL
        logger.debug(f"Circuit breaker: reset giornaliero a equity={current_equity:,.0f}")

    def reset_weekly(self, current_equity: float) -> None:
        """Chiama all'inizio di ogni settimana di mercato (lunedì)."""
        self._weekly_start  = current_equity
        self._weekly_latched = CircuitBreakerLevel.NORMAL
        logger.debug(f"Circuit breaker: reset settimanale a equity={current_equity:,.0f}")

    def get_history(self) -> list[dict]:
        """Storico di tutti i trigger dei circuit breaker."""
        return list(self._history)

    def is_lock_file_present(self) -> bool:
        return self._lock_path.exists()

    # ── PRIVATI ──────────────────────────────────────────────────────────────

    def _trigger(
        self,
        level: CircuitBreakerLevel,
        daily_dd: float,
        weekly_dd: float,
        peak_dd: float,
        equity: float,
        regime: str,
    ) -> None:
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "level":     level.value,
            "daily_dd":  round(daily_dd, 6),
            "weekly_dd": round(weekly_dd, 6),
            "peak_dd":   round(peak_dd, 6),
            "equity":    round(equity, 2),
            "regime":    regime,
        }
        self._history.append(event)

        log_fn = logger.critical if level.is_halt else logger.warning
        log_fn(
            f"CIRCUIT BREAKER [{level.value}] | "
            f"daily={daily_dd:.2%} weekly={weekly_dd:.2%} peak={peak_dd:.2%} | "
            f"equity=${equity:,.0f} | regime={regime}"
        )

    def _write_lock_file(self) -> None:
        payload = {
            "triggered_at": datetime.utcnow().isoformat(),
            "reason": "Peak drawdown exceeded threshold — manual deletion required to resume",
        }
        try:
            self._lock_path.write_text(json.dumps(payload, indent=2))
            logger.critical(
                f"TRADING HALT: lock file scritto in '{self._lock_path}'. "
                "Rimuovere manualmente per sbloccare."
            )
        except OSError as e:
            logger.error(f"Impossibile scrivere il lock file: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# RISK MANAGER
# ──────────────────────────────────────────────────────────────────────────────

class RiskManager:
    """
    Valida ogni segnale rispetto a tutti i livelli di rischio configurati.

    Il RiskManager ha POTERE DI VETO ASSOLUTO: può approvare, modificare
    o rifiutare qualsiasi segnale. Le decisioni sono in cascata — ogni layer
    può ulteriormente stringere i limiti stabiliti dal layer precedente.

    Utilizzo:
        rm = RiskManager(config["risk"], initial_capital=100_000)
        decision = rm.validate_signal(signal, portfolio_state)
        if decision.approved:
            execute(decision.modified_signal)
    """

    def __init__(self, config: dict, initial_capital: float) -> None:
        """
        Args:
            config:          Sezione 'risk' di settings.yaml
            initial_capital: Capitale iniziale in USD
        """
        self.config          = config
        self.initial_capital = initial_capital

        # Limiti di portafoglio
        self._max_exposure        = float(config.get("max_exposure",       0.80))
        self._max_single_pos      = float(config.get("max_single_position", 0.15))
        self._max_leverage        = float(config.get("max_leverage",        1.25))
        self._max_concurrent      = int(config.get("max_concurrent",        5))
        self._max_daily_trades    = int(config.get("max_daily_trades",      20))
        self._max_risk_per_trade  = float(config.get("max_risk_per_trade",  0.01))
        self._max_sector_exposure = float(config.get("max_sector_exposure", 0.30))
        self._min_position_usd    = float(config.get("min_position_usd",    100.0))
        self._duplicate_secs      = int(config.get("duplicate_block_secs",  60))

        # Correlazione
        self._corr_reduce_thr = float(config.get("corr_reduce_threshold",  0.70))
        self._corr_reject_thr = float(config.get("corr_reject_threshold",  0.85))
        self._corr_window     = int(config.get("correlation_window",        60))

        # Gap risk overnight
        self._gap_multiplier  = float(config.get("gap_multiplier",          3.0))
        self._gap_max_loss    = float(config.get("gap_max_loss_pct",         0.02))

        # Flicker rate soglia per disabilitare la leva
        self._flicker_leverage_thr = int(
            config.get("flicker_threshold",
                       config.get("flicker_threshold", 4))
        )

        # Contatori
        self._daily_trade_count: int = 0
        self._daily_trade_date: Optional[date] = None
        # {(symbol, direction): last_timestamp}
        self._last_signal_ts: dict[tuple[str, str], datetime] = {}

        # Circuit Breaker
        self.circuit_breaker = CircuitBreaker(config)

        # Settore (estendibile dall'esterno)
        self._sector_map: dict[str, str] = dict(_SECTOR_MAP)
        extra_sectors = config.get("sector_map", {})
        self._sector_map.update(extra_sectors)

    # ──────────────────────────────────────────────────────────────────────
    # ENTRY POINT
    # ──────────────────────────────────────────────────────────────────────

    def validate_signal(
        self,
        signal: Signal,
        state: PortfolioState,
    ) -> RiskDecision:
        """
        Pipeline di validazione completa.

        I check vengono applicati in ordine di priorità — ogni layer può
        rifiutare o ridurre il segnale. Il primo rifiuto termina la pipeline.

        Returns:
            RiskDecision con approved=True + eventuale signal modificato,
            oppure approved=False + rejection_reason.
        """
        modifications: list[str] = []
        size_mult = 1.0

        # ── 1. Lock file / PEAK_HALT ─────────────────────────────────────
        if self.circuit_breaker.is_lock_file_present():
            return self._reject(
                "PEAK_HALT attivo: trading_halted.lock presente. "
                "Rimozione manuale necessaria per riprendere."
            )

        # ── 2. Aggiorna circuit breaker con stato corrente ────────────────
        if state.current_date:
            cb_level = self.circuit_breaker.update(
                current_equity=state.equity,
                current_date=state.current_date,
                regime=state.current_regime,
            )
        else:
            cb_level = self.circuit_breaker.check()

        # ── 3. Halt → rifiuto immediato ───────────────────────────────────
        if cb_level.is_halt:
            return self._reject(
                f"Circuit breaker {cb_level.value} attivo: "
                f"equity={state.equity:,.0f}, drawdown={state.drawdown:.2%}",
                modifications,
            )

        # ── 4. Duplicate check ────────────────────────────────────────────
        key = (signal.symbol, signal.direction)
        last_ts = self._last_signal_ts.get(key)
        if last_ts and (datetime.utcnow() - last_ts).total_seconds() < self._duplicate_secs:
            return self._reject(
                f"Segnale duplicato: {signal.symbol} {signal.direction} "
                f"già inviato {(datetime.utcnow() - last_ts).total_seconds():.0f}s fa."
            )

        # ── 5. Max posizioni aperte ───────────────────────────────────────
        already_open = state.n_positions
        is_new_position = signal.symbol not in state.positions
        if is_new_position and already_open >= self._max_concurrent:
            return self._reject(
                f"Limite posizioni aperte raggiunto: {already_open}/{self._max_concurrent}."
            )

        # ── 6. Max trade giornalieri ──────────────────────────────────────
        self._sync_daily_counter(state.current_date)
        if self._daily_trade_count >= self._max_daily_trades:
            return self._reject(
                f"Limite trade giornalieri raggiunto: {self._daily_trade_count}/{self._max_daily_trades}."
            )

        # ── 7. Stop loss obbligatorio ─────────────────────────────────────
        stop_distance = signal.entry_price - signal.stop_loss
        if signal.stop_loss <= 0 or stop_distance <= 0:
            return self._reject(
                f"Stop loss invalido o mancante: entry={signal.entry_price:.2f}, "
                f"stop={signal.stop_loss:.2f}. Ogni posizione DEVE avere uno stop."
            )

        # ── 8. Position sizing risk-based (1% per trade) ──────────────────
        risk_usd   = state.equity * self._max_risk_per_trade
        risk_shares = risk_usd / stop_distance

        # Applica gap overnight (3× stop gap-through = max 2% portfolio)
        gap_through      = self._gap_multiplier * stop_distance
        gap_max_loss_usd = state.equity * self._gap_max_loss
        gap_max_shares   = gap_max_loss_usd / gap_through
        overnight_shares = min(risk_shares, gap_max_shares)

        # Converte il sizing strategia in shares per confronto
        strategy_value  = state.equity * signal.position_size_pct
        strategy_shares = strategy_value / signal.entry_price

        # Usa il minimo tra sizing strategia e sizing risk-based
        final_shares = min(strategy_shares, overnight_shares)
        final_pct    = (final_shares * signal.entry_price) / state.equity

        if final_shares < overnight_shares:
            modifications.append(
                f"Risk sizing: {strategy_shares:.0f}→{overnight_shares:.0f} shares "
                f"(gap risk 3×, max loss 2%)"
            )

        # ── 9. Cap singola posizione (15%) ────────────────────────────────
        if final_pct > self._max_single_pos:
            final_pct    = self._max_single_pos
            final_shares = (state.equity * final_pct) / signal.entry_price
            modifications.append(f"Capped a max single position {self._max_single_pos:.0%}")

        # ── 10. Minimo posizione ($100) ────────────────────────────────────
        if final_shares * signal.entry_price < self._min_position_usd:
            return self._reject(
                f"Posizione troppo piccola: "
                f"${final_shares * signal.entry_price:.0f} < ${self._min_position_usd:.0f}."
            )

        # ── 11. Esposizione totale (80%) ───────────────────────────────────
        existing_exposure = state.total_exposure
        new_exposure      = existing_exposure + final_pct

        if new_exposure > self._max_exposure:
            # Riduci per stare al limite
            available_pct = max(self._max_exposure - existing_exposure, 0.0)
            if available_pct < 0.01:    # < 1% disponibile → non vale il trade
                return self._reject(
                    f"Esposizione massima raggiunta: "
                    f"{existing_exposure:.1%} + {final_pct:.1%} > {self._max_exposure:.0%}."
                )
            modifications.append(
                f"Esposizione ridotta: {final_pct:.1%}→{available_pct:.1%} "
                f"(limite 80%)"
            )
            final_pct    = available_pct
            final_shares = (state.equity * final_pct) / signal.entry_price

        # ── 12. Esposizione settoriale (30%) ───────────────────────────────
        sector       = self._sector_map.get(signal.symbol, "UNKNOWN")
        sector_exp   = state.sector_exposure(sector)
        new_sector   = sector_exp + final_pct

        if new_sector > self._max_sector_exposure and sector != "UNKNOWN":
            available_sec = max(self._max_sector_exposure - sector_exp, 0.0)
            if available_sec < 0.01:
                return self._reject(
                    f"Esposizione settore '{sector}' massima: "
                    f"{sector_exp:.1%} già investito (limite {self._max_sector_exposure:.0%})."
                )
            modifications.append(
                f"Settore '{sector}' ridotto: {final_pct:.1%}→{available_sec:.1%}"
            )
            final_pct    = available_sec
            final_shares = (state.equity * final_pct) / signal.entry_price

        # ── 13. Correlazione con posizioni esistenti ───────────────────────
        if state.price_history is not None and state.positions:
            max_corr = self._compute_max_correlation(
                signal.symbol, list(state.positions.keys()), state.price_history
            )

            if max_corr >= self._corr_reject_thr:
                return self._reject(
                    f"Correlazione troppo alta con posizioni esistenti: "
                    f"{max_corr:.2f} ≥ {self._corr_reject_thr:.2f}."
                )
            elif max_corr >= self._corr_reduce_thr:
                final_pct    *= 0.5
                final_shares *= 0.5
                modifications.append(
                    f"Correlazione {max_corr:.2f}: sizing ×0.5 "
                    f"(soglia {self._corr_reduce_thr:.2f})"
                )

        # ── 14. Circuit breaker REDUCE → sizing ×0.5 ──────────────────────
        if cb_level.is_reduce:
            final_pct    *= cb_level.size_multiplier
            final_shares *= cb_level.size_multiplier
            size_mult    *= cb_level.size_multiplier
            modifications.append(
                f"Circuit breaker {cb_level.value}: sizing ×{cb_level.size_multiplier}"
            )

        # ── 15. Leva: solo low-vol, force 1.0× in condizioni avverse ──────
        effective_leverage = signal.leverage
        force_1x_reasons   = []

        if cb_level != CircuitBreakerLevel.NORMAL:
            force_1x_reasons.append(f"circuit breaker {cb_level.value}")
        if state.n_positions >= 3:
            force_1x_reasons.append(f"{state.n_positions} posizioni aperte")
        if state.flicker_rate >= self._flicker_leverage_thr:
            force_1x_reasons.append(f"flicker rate {state.flicker_rate:.1f}")
        if "[UNCERTAINTY" in signal.reasoning:
            force_1x_reasons.append("regime incerto")

        if force_1x_reasons and effective_leverage > 1.0:
            effective_leverage = 1.0
            modifications.append(
                f"Leva forzata a 1.0× ({', '.join(force_1x_reasons)})"
            )

        # ── 16. Buying power ───────────────────────────────────────────────
        order_value = final_shares * signal.entry_price
        if order_value > state.buying_power:
            return self._reject(
                f"Buying power insufficiente: "
                f"ordine ${order_value:,.0f} > disponibile ${state.buying_power:,.0f}."
            )

        # ── APPROVATO ─────────────────────────────────────────────────────
        final_pct = max(0.0, min(final_pct, self._max_single_pos))

        modified = _replace_signal(
            signal,
            position_size_pct=round(final_pct, 6),
            leverage=effective_leverage,
        )

        # Registra il timestamp per il duplicate check
        self._last_signal_ts[key] = datetime.utcnow()

        if modifications:
            logger.info(
                f"[RISK] {signal.symbol}: approvato con modifiche — "
                + "; ".join(modifications)
            )
        else:
            logger.debug(f"[RISK] {signal.symbol}: approvato senza modifiche.")

        return RiskDecision(
            approved=True,
            modified_signal=modified,
            rejection_reason=None,
            modifications=modifications,
            size_multiplier=round(size_mult, 4),
        )

    # ──────────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ──────────────────────────────────────────────────────────────────────

    def initialize(self, equity: float, current_date: date) -> None:
        """Inizializza il circuit breaker e i contatori. Chiamare all'avvio."""
        self.circuit_breaker.initialize(equity, current_date)
        self._daily_trade_date  = current_date
        self._daily_trade_count = 0
        logger.info(f"RiskManager inizializzato: equity=${equity:,.0f}")

    def register_trade(self, symbol: str, current_date: Optional[date] = None) -> None:
        """
        Registra un trade eseguito. Aggiorna il contatore giornaliero.
        Chiamare DOPO l'esecuzione di un ordine.
        """
        self._sync_daily_counter(current_date)
        self._daily_trade_count += 1
        logger.debug(
            f"[RISK] Trade registrato: {symbol} | "
            f"trade giornalieri: {self._daily_trade_count}/{self._max_daily_trades}"
        )

    def reset_daily(self, equity: float, current_date: Optional[date] = None) -> None:
        """Chiama all'apertura del mercato ogni giorno."""
        self.circuit_breaker.reset_daily(equity)
        self._daily_trade_count = 0
        if current_date:
            self._daily_trade_date = current_date
        logger.info(f"[RISK] Reset giornaliero: equity=${equity:,.0f}")

    def reset_weekly(self, equity: float) -> None:
        """Chiama ogni lunedì mattina."""
        self.circuit_breaker.reset_weekly(equity)
        logger.info(f"[RISK] Reset settimanale: equity=${equity:,.0f}")

    def should_close_all_positions(self) -> bool:
        """
        True se il circuit breaker richiede la chiusura immediata
        di tutte le posizioni aperte.
        """
        return self.circuit_breaker.check().is_halt

    # ──────────────────────────────────────────────────────────────────────
    # POSITION SIZING DIRETTO (senza signal completo)
    # ──────────────────────────────────────────────────────────────────────

    def compute_safe_shares(
        self,
        entry_price: float,
        stop_loss: float,
        equity: float,
    ) -> int:
        """
        Calcola il numero massimo di azioni compatibile con:
        - 1% rischio per trade
        - 3× gap overnight ≤ 2% del portafoglio
        - 15% max position
        """
        stop_dist = entry_price - stop_loss
        if stop_dist <= 0:
            return 0

        risk_shares = (equity * self._max_risk_per_trade) / stop_dist
        gap_shares  = (equity * self._gap_max_loss) / (self._gap_multiplier * stop_dist)
        pct_shares  = (equity * self._max_single_pos) / entry_price

        return int(min(risk_shares, gap_shares, pct_shares))

    # ──────────────────────────────────────────────────────────────────────
    # UTILITÀ PRIVATE
    # ──────────────────────────────────────────────────────────────────────

    def _compute_max_correlation(
        self,
        new_symbol: str,
        existing_symbols: list[str],
        price_history: pd.DataFrame,
    ) -> float:
        """Correlazione massima tra il nuovo simbolo e le posizioni esistenti."""
        if new_symbol not in price_history.columns:
            return 0.0

        new_rets = price_history[new_symbol].pct_change().tail(self._corr_window).dropna()
        max_corr = 0.0

        for sym in existing_symbols:
            if sym == new_symbol or sym not in price_history.columns:
                continue
            exist_rets = price_history[sym].pct_change().tail(self._corr_window).dropna()
            aligned    = new_rets.align(exist_rets, join="inner")[0]
            if len(aligned) < 20:
                continue
            corr = float(new_rets.align(exist_rets, join="inner")[0].corr(
                new_rets.align(exist_rets, join="inner")[1]
            ))
            if not np.isnan(corr):
                max_corr = max(max_corr, abs(corr))

        return max_corr

    def _sync_daily_counter(self, current_date: Optional[date]) -> None:
        """Resetta il contatore trade se il giorno è cambiato."""
        if current_date and current_date != self._daily_trade_date:
            self._daily_trade_count = 0
            self._daily_trade_date  = current_date

    @staticmethod
    def _reject(reason: str, modifications: list[str] | None = None) -> RiskDecision:
        logger.warning(f"[RISK] RIFIUTO: {reason}")
        return RiskDecision(
            approved=False,
            modified_signal=None,
            rejection_reason=reason,
            modifications=modifications or [],
            size_multiplier=0.0,
        )


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY
# ──────────────────────────────────────────────────────────────────────────────

def _pct_change(current: float, start: float) -> float:
    """Variazione percentuale — negativa se in perdita."""
    return (current - start) / start if start > 0 else 0.0


def _replace_signal(signal: Signal, **kwargs) -> Signal:
    """
    Crea una copia modificata del Signal senza mutare l'originale.
    Equivalente a dataclasses.replace() ma compatibile con Signal non-frozen.
    """
    from dataclasses import replace
    mods_str = "; ".join(f"{k}={v}" for k, v in kwargs.items())
    existing_reasoning = signal.reasoning
    new_reasoning      = (
        f"{existing_reasoning} [RISK: {mods_str}]"
        if existing_reasoning
        else f"[RISK: {mods_str}]"
    )
    return replace(signal, reasoning=new_reasoning, **kwargs)
