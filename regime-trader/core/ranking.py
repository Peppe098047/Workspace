"""
Momentum Ranker — classifica i titoli e seleziona i migliori candidati BUY.

CONCETTO (per chi inizia): il "momentum" è una delle anomalie più documentate
in finanza — i titoli che hanno performato meglio di recente tendono, in media,
a continuare a farlo nel breve periodo. Il bot lo usa per scegliere QUALI
titoli comprare; il regime HMM continua a decidere QUANTO investire in totale.

Punteggio composito per ogni titolo (z-score cross-sectional, cioè confrontato
con gli altri titoli dello stesso ciclo):

  1. MOMENTUM RISK-ADJUSTED  = rendimento ultimi N bar / (ATR% del titolo)
     Normalizzare per la volatilità evita di premiare i titoli più "ballerini":
     +3% su un titolo tranquillo vale più di +3% su uno che oscilla del 5% al giorno.

  2. RELATIVE STRENGTH vs SPY = rendimento del titolo − rendimento di SPY
     Premia chi sta battendo il mercato, non chi sale solo perché tutto sale.

score = 0.6 × z(momentum risk-adjusted) + 0.4 × z(relative strength)

I titoli GIÀ IN POSIZIONE restano sempre selezionati: vendere solo perché un
titolo è sceso di un posto in classifica genererebbe churn (e costi di spread).
L'uscita resta affidata a trailing stop e square-off serale.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger("regime-trader.ranking")


@dataclass
class RankedSymbol:
    """Punteggio di un titolo nel ranking del ciclo corrente."""
    symbol: str
    score: float
    momentum: float           # rendimento ultimi N bar
    risk_adj_momentum: float  # rendimento / ATR%
    relative_strength: float  # rendimento − rendimento benchmark


class MomentumRanker:
    """
    Config (sezione `ranking` di settings.yaml):
      enabled:        attiva/disattiva il ranking (default false)
      lookback_bars:  barre per il rendimento momentum (default 20 ≈ 3 giorni orari)
      top_k:          quanti titoli possono ricevere BUY (default 5 = max_concurrent)
      benchmark:      simbolo di riferimento per la relative strength (default SPY)
      inverse_pairs:  coppie {inverso: benchmark} in mutua esclusione (es. SH: SPY)
    """

    _ATR_PERIOD = 14

    def __init__(self, config: dict) -> None:
        cfg = config.get("ranking", {}) or {}
        self.enabled       = bool(cfg.get("enabled", False))
        self.lookback_bars = int(cfg.get("lookback_bars", 20))
        self.top_k         = int(cfg.get("top_k", 5))
        self.benchmark     = str(cfg.get("benchmark", "SPY"))

        # Mutua esclusione benchmark ↔ ETF inverso (es. SPY ↔ SH): tenerli
        # entrambi long significa auto-coprirsi pagando due spread per
        # un'esposizione netta ~0 (successo il 2026-06-10, 4ª sessione).
        # La mappa in config è {inverso: benchmark}; qui diventa simmetrica.
        pairs = cfg.get("inverse_pairs", {}) or {}
        self.inverse_pairs: dict[str, str] = {}
        for a, b in pairs.items():
            self.inverse_pairs[str(a)] = str(b)
            self.inverse_pairs[str(b)] = str(a)

    # ──────────────────────────────────────────────────────────────────────
    # API principale
    # ──────────────────────────────────────────────────────────────────────

    def select(
        self,
        symbols: list[str],
        bars: dict[str, pd.DataFrame],
        held: set[str] | None = None,
    ) -> set[str]:
        """
        Ritorna l'insieme dei simboli idonei al BUY in questo ciclo:
        i top-K del ranking + i titoli già in posizione (mai esclusi).

        Se il ranking è disattivato o non calcolabile, ritorna tutti i simboli
        (comportamento identico a prima dell'introduzione del ranking).
        """
        held = held or set()
        if not self.enabled:
            return set(symbols)

        ranked = self.rank(symbols, bars)
        if not ranked:
            logger.warning("Ranking non calcolabile (dati insufficienti): nessun filtro applicato.")
            return set(symbols)

        # Scorre la classifica e riempie il top-K saltando chi ha il proprio
        # "inverso" già scelto o già in portafoglio (mutua esclusione).
        top: list[str] = []
        pair_excluded: list[str] = []
        for r in ranked:
            if len(top) >= self.top_k:
                break
            mate = self.inverse_pairs.get(r.symbol)
            if mate and (mate in top or mate in held):
                pair_excluded.append(f"{r.symbol}↔{mate}")
                continue
            top.append(r.symbol)

        selected = set(top) | held

        if pair_excluded:
            logger.info(
                "Mutua esclusione benchmark/inverso: fuori dal BUY %s",
                ", ".join(pair_excluded),
            )

        score_by_symbol = {r.symbol: r.score for r in ranked}
        excluded = [r.symbol for r in ranked if r.symbol not in selected]
        if excluded:
            logger.info(
                "Ranking: top-%d = %s | esclusi dal BUY: %s",
                self.top_k,
                ", ".join(f"{s}({score_by_symbol[s]:+.2f})" for s in top),
                ", ".join(excluded),
            )
        return selected

    def rank(
        self,
        symbols: list[str],
        bars: dict[str, pd.DataFrame],
    ) -> list[RankedSymbol]:
        """Classifica i simboli per punteggio decrescente. Vuoto se dati insufficienti."""
        bench_ret = self._lookback_return(bars.get(self.benchmark))

        raw: list[tuple[str, float, float, float]] = []
        for sym in symbols:
            df = bars.get(sym)
            ret = self._lookback_return(df)
            if ret is None:
                continue
            atr_pct = self._atr_pct(df)
            if atr_pct is None or atr_pct <= 0:
                continue
            risk_adj = ret / atr_pct
            rel = ret - bench_ret if bench_ret is not None else 0.0
            raw.append((sym, ret, risk_adj, rel))

        if len(raw) < 2:
            return []   # con meno di 2 titoli il confronto cross-sectional non ha senso

        risk_adj_z = self._zscores([r[2] for r in raw])
        rel_z      = self._zscores([r[3] for r in raw])

        ranked = [
            RankedSymbol(
                symbol=sym,
                score=0.6 * raz + 0.4 * rz,
                momentum=ret,
                risk_adj_momentum=risk_adj,
                relative_strength=rel,
            )
            for (sym, ret, risk_adj, rel), raz, rz in zip(raw, risk_adj_z, rel_z)
        ]
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked

    # ──────────────────────────────────────────────────────────────────────
    # Helper interni
    # ──────────────────────────────────────────────────────────────────────

    def _min_bars(self) -> int:
        return self.lookback_bars + self._ATR_PERIOD + 1

    def _lookback_return(self, df: pd.DataFrame | None) -> float | None:
        """Rendimento percentuale sugli ultimi `lookback_bars`."""
        if df is None or "close" not in df or len(df) < self._min_bars():
            return None
        close = df["close"]
        past  = float(close.iloc[-self.lookback_bars - 1])
        now   = float(close.iloc[-1])
        if past <= 0:
            return None
        return now / past - 1.0

    def _atr_pct(self, df: pd.DataFrame | None) -> float | None:
        """ATR(14) in percentuale del prezzo corrente (misura di volatilità)."""
        if df is None or len(df) < self._min_bars():
            return None
        from core.regime_strategies import _atr
        price = float(df["close"].iloc[-1])
        if price <= 0:
            return None
        return _atr(df, self._ATR_PERIOD) / price

    @staticmethod
    def _zscores(values: list[float]) -> list[float]:
        """Z-score cross-sectional; tutti 0 se la deviazione standard è nulla."""
        arr = np.asarray(values, dtype=float)
        std = arr.std()
        if std <= 1e-12:
            return [0.0] * len(values)
        return list((arr - arr.mean()) / std)
