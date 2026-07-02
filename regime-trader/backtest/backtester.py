"""
Walk-Forward Allocation Backtester.

Questo è un backtester BASATO SU ALLOCAZIONE, non su singoli trade.
Ad ogni barra calcola un'allocazione target (% del portafoglio investita)
e ribilancia quando la deviazione supera la soglia configurata.

Matematica dell'allocazione (esatta):
  equity        = cash + shares * price
  target_shares = int(equity * target_alloc / price)
  delta         = target_shares - current_shares
  cash         -= delta * exec_price  ← può diventare negativo (margine)

Con leva 1.25× → target_alloc > 1.0 → cash < 0 (margine).
equity = cash + shares*price rimane corretto perché il valore azionario
supera il debito margine.

Fill delay: segnale a barra N → esecuzione all'open di barra N+1.
Stop loss: SOLO nel trading live. In backtest non si usano stop intraday.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from core.hmm_engine import HMMEngine, RegimeState
from core.regime_strategies import StrategyOrchestrator
from data.feature_engineering import FeatureEngineer

logger = logging.getLogger("regime-trader")


# ──────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """Un singolo evento di ribilanciamento (cambio di allocazione)."""
    date: pd.Timestamp
    symbol: str
    action: str              # "BUY" | "SELL" | "FLAT"
    shares_delta: float
    exec_price: float
    slippage_cost: float
    allocation_before: float
    allocation_after: float
    regime: str
    regime_probability: float
    strategy_name: str
    pnl: float = 0.0         # P&L realizzato al momento della chiusura


@dataclass
class FoldResult:
    """Risultati di un singolo fold walk-forward."""
    fold_index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    equity_curve: pd.Series            # NAV giornaliero
    returns: pd.Series                 # Rendimenti giornalieri
    regime_labels: pd.Series           # Etichetta regime per ogni barra
    regime_probabilities: pd.Series    # Confidenza regime
    allocation_series: pd.Series       # Allocazione effettiva (0→1+)
    trade_log: list[TradeRecord] = field(default_factory=list)

    @property
    def n_rebalances(self) -> int:
        return len(self.trade_log)


@dataclass
class BacktestResult:
    """Risultati aggregati dell'intero backtest walk-forward."""
    folds: list[FoldResult] = field(default_factory=list)
    combined_equity: Optional[pd.Series] = None
    combined_returns: Optional[pd.Series] = None
    combined_regimes: Optional[pd.Series] = None
    combined_trades: list[TradeRecord] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def n_folds(self) -> int:
        return len(self.folds)

    @property
    def n_total_rebalances(self) -> int:
        return sum(f.n_rebalances for f in self.folds)


# ──────────────────────────────────────────────────────────────────────────────
# HELPER — ALLOCATION FROM REGIME
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_allocation(
    regime_state: RegimeState,
    vol_rank: float,
    price: float,
    ema50: float,
    is_flickering: bool,
    strategy_config: dict,
) -> tuple[float, float]:
    """
    Converte regime + vol_rank in (target_alloc, leverage) senza istanziare
    le classi strategia complete — efficiente per il loop di backtesting.

    Returns: (target_allocation, leverage) dove target_alloc può superare 1.0
    """
    min_conf  = strategy_config.get("min_confidence", 0.55)
    uncertain = (
        regime_state.probability < min_conf
        or is_flickering
        or not regime_state.is_confirmed
    )

    if vol_rank <= 0.33:
        alloc    = 0.95
        leverage = 1.25
    elif vol_rank >= 0.67:
        alloc    = 0.60
        leverage = 1.0
    else:
        # Mid-vol: dipende dal trend (close vs EMA50)
        alloc    = 0.95 if price > ema50 else 0.60
        leverage = 1.0

    if uncertain:
        alloc   /= 2.0
        leverage = 1.0

    return alloc * leverage, leverage


# ──────────────────────────────────────────────────────────────────────────────
# BACKTESTER
# ──────────────────────────────────────────────────────────────────────────────

class WalkForwardBacktester:
    """
    Backtest walk-forward allocation-based.

    Nessun look-ahead bias:
    - HMM riaddestrato su ogni IS window separatamente
    - Forward algorithm aggiornato in modo incrementale barra per barra
    - Features calcolate su dati IS+OOS fino alla barra corrente
    """

    def __init__(self, config: dict, feature_engineer: FeatureEngineer) -> None:
        self.config           = config
        self.feature_engineer = feature_engineer

    # ──────────────────────────────────────────────────────────────────────
    # ENTRY POINT
    # ──────────────────────────────────────────────────────────────────────

    def run(
        self,
        data: dict[str, pd.DataFrame],
        symbols: list[str],
        hmm_config: dict,
        strategy_config: dict,
        risk_config: dict = None,
    ) -> BacktestResult:
        """
        Esegue il backtest walk-forward completo.

        Args:
            data:            {simbolo: DataFrame OHLCV} con tutta la storia
            symbols:         Simboli da includere nel portafoglio
            hmm_config:      Config HMM (n_candidates, n_init, ecc.)
            strategy_config: Config strategia (min_confidence, threshold, ecc.)
            risk_config:     Non usato in backtest (solo per live trading)

        Returns:
            BacktestResult con fold individuali e curve aggregate
        """
        # Simbolo primario per training HMM (indice di mercato)
        primary = symbols[0]
        primary_ohlcv = data[primary]

        folds_idx = self._generate_folds(primary_ohlcv.index)
        if not folds_idx:
            raise ValueError(
                f"Dati insufficienti per il walk-forward. "
                f"Servono almeno "
                f"{self.config.get('train_window', 252) + self.config.get('test_window', 126)} barre."
            )

        logger.info(
            f"Walk-forward: {len(folds_idx)} fold(s), "
            f"IS={self.config.get('train_window', 252)} OOS={self.config.get('test_window', 126)} "
            f"step={self.config.get('step_size', 126)}"
        )

        all_folds: list[FoldResult] = []
        for i, (is_sl, oos_sl) in enumerate(folds_idx):
            logger.info(
                f"Fold {i+1}/{len(folds_idx)}: "
                f"IS [{primary_ohlcv.index[is_sl.start].date()} → "
                f"{primary_ohlcv.index[is_sl.stop-1].date()}] | "
                f"OOS [{primary_ohlcv.index[oos_sl.start].date()} → "
                f"{primary_ohlcv.index[oos_sl.stop-1].date()}]"
            )
            fold = self._run_fold(
                fold_index=i,
                is_slice=is_sl,
                oos_slice=oos_sl,
                primary_ohlcv=primary_ohlcv,
                all_data=data,
                symbols=symbols,
                hmm_config=hmm_config,
                strategy_config=strategy_config,
            )
            all_folds.append(fold)

        result = self._combine_folds(all_folds, self.config)
        return result

    # ──────────────────────────────────────────────────────────────────────
    # FOLD WINDOWS
    # ──────────────────────────────────────────────────────────────────────

    def _generate_folds(
        self, index: pd.DatetimeIndex
    ) -> list[tuple[slice, slice]]:
        """Genera le coppie (IS_slice, OOS_slice) per il walk-forward."""
        is_len  = self.config.get("train_window", 252)
        oos_len = self.config.get("test_window", 126)
        step    = self.config.get("step_size", 126)
        n       = len(index)

        folds  = []
        start  = 0
        while start + is_len + oos_len <= n:
            is_end  = start + is_len
            oos_end = min(is_end + oos_len, n)
            folds.append((slice(start, is_end), slice(is_end, oos_end)))
            start += step

        return folds

    # ──────────────────────────────────────────────────────────────────────
    # SINGOLO FOLD
    # ──────────────────────────────────────────────────────────────────────

    def _run_fold(
        self,
        fold_index: int,
        is_slice: slice,
        oos_slice: slice,
        primary_ohlcv: pd.DataFrame,
        all_data: dict[str, pd.DataFrame],
        symbols: list[str],
        hmm_config: dict,
        strategy_config: dict,
    ) -> FoldResult:
        """Addestra HMM su IS, simula OOS barra per barra."""
        # Dati IS e OOS per il simbolo primario
        is_ohlcv  = primary_ohlcv.iloc[is_slice]
        oos_ohlcv = primary_ohlcv.iloc[oos_slice]

        # Features su tutto il periodo (IS+OOS) per evitare NaN alle giunzioni
        combined_ohlcv = primary_ohlcv.iloc[:oos_slice.stop]
        all_features   = self.feature_engineer.compute(combined_ohlcv)

        is_features  = all_features.reindex(is_ohlcv.index).dropna()
        oos_features = all_features.reindex(oos_ohlcv.index).dropna()

        if len(is_features) < HMMEngine.MIN_TRAIN_BARS:
            logger.warning(
                f"Fold {fold_index}: IS features insufficienti "
                f"({len(is_features)} < {HMMEngine.MIN_TRAIN_BARS}), skip."
            )
            return self._empty_fold(fold_index, is_ohlcv, oos_ohlcv)

        # Addestramento HMM su IS
        engine = HMMEngine(config=hmm_config)
        engine.fit(is_features)

        # Mappa regime → vol_rank per generare i segnali in modo efficiente
        vol_ranks = self._build_vol_ranks(engine)

        # Precomputa EMA50 per tutto il periodo combinato (no look-ahead)
        ema50_all = combined_ohlcv["close"].ewm(span=50, adjust=False).mean()
        ema50_oos = ema50_all.reindex(oos_ohlcv.index)

        # Inizializza il forward state dell'HMM con le IS features
        engine._reset_filter()
        for _, row in is_features.iterrows():
            engine._update_forward_cache(row.values.astype(float))

        # Simula OOS
        return self._simulate_oos(
            fold_index=fold_index,
            is_ohlcv=is_ohlcv,
            oos_ohlcv=oos_ohlcv,
            oos_features=oos_features,
            ema50_oos=ema50_oos,
            engine=engine,
            vol_ranks=vol_ranks,
            symbols=symbols,
            all_data=all_data,
            strategy_config=strategy_config,
        )

    def _build_vol_ranks(self, engine: HMMEngine) -> dict[int, float]:
        """Calcola il vol_rank [0.0, 1.0] per ogni stato HMM."""
        sorted_by_vol = sorted(
            engine._regime_info.values(),
            key=lambda r: r.expected_volatility,
        )
        n = len(sorted_by_vol)
        return {
            info.regime_id: i / max(n - 1, 1)
            for i, info in enumerate(sorted_by_vol)
        }

    # ──────────────────────────────────────────────────────────────────────
    # SIMULAZIONE OOS
    # ──────────────────────────────────────────────────────────────────────

    def _simulate_oos(
        self,
        fold_index: int,
        is_ohlcv: pd.DataFrame,
        oos_ohlcv: pd.DataFrame,
        oos_features: pd.DataFrame,
        ema50_oos: pd.Series,
        engine: HMMEngine,
        vol_ranks: dict[int, float],
        symbols: list[str],
        all_data: dict[str, pd.DataFrame],
        strategy_config: dict,
    ) -> FoldResult:
        """
        Simula la strategia barra per barra sul periodo OOS.

        Fill delay: segnale a barra N → esecuzione all'open di barra N+1.
        """
        initial_capital = self.config.get("initial_capital", 100_000.0)
        slippage        = self.config.get("slippage_pct", 0.0005)
        rebal_threshold = strategy_config.get("rebalance_threshold", 0.10)
        n_sym           = len(symbols)

        # Stato portafoglio: cash + shares per simbolo
        cash    = float(initial_capital)
        shares  = {sym: 0.0 for sym in symbols}
        current_alloc = 0.0  # allocazione effettiva corrente

        equity_list  = []
        regime_list  = []
        prob_list    = []
        alloc_list   = []
        trade_log    = []

        # Segnale pendente (fill delay: eseguito all'open della barra successiva)
        pending: dict | None = None

        oos_dates = oos_ohlcv.index

        for i, bar_date in enumerate(oos_dates):
            if bar_date not in oos_features.index:
                continue

            # Prezzi correnti
            prices = self._get_prices(bar_date, all_data, symbols, "close")
            opens  = self._get_prices(bar_date, all_data, symbols, "open")

            # Esegui ribilanciamento pendente all'open di questa barra
            if pending is not None:
                cash, shares, trade = self._execute_pending(
                    pending, opens, shares, cash, slippage, bar_date
                )
                if trade:
                    trade_log.append(trade)
                    current_alloc = pending["target_alloc"]
                pending = None

            # Mark-to-market al close
            equity = cash + sum(shares[s] * prices.get(s, 0.0) for s in symbols)

            # Aggiorna HMM con la feature row corrente (incrementale O(K²))
            feat_row      = oos_features.loc[bar_date]
            regime_state  = engine.update(feat_row, timestamp=bar_date)
            vol_rank      = vol_ranks.get(regime_state.state_id, 0.5)
            ema50_val     = float(ema50_oos.loc[bar_date]) if bar_date in ema50_oos.index else float(prices.get(symbols[0], 0))
            price_primary = prices.get(symbols[0], 0.0)

            # Calcola allocazione target
            target_alloc, leverage = _resolve_allocation(
                regime_state=regime_state,
                vol_rank=vol_rank,
                price=price_primary,
                ema50=ema50_val,
                is_flickering=engine.is_flickering(),
                strategy_config=strategy_config,
            )

            # Verifica soglia di ribilanciamento
            mkt_alloc = (sum(shares[s] * prices.get(s, 0.0) for s in symbols) / equity
                         if equity > 0 else 0.0)

            if abs(mkt_alloc - target_alloc) > rebal_threshold:
                # Calcola target shares per ogni simbolo (equal weight)
                target_shares_map = {
                    sym: int(equity * target_alloc / n_sym / max(prices.get(sym, 1.0), 0.01))
                    for sym in symbols
                }
                pending = {
                    "target_shares": target_shares_map,
                    "target_alloc": target_alloc,
                    "regime_label": regime_state.label,
                    "regime_prob": regime_state.probability,
                    "strategy_name": _strategy_name(vol_rank),
                }

            equity_list.append(equity)
            regime_list.append(regime_state.label)
            prob_list.append(regime_state.probability)
            alloc_list.append(mkt_alloc)

        # Costruisci series
        valid_dates = [d for d in oos_dates if d in oos_features.index]
        equity_s = pd.Series(equity_list, index=valid_dates, name="equity")
        returns_s = equity_s.pct_change().dropna()

        return FoldResult(
            fold_index=fold_index,
            train_start=is_ohlcv.index[0],
            train_end=is_ohlcv.index[-1],
            test_start=oos_ohlcv.index[0],
            test_end=oos_ohlcv.index[-1],
            equity_curve=equity_s,
            returns=returns_s,
            regime_labels=pd.Series(regime_list, index=valid_dates, name="regime"),
            regime_probabilities=pd.Series(prob_list, index=valid_dates, name="prob"),
            allocation_series=pd.Series(alloc_list, index=valid_dates, name="alloc"),
            trade_log=trade_log,
        )

    def _execute_pending(
        self,
        pending: dict,
        opens: dict[str, float],
        shares: dict[str, float],
        cash: float,
        slippage: float,
        bar_date: pd.Timestamp,
    ) -> tuple[float, dict[str, float], TradeRecord | None]:
        """Esegue un ribilanciamento pendente all'open della barra corrente."""
        trades_executed = []

        for sym, target_qty in pending["target_shares"].items():
            current_qty = int(shares.get(sym, 0.0))
            delta       = target_qty - current_qty
            if delta == 0:
                continue

            open_price  = opens.get(sym, 0.0)
            if open_price <= 0:
                continue

            sign        = 1 if delta > 0 else -1
            fill_price  = open_price * (1 + sign * slippage)
            slip_cost   = abs(delta) * open_price * slippage

            cash          -= delta * fill_price
            shares[sym]    = float(target_qty)

            trades_executed.append(TradeRecord(
                date=bar_date,
                symbol=sym,
                action="BUY" if delta > 0 else "SELL",
                shares_delta=float(delta),
                exec_price=fill_price,
                slippage_cost=slip_cost,
                allocation_before=0.0,  # approssimato, aggiornato fuori
                allocation_after=pending["target_alloc"],
                regime=pending["regime_label"],
                regime_probability=pending["regime_prob"],
                strategy_name=pending["strategy_name"],
            ))

        # Ritorna il primo trade come rappresentativo (per semplicità)
        primary_trade = trades_executed[0] if trades_executed else None
        return cash, shares, primary_trade

    # ──────────────────────────────────────────────────────────────────────
    # AGGREGAZIONE
    # ──────────────────────────────────────────────────────────────────────

    def _combine_folds(
        self,
        folds: list[FoldResult],
        config: dict,
    ) -> BacktestResult:
        """Concatena i fold OOS in un'unica equity curve."""
        initial_capital = config.get("initial_capital", 100_000.0)

        all_returns = pd.concat([f.returns for f in folds]).sort_index()
        # Rimuove date duplicate tra fold sovrapposti
        all_returns = all_returns[~all_returns.index.duplicated(keep="first")]

        combined_equity = (1 + all_returns).cumprod() * initial_capital

        combined_regimes = pd.concat(
            [f.regime_labels for f in folds]
        ).sort_index()
        combined_regimes = combined_regimes[~combined_regimes.index.duplicated(keep="first")]

        all_trades = [t for f in folds for t in f.trade_log]

        return BacktestResult(
            folds=folds,
            combined_equity=combined_equity,
            combined_returns=all_returns,
            combined_regimes=combined_regimes,
            combined_trades=all_trades,
            config=config,
            metadata={
                "n_folds": len(folds),
                "n_bars": len(combined_equity),
                "initial_capital": initial_capital,
            },
        )

    # ──────────────────────────────────────────────────────────────────────
    # UTILITY
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_prices(
        bar_date: pd.Timestamp,
        all_data: dict[str, pd.DataFrame],
        symbols: list[str],
        col: str = "close",
    ) -> dict[str, float]:
        prices = {}
        for sym in symbols:
            df = all_data.get(sym)
            if df is not None and bar_date in df.index:
                prices[sym] = float(df.loc[bar_date, col])
        return prices

    @staticmethod
    def _empty_fold(
        fold_index: int,
        is_ohlcv: pd.DataFrame,
        oos_ohlcv: pd.DataFrame,
    ) -> FoldResult:
        """Fold vuoto per fold con dati insufficienti."""
        idx = oos_ohlcv.index
        empty = pd.Series(dtype=float)
        return FoldResult(
            fold_index=fold_index,
            train_start=is_ohlcv.index[0],
            train_end=is_ohlcv.index[-1],
            test_start=oos_ohlcv.index[0],
            test_end=oos_ohlcv.index[-1],
            equity_curve=empty,
            returns=empty,
            regime_labels=pd.Series(dtype=str),
            regime_probabilities=empty,
            allocation_series=empty,
            trade_log=[],
        )


def _strategy_name(vol_rank: float) -> str:
    if vol_rank <= 0.33:
        return "LowVolBullStrategy"
    if vol_rank >= 0.67:
        return "HighVolDefensiveStrategy"
    return "MidVolCautiousStrategy"
