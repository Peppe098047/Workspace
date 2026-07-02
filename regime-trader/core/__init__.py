from core.hmm_engine import HMMEngine, RegimeInfo, RegimeState, REGIME_LABELS
from core.regime_strategies import (
    Signal,
    AllocationTarget,
    BaseStrategy,
    LowVolBullStrategy,
    MidVolCautiousStrategy,
    HighVolDefensiveStrategy,
    StrategyOrchestrator,
    RegimeStrategy,
    LABEL_TO_STRATEGY,
    # alias backward-compat
    CrashDefensiveStrategy,
    BearTrendStrategy,
    BullTrendStrategy,
    EuphoriaCautiousStrategy,
    MeanReversionStrategy,
)
from core.risk_manager import RiskManager
from core.signal_generator import SignalGenerator

__all__ = [
    # HMM
    "HMMEngine", "RegimeInfo", "RegimeState", "REGIME_LABELS",
    # Strategies
    "Signal", "AllocationTarget", "BaseStrategy",
    "LowVolBullStrategy", "MidVolCautiousStrategy", "HighVolDefensiveStrategy",
    "StrategyOrchestrator", "RegimeStrategy", "LABEL_TO_STRATEGY",
    "CrashDefensiveStrategy", "BearTrendStrategy", "BullTrendStrategy",
    "EuphoriaCautiousStrategy", "MeanReversionStrategy",
    # Risk & Signals
    "RiskManager", "SignalGenerator",
]
