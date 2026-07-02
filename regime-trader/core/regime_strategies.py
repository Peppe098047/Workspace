"""
Regime Strategies — allocation layer driven by HMM volatility classification.

DESIGN: the HMM detects VOLATILITY ENVIRONMENT, not price direction.
Three strategies map each volatility tier to a sizing decision:

  Low vol  → 95% invested, 1.25× leverage  (calm markets trend up ~70% of the time)
  Mid vol  → 95% if trend intact / 60% if broken, 1.0× leverage
  High vol → 60% invested, 1.0× leverage   (stay long — catch V-rebounds)

ALWAYS LONG. NEVER SHORT.
The correct response to high volatility is REDUCING allocation, not reversing.
V-rebounds are sharp; the HMM is 2-3 days late detecting them — shorting
during rebounds wipes out all crash gains.

The StrategyOrchestrator sorts regimes by expected_volatility (from HMM means),
NOT by label. "BULL" label does not imply low vol.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from core.hmm_engine import RegimeInfo, RegimeState

logger = logging.getLogger("regime-trader")

_MIN_BARS = 60  # Minimo per il warm-up degli indicatori tecnici


# ──────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """Segnale di trading per un singolo simbolo prodotto da una strategia."""
    symbol: str
    direction: str                 # "LONG" | "FLAT"
    confidence: float              # probabilità del regime [0, 1]
    entry_price: float
    stop_loss: float
    take_profit: Optional[float]
    position_size_pct: float      # frazione del portafoglio [0, 1]
    leverage: float               # 1.0 o 1.25
    regime_id: int
    regime_name: str
    regime_probability: float
    timestamp: Optional[datetime]
    reasoning: str
    strategy_name: str
    metadata: dict = field(default_factory=dict)


@dataclass
class AllocationTarget:
    """Vista aggregata backward-compatible dell'allocazione per tutti i simboli."""
    weights: dict[str, float]     # {simbolo: peso nel portafoglio}
    leverage: float
    gross_exposure: float         # somma dei pesi assoluti
    regime_name: str
    has_trend: bool               # rilevante per il regime mid-vol
    signals: list[Signal] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# INDICATORI TECNICI (helper interni)
# ──────────────────────────────────────────────────────────────────────────────

def _atr(bars: pd.DataFrame, period: int = 14) -> float:
    """ATR corrente con Wilder's smoothing (EWM alpha=1/period, adjust=False)."""
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def _ema(close: pd.Series, period: int = 50) -> float:
    """EMA corrente."""
    return float(close.ewm(span=period, adjust=False).mean().iloc[-1])


def _trend_intact(bars: pd.DataFrame, ema_period: int = 50) -> bool:
    """True se l'ultimo close è sopra la EMA(ema_period)."""
    close = bars["close"]
    return float(close.iloc[-1]) > _ema(close, ema_period)


def _apply_uncertainty(signal: Signal) -> Signal:
    """
    Dimezza il sizing e forza la leva a 1.0× in modalità incertezza.
    Triggerato quando: confidenza < soglia, flicker attivo, o regime non confermato.
    """
    return replace(
        signal,
        position_size_pct=signal.position_size_pct / 2.0,
        leverage=1.0,
        reasoning=signal.reasoning + " [UNCERTAINTY — size halved]",
    )


# ──────────────────────────────────────────────────────────────────────────────
# BASE STRATEGY
# ──────────────────────────────────────────────────────────────────────────────

class BaseStrategy(ABC):
    """Classe base astratta per tutte le strategie di regime."""

    def __init__(self, config: dict) -> None:
        self.config = config

    @property
    @abstractmethod
    def name(self) -> str:
        """Nome human-readable della strategia."""

    @abstractmethod
    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: "RegimeState",
    ) -> Optional[Signal]:
        """
        Genera un segnale di trading per un singolo simbolo.

        Args:
            symbol:       Ticker
            bars:         OHLCV DataFrame per questo simbolo
            regime_state: Stato HMM corrente

        Returns:
            Signal se c'è abbastanza storia, None altrimenti
        """

    def _has_enough_bars(self, bars: pd.DataFrame) -> bool:
        return len(bars) >= _MIN_BARS


# ──────────────────────────────────────────────────────────────────────────────
# STRATEGIA 1 — LOW VOL BULL
# ──────────────────────────────────────────────────────────────────────────────

class LowVolBullStrategy(BaseStrategy):
    """
    Regime a bassa volatilità: i mercati calmi tendono al rialzo ~70% del tempo.

    Fully invested con leva moderata — la maggior parte dei rendimenti totali
    viene generata in questo regime grazie al compounding.

    Stop: max(price - 3×ATR,  EMA50 - 0.5×ATR)
    """

    ALLOCATION    = 0.95
    LEVERAGE      = 1.25
    ATR_STOP_MULT = 3.0
    EMA_STOP_MULT = 0.5
    EMA_PERIOD    = 50
    ATR_PERIOD    = 14

    @property
    def name(self) -> str:
        return "LowVolBullStrategy"

    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: "RegimeState",
    ) -> Optional[Signal]:
        if not self._has_enough_bars(bars):
            return None

        close = bars["close"]
        price = float(close.iloc[-1])
        atr   = _atr(bars, self.ATR_PERIOD)
        ema50 = _ema(close, self.EMA_PERIOD)

        # max() = il più alto dei due stop → stop più stretto → minor drawdown
        stop_atr = price - self.ATR_STOP_MULT * atr
        stop_ema = ema50 - self.EMA_STOP_MULT * atr
        stop     = max(stop_atr, stop_ema)
        # Lo stop deve SEMPRE stare sotto il prezzo (in crash EMA50 è sopra il
        # prezzo). Il cap usa il moltiplicatore della strategia, così la
        # distanza minima resta coerente col regime.
        stop     = min(stop, price - self.EMA_STOP_MULT * atr)

        return Signal(
            symbol=symbol,
            direction="LONG",
            confidence=regime_state.probability,
            entry_price=price,
            stop_loss=stop,
            take_profit=None,
            position_size_pct=self.ALLOCATION,
            leverage=self.LEVERAGE,
            regime_id=regime_state.state_id,
            regime_name=regime_state.label,
            regime_probability=regime_state.probability,
            timestamp=regime_state.timestamp,
            reasoning=(
                f"Low-vol: fully invested {self.ALLOCATION:.0%}, 1.25× leverage. "
                f"Stop=max(price-3ATR={stop_atr:.2f}, EMA50-0.5ATR={stop_ema:.2f})"
                f"={stop:.2f}"
            ),
            strategy_name=self.name,
            metadata={
                "atr": atr,
                "ema50": ema50,
                "has_trend": True,
                "stop_atr": stop_atr,
                "stop_ema": stop_ema,
            },
        )


# ──────────────────────────────────────────────────────────────────────────────
# STRATEGIA 2 — MID VOL CAUTIOUS
# ──────────────────────────────────────────────────────────────────────────────

class MidVolCautiousStrategy(BaseStrategy):
    """
    Regime di volatilità media: rimane investito se il trend regge, riduce se no.

    Rilevamento trend: close vs EMA50.
      > EMA50 → allocazione 95%, leva 1.0× (trend intatto)
      < EMA50 → allocazione 60%, leva 1.0× (trend rotto)

    Stop: EMA50 - 0.5×ATR
    """

    ALLOCATION_TREND    = 0.95
    ALLOCATION_NO_TREND = 0.60
    LEVERAGE            = 1.0
    EMA_STOP_MULT       = 0.5
    EMA_PERIOD          = 50
    ATR_PERIOD          = 14

    @property
    def name(self) -> str:
        return "MidVolCautiousStrategy"

    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: "RegimeState",
    ) -> Optional[Signal]:
        if not self._has_enough_bars(bars):
            return None

        close    = bars["close"]
        price    = float(close.iloc[-1])
        atr      = _atr(bars, self.ATR_PERIOD)
        ema50    = _ema(close, self.EMA_PERIOD)
        trend_ok = price > ema50

        allocation  = self.ALLOCATION_TREND if trend_ok else self.ALLOCATION_NO_TREND
        stop        = ema50 - self.EMA_STOP_MULT * atr
        # Lo stop deve SEMPRE stare sotto il prezzo (in crash EMA50 è sopra il
        # prezzo). Cap col moltiplicatore della strategia.
        stop        = min(stop, price - self.EMA_STOP_MULT * atr)
        trend_label = "intact" if trend_ok else "broken"

        return Signal(
            symbol=symbol,
            direction="LONG",
            confidence=regime_state.probability,
            entry_price=price,
            stop_loss=stop,
            take_profit=None,
            position_size_pct=allocation,
            leverage=self.LEVERAGE,
            regime_id=regime_state.state_id,
            regime_name=regime_state.label,
            regime_probability=regime_state.probability,
            timestamp=regime_state.timestamp,
            reasoning=(
                f"Mid-vol: trend {trend_label} "
                f"(price={price:.2f} {'>' if trend_ok else '<'} EMA50={ema50:.2f}), "
                f"allocation={allocation:.0%}, 1.0× leverage. "
                f"Stop=EMA50-0.5ATR={stop:.2f}"
            ),
            strategy_name=self.name,
            metadata={
                "atr": atr,
                "ema50": ema50,
                "has_trend": trend_ok,
                "trend_label": trend_label,
            },
        )


# ──────────────────────────────────────────────────────────────────────────────
# STRATEGIA 3 — HIGH VOL DEFENSIVE
# ──────────────────────────────────────────────────────────────────────────────

class HighVolDefensiveStrategy(BaseStrategy):
    """
    Regime ad alta volatilità: riduce l'esposizione ma rimane LONG.

    60% allocazione cattura i rimbalzi a V senza rischiare loss illimitate
    di una posizione short. Stop più ampio (1.0×ATR sotto EMA) per tollerare
    l'alta volatilità senza essere stoppato dal rumore.

    Stop: EMA50 - 1.0×ATR
    """

    ALLOCATION    = 0.60
    LEVERAGE      = 1.0
    EMA_STOP_MULT = 1.0
    EMA_PERIOD    = 50
    ATR_PERIOD    = 14

    @property
    def name(self) -> str:
        return "HighVolDefensiveStrategy"

    def generate_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        regime_state: "RegimeState",
    ) -> Optional[Signal]:
        if not self._has_enough_bars(bars):
            return None

        close = bars["close"]
        price = float(close.iloc[-1])
        atr   = _atr(bars, self.ATR_PERIOD)
        ema50 = _ema(close, self.EMA_PERIOD)
        stop  = ema50 - self.EMA_STOP_MULT * atr
        # Lo stop deve SEMPRE stare sotto il prezzo (in crash EMA50 è sopra il
        # prezzo). Cap a 1,0×ATR (= EMA_STOP_MULT): col vecchio 0,5 fisso, in
        # crash lo stop diventava strettissimo proprio nel regime più rumoroso
        # → stop-out garantito entro l'ora (visto il 2026-06-10, 10/10 stop-out).
        stop  = min(stop, price - self.EMA_STOP_MULT * atr)

        return Signal(
            symbol=symbol,
            direction="LONG",
            confidence=regime_state.probability,
            entry_price=price,
            stop_loss=stop,
            take_profit=None,
            position_size_pct=self.ALLOCATION,
            leverage=self.LEVERAGE,
            regime_id=regime_state.state_id,
            regime_name=regime_state.label,
            regime_probability=regime_state.probability,
            timestamp=regime_state.timestamp,
            reasoning=(
                f"High-vol: 60% long (NOT short — V-rebounds are fast and "
                f"HMM is 2-3 days late detecting them). "
                f"Wider stop=EMA50-1.0ATR={stop:.2f}"
            ),
            strategy_name=self.name,
            metadata={
                "atr": atr,
                "ema50": ema50,
                "has_trend": None,   # non usato in questo regime
            },
        )


# ──────────────────────────────────────────────────────────────────────────────
# STRATEGY ORCHESTRATOR
# ──────────────────────────────────────────────────────────────────────────────

class StrategyOrchestrator:
    """
    Mappa i regime_id alle strategie in base al rango di volatilità.

    Algoritmo:
      1. Ordina i RegimeInfo per expected_volatility (crescente)
      2. Calcola position = rank / (n-1) ∈ [0.0, 1.0]
      3. position ≤ 0.33 → LowVolBullStrategy
         position ≥ 0.67 → HighVolDefensiveStrategy
         altrimenti      → MidVolCautiousStrategy

    Le etichette HMM (BULL, BEAR, ecc.) vengono IGNORATE.
    "BULL" label ≠ low vol. Conta solo expected_volatility.
    """

    def __init__(
        self,
        config: dict,
        regime_infos: dict[int, "RegimeInfo"],
    ) -> None:
        self.config = config
        self._strategy_map: dict[int, BaseStrategy] = {}
        self._vol_ranks: dict[int, float] = {}
        self._build_strategy_map(regime_infos)

    # ──────────────────────────────────────────────────────────────────────
    # Costruzione mappa
    # ──────────────────────────────────────────────────────────────────────

    def _build_strategy_map(self, regime_infos: dict[int, "RegimeInfo"]) -> None:
        """Ricostruisce il mapping regime_id → strategia."""
        sorted_by_vol = sorted(
            regime_infos.values(), key=lambda r: r.expected_volatility
        )
        n = len(sorted_by_vol)

        self._strategy_map = {}
        self._vol_ranks    = {}

        for rank, info in enumerate(sorted_by_vol):
            position = rank / max(n - 1, 1)   # [0.0, 1.0]

            if position <= 0.33:
                strategy = LowVolBullStrategy(self.config)
                tier     = "LOW"
            elif position >= 0.67:
                strategy = HighVolDefensiveStrategy(self.config)
                tier     = "HIGH"
            else:
                strategy = MidVolCautiousStrategy(self.config)
                tier     = "MID"

            self._strategy_map[info.regime_id] = strategy
            self._vol_ranks[info.regime_id]    = position

            logger.debug(
                f"  regime {info.regime_id} ({info.regime_name}): "
                f"vol={info.expected_volatility:.4f}, "
                f"pos={position:.2f} → {tier} → {strategy.name}"
            )

        tiers = [s.name for s in self._strategy_map.values()]
        logger.info(f"StrategyOrchestrator: {n} regimes mapped → {tiers}")

    # ──────────────────────────────────────────────────────────────────────
    # Generazione segnali
    # ──────────────────────────────────────────────────────────────────────

    def generate_signals(
        self,
        symbols: list[str],
        bars: dict[str, pd.DataFrame],
        regime_state: "RegimeState",
        is_flickering: bool = False,
    ) -> list[Signal]:
        """
        Genera segnali per tutti i simboli nel regime corrente.

        Attiva la modalità incertezza (sizing dimezzato, leva 1.0×) se:
        - probabilità del regime < min_confidence
        - HMM è in flicker (troppi cambi di regime recenti)
        - il regime non è ancora confermato (dentro la finestra N-barre)
        """
        strategy = self._strategy_map.get(regime_state.state_id)
        if strategy is None:
            logger.warning(
                f"Nessuna strategia per regime_id={regime_state.state_id}. "
                f"IDs noti: {list(self._strategy_map.keys())}"
            )
            return []

        min_conf  = self.config.get("min_confidence", 0.55)
        uncertain = (
            regime_state.probability < min_conf
            or is_flickering
            or not regime_state.is_confirmed
        )

        signals: list[Signal] = []
        skipped: list[str]    = []

        for symbol in symbols:
            if symbol not in bars:
                skipped.append(symbol)
                continue

            sig = strategy.generate_signal(symbol, bars[symbol], regime_state)
            if sig is None:
                skipped.append(symbol)
                continue

            if uncertain:
                sig = _apply_uncertainty(sig)

            signals.append(sig)

        if skipped:
            logger.debug(f"Simboli saltati (dati insufficienti): {skipped}")

        if signals:
            logger.info(
                f"{len(signals)} segnali: regime={regime_state.label} | "
                f"strategy={strategy.name} | "
                f"size={signals[0].position_size_pct:.0%} | "
                f"leva={signals[0].leverage}× | "
                f"uncertain={uncertain}"
            )

        return signals

    # ──────────────────────────────────────────────────────────────────────
    # Ribilanciamento
    # ──────────────────────────────────────────────────────────────────────

    def needs_rebalance(
        self,
        current_weights: dict[str, float],
        target_weights: dict[str, float],
        threshold: float = 0.10,
    ) -> bool:
        """
        True se almeno un simbolo ha uno scarto > threshold tra peso corrente e target.
        Evita il churn da piccole fluttuazioni di probabilità.
        """
        all_symbols = set(current_weights) | set(target_weights)
        return any(
            abs(current_weights.get(s, 0.0) - target_weights.get(s, 0.0)) > threshold
            for s in all_symbols
        )

    # ──────────────────────────────────────────────────────────────────────
    # Aggiornamento post-retrain
    # ──────────────────────────────────────────────────────────────────────

    def update_regime_infos(self, regime_infos: dict[int, "RegimeInfo"]) -> None:
        """Ricostruisce il mapping dopo il retrain dell'HMM."""
        logger.info("StrategyOrchestrator: rebuild dopo retrain HMM.")
        self._build_strategy_map(regime_infos)

    # ──────────────────────────────────────────────────────────────────────
    # Query
    # ──────────────────────────────────────────────────────────────────────

    def get_vol_rank(self, regime_id: int) -> Optional[float]:
        """Rango di volatilità [0.0, 1.0] per un regime_id."""
        return self._vol_ranks.get(regime_id)

    def get_strategy(self, regime_id: int) -> Optional[BaseStrategy]:
        """Istanza della strategia per un regime_id."""
        return self._strategy_map.get(regime_id)

    def get_allocation_summary(self) -> dict[int, dict]:
        """Sommario leggibile del mapping regime → strategia."""
        return {
            rid: {
                "strategy": s.name,
                "vol_rank": self._vol_ranks.get(rid, 0.0),
            }
            for rid, s in self._strategy_map.items()
        }


# ──────────────────────────────────────────────────────────────────────────────
# BACKWARD-COMPATIBLE WRAPPER
# ──────────────────────────────────────────────────────────────────────────────

class RegimeStrategy:
    """
    Facade backward-compatible — wrappa StrategyOrchestrator con l'interfaccia
    originale (compute_target / needs_rebalance).

    Per nuovo codice, usare direttamente StrategyOrchestrator.
    """

    def __init__(
        self,
        config: dict,
        symbols: list[str],
        regime_infos: Optional[dict] = None,
    ) -> None:
        self.config  = config
        self.symbols = symbols
        self._orchestrator: Optional[StrategyOrchestrator] = None
        if regime_infos:
            self._orchestrator = StrategyOrchestrator(config, regime_infos)

    def bind_regime_infos(self, regime_infos: dict) -> None:
        """Collega i RegimeInfo dell'HMM. Obbligatorio prima di compute_target()."""
        self._orchestrator = StrategyOrchestrator(self.config, regime_infos)

    def compute_target(
        self,
        regime_state: "RegimeState",
        bars: dict[str, pd.DataFrame],
        is_flickering: bool = False,
    ) -> AllocationTarget:
        """Calcola l'AllocationTarget per tutti i simboli nel regime corrente."""
        if self._orchestrator is None:
            raise RuntimeError(
                "RegimeStrategy: chiamare bind_regime_infos() con "
                "l'output dell'HMM prima di compute_target()."
            )

        signals = self._orchestrator.generate_signals(
            self.symbols, bars, regime_state, is_flickering
        )

        if not signals:
            return AllocationTarget(
                weights={s: 0.0 for s in self.symbols},
                leverage=1.0,
                gross_exposure=0.0,
                regime_name=regime_state.label,
                has_trend=False,
                signals=[],
            )

        # Equal-weight tra i simboli con segnale valido
        size_pct = signals[0].position_size_pct
        per_sym  = size_pct / len(signals)
        weights  = {sig.symbol: per_sym for sig in signals}

        has_trend = signals[0].metadata.get("has_trend")
        if has_trend is None:
            has_trend = False

        return AllocationTarget(
            weights=weights,
            leverage=signals[0].leverage,
            gross_exposure=sum(weights.values()),
            regime_name=regime_state.label,
            has_trend=bool(has_trend),
            signals=signals,
        )

    def needs_rebalance(
        self,
        current_weights: dict[str, float],
        target_weights: dict[str, float],
    ) -> bool:
        """True se lo scarto supera la soglia di ribilanciamento configurata."""
        threshold = self.config.get("rebalance_threshold", 0.10)
        if self._orchestrator:
            return self._orchestrator.needs_rebalance(
                current_weights, target_weights, threshold
            )
        all_syms = set(current_weights) | set(target_weights)
        return any(
            abs(current_weights.get(s, 0.0) - target_weights.get(s, 0.0)) > threshold
            for s in all_syms
        )


# ──────────────────────────────────────────────────────────────────────────────
# ALIAS BACKWARD-COMPATIBLE
# ──────────────────────────────────────────────────────────────────────────────

# Alias per etichette HMM: solo come riferimento, l'orchestratore usa la vol
CrashDefensiveStrategy   = HighVolDefensiveStrategy
BearTrendStrategy        = HighVolDefensiveStrategy
WeakBearStrategy         = MidVolCautiousStrategy
MeanReversionStrategy    = MidVolCautiousStrategy
WeakBullStrategy         = MidVolCautiousStrategy
BullTrendStrategy        = LowVolBullStrategy
StrongBullStrategy       = LowVolBullStrategy
EuphoriaCautiousStrategy = LowVolBullStrategy

# Lookup label → classe strategia (riferimento, ignorato dall'orchestratore)
LABEL_TO_STRATEGY: dict[str, type[BaseStrategy]] = {
    "CRASH":        HighVolDefensiveStrategy,
    "STRONG_BEAR":  HighVolDefensiveStrategy,
    "BEAR":         HighVolDefensiveStrategy,
    "WEAK_BEAR":    MidVolCautiousStrategy,
    "NEUTRAL":      MidVolCautiousStrategy,
    "WEAK_BULL":    MidVolCautiousStrategy,
    "BULL":         LowVolBullStrategy,
    "STRONG_BULL":  LowVolBullStrategy,
    "EUPHORIA":     LowVolBullStrategy,
}
