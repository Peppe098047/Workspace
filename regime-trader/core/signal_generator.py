"""
Signal Generator — orchestra HMM + Strategy + Risk per produrre segnali di trading.

Responsabilità:
- Coordinare HMM Engine, Regime Strategy e Risk Manager
- Produrre segnali di ordine (buy/sell/hold) per ogni simbolo
- Gestire la logica di ribilanciamento
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from core.hmm_engine import HMMEngine, RegimeState
from core.regime_strategies import AllocationTarget, RegimeStrategy
from core.risk_manager import RiskDecision, RiskManager


class SignalType(str, Enum):
    BUY  = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class TradingSignal:
    """Segnale di trading per un singolo simbolo."""
    symbol: str
    signal: SignalType
    target_weight: float       # Peso target nel portafoglio
    current_weight: float      # Peso attuale
    shares: float              # Numero di azioni da comprare/vendere
    regime_state: RegimeState
    risk_decision: RiskDecision
    confidence: float


class SignalGenerator:
    """
    Combina il regime HMM con la strategia di allocazione e il risk manager
    per produrre segnali concreti di trading pronti per l'esecuzione.
    """

    def __init__(
        self,
        hmm_engine: HMMEngine,
        strategy: RegimeStrategy,
        risk_manager: RiskManager,
    ) -> None:
        self.hmm    = hmm_engine
        self.strategy = strategy
        self.risk   = risk_manager

    def generate(
        self,
        features: pd.DataFrame,
        prices: pd.DataFrame,
        current_positions: dict[str, float],
        portfolio_value: float,
    ) -> list[TradingSignal]:
        """
        Genera la lista di segnali di trading per il ciclo corrente.

        Args:
            features:          Feature per il modello HMM
            prices:            Prezzi OHLCV recenti
            current_positions: Posizioni aperte {simbolo: valore USD}
            portfolio_value:   NAV corrente del portafoglio

        Returns:
            Lista di TradingSignal ordinata per urgenza
        """
        raise NotImplementedError

    def _compute_current_weights(
        self,
        current_positions: dict[str, float],
        portfolio_value: float,
    ) -> dict[str, float]:
        """Calcola i pesi attuali dal valore delle posizioni aperte."""
        raise NotImplementedError

    def _build_signal(
        self,
        symbol: str,
        target_weight: float,
        current_weight: float,
        portfolio_value: float,
        price: float,
        regime_state: RegimeState,
    ) -> TradingSignal:
        """Costruisce un singolo TradingSignal per un simbolo."""
        raise NotImplementedError
