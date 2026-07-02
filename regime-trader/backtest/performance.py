"""
Analisi della performance del backtest.

Metriche calcolate:
- Core: total_return, CAGR, Sharpe, Sortino, Calmar, max drawdown
- Regime: breakdown per regime (n_bars, sharpe, avg_return)
- Confidenza: sharpe per bucket <50%, 50-60%, 60-70%, 70%+
- Benchmark: buy&hold, SMA-200, random entry (100 seed)
- Worst-case: worst day/week/month, consecutive losses, time underwater
- Export: CSV + tabelle Rich
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("regime-trader")

try:
    from rich.console import Console
    from rich.table import Table

    _RICH = True
except ImportError:
    _RICH = False


# ──────────────────────────────────────────────────────────────────────────────
# DATACLASSES
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeBreakdown:
    """Statistiche di performance per un singolo regime."""
    regime_name: str
    n_bars: int
    avg_daily_return: float
    annualized_return: float
    annualized_vol: float
    sharpe: float
    avg_allocation: float


@dataclass
class ConfidenceBucket:
    """Statistiche per un bucket di confidenza del regime."""
    label: str         # es. "70-80%"
    n_bars: int
    avg_return: float
    sharpe: float
    win_rate: float    # % barre con rendimento positivo


@dataclass
class BenchmarkResult:
    """Confronto tra strategia e un benchmark."""
    name: str
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    equity_curve: pd.Series


@dataclass
class PerformanceMetrics:
    """Metriche complete di performance per l'intero backtest."""
    # Rendimento
    total_return: float
    cagr: float
    # Rischio-rendimento
    sharpe: float
    sortino: float
    calmar: float
    # Drawdown
    max_drawdown_pct: float
    max_drawdown_days: int
    # Worst-case
    worst_day: float
    worst_week: float
    worst_month: float
    # Attività
    n_rebalances: int
    avg_allocation: float
    # Serie
    equity_curve: pd.Series
    daily_returns: pd.Series
    # Analisi per regime e confidenza
    regime_breakdown: list[RegimeBreakdown] = field(default_factory=list)
    confidence_buckets: list[ConfidenceBucket] = field(default_factory=list)
    # Benchmark
    benchmarks: list[BenchmarkResult] = field(default_factory=list)
    # Extra
    max_consecutive_losses: int = 0
    longest_time_underwater: int = 0
    # Metadata
    n_bars: int = 0
    start_date: Optional[pd.Timestamp] = None
    end_date: Optional[pd.Timestamp] = None


@dataclass
class StressScenario:
    """Singolo scenario di stress test."""
    name: str
    description: str
    modified_returns: pd.Series


@dataclass
class StressResult:
    """Risultati aggregati dello stress testing."""
    scenarios: list[StressScenario] = field(default_factory=list)
    worst_equity_curve: Optional[pd.Series] = None
    mean_max_loss: float = 0.0
    pct_circuit_breaker_fired: float = 0.0


# ──────────────────────────────────────────────────────────────────────────────
# CALCOLO METRICHE
# ──────────────────────────────────────────────────────────────────────────────

def _sharpe(returns: pd.Series, rf: float = 0.0, ann: int = 252) -> float:
    """Sharpe ratio annualizzato, restituisce 0 se vol = 0."""
    excess = returns - rf / ann
    vol    = excess.std()
    return float(excess.mean() / vol * math.sqrt(ann)) if vol > 0 else 0.0


def _sortino(returns: pd.Series, rf: float = 0.0, ann: int = 252) -> float:
    """Sortino ratio — usa solo deviazione negativa."""
    excess   = returns - rf / ann
    downside = excess[excess < 0]
    dstd     = downside.std()
    return float(excess.mean() / dstd * math.sqrt(ann)) if dstd > 0 else 0.0


def _cagr(equity: pd.Series, ann: int = 252) -> float:
    """CAGR dalla serie di equity (non dai rendimenti)."""
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    n_years = len(equity) / ann
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / n_years) - 1)


def _max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """
    Restituisce (max_drawdown_pct, max_duration_in_bars).
    max_drawdown_pct è negativo.
    """
    hwm    = equity.cummax()
    dd     = equity / hwm - 1
    max_dd = float(dd.min())

    underwater = dd < 0
    max_dur = current = 0
    for u in underwater:
        if u:
            current += 1
            max_dur  = max(max_dur, current)
        else:
            current  = 0

    return max_dd, max_dur


def _calmar(cagr: float, max_dd: float) -> float:
    return cagr / abs(max_dd) if max_dd < 0 else 0.0


def _worst_n_bars(returns: pd.Series, n: int) -> float:
    """Rendimento peggiore su finestre rolling di n barre."""
    rolling = returns.rolling(n).sum()
    return float(rolling.min()) if not rolling.empty else 0.0


def _max_consecutive_losses(returns: pd.Series) -> int:
    streak = max_streak = 0
    for r in returns:
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


def _longest_underwater(equity: pd.Series) -> int:
    hwm        = equity.cummax()
    underwater = equity < hwm
    streak = max_streak = 0
    for u in underwater:
        if u:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


# ──────────────────────────────────────────────────────────────────────────────
# ANALISI PER REGIME E CONFIDENZA
# ──────────────────────────────────────────────────────────────────────────────

def _compute_regime_breakdown(
    returns: pd.Series,
    regimes: pd.Series,
    allocation: pd.Series,
) -> list[RegimeBreakdown]:
    """Statistiche di rendimento aggregate per etichetta di regime."""
    aligned = pd.DataFrame({"ret": returns, "regime": regimes, "alloc": allocation}).dropna()
    results = []

    for regime_name, grp in aligned.groupby("regime"):
        n      = len(grp)
        avg_r  = float(grp["ret"].mean())
        vol    = float(grp["ret"].std()) if n > 1 else 0.0
        sharpe = float(avg_r / vol * math.sqrt(252)) if vol > 0 else 0.0

        results.append(RegimeBreakdown(
            regime_name=str(regime_name),
            n_bars=n,
            avg_daily_return=avg_r,
            annualized_return=float((1 + avg_r) ** 252 - 1),
            annualized_vol=float(vol * math.sqrt(252)),
            sharpe=sharpe,
            avg_allocation=float(grp["alloc"].mean()),
        ))

    return sorted(results, key=lambda r: r.annualized_return)


def _compute_confidence_buckets(
    returns: pd.Series,
    probs: pd.Series,
) -> list[ConfidenceBucket]:
    """Statistiche aggregate per bucket di confidenza del regime."""
    bins   = [0.0, 0.50, 0.60, 0.70, 0.80, 1.01]
    labels = ["<50%", "50-60%", "60-70%", "70-80%", ">80%"]

    aligned = pd.DataFrame({"ret": returns, "prob": probs}).dropna()
    aligned["bucket"] = pd.cut(aligned["prob"], bins=bins, labels=labels, right=False)

    results = []
    for label in labels:
        grp = aligned[aligned["bucket"] == label]
        n   = len(grp)
        if n == 0:
            results.append(ConfidenceBucket(label, 0, 0.0, 0.0, 0.0))
            continue

        avg_r  = float(grp["ret"].mean())
        vol    = float(grp["ret"].std()) if n > 1 else 0.0
        sharpe = float(avg_r / vol * math.sqrt(252)) if vol > 0 else 0.0
        win    = float((grp["ret"] > 0).mean())

        results.append(ConfidenceBucket(label, n, avg_r, sharpe, win))

    return results


# ──────────────────────────────────────────────────────────────────────────────
# BENCHMARK
# ──────────────────────────────────────────────────────────────────────────────

def _benchmark_buy_hold(
    ohlcv: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_capital: float,
) -> BenchmarkResult:
    """Buy-and-hold: compra all'inizio, tiene fino alla fine."""
    close = ohlcv["close"].loc[start:end]
    eq    = close / close.iloc[0] * initial_capital
    rets  = eq.pct_change().dropna()

    max_dd, _ = _max_drawdown(eq)
    return BenchmarkResult(
        name="Buy & Hold",
        total_return=float(eq.iloc[-1] / eq.iloc[0] - 1),
        cagr=_cagr(eq),
        sharpe=_sharpe(rets),
        max_drawdown=max_dd,
        equity_curve=eq,
    )


def _benchmark_sma200(
    ohlcv: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_capital: float,
) -> BenchmarkResult:
    """
    SMA-200 trend: long quando close > SMA200, flat altrimenti.
    Nessuna leva, slippage zero (confronto pulito).
    """
    close    = ohlcv["close"]
    sma      = close.rolling(200).mean()
    long_sig = (close > sma).astype(float)

    close_oos = close.loc[start:end]
    long_oos  = long_sig.loc[start:end]
    rets      = (close_oos.pct_change() * long_oos.shift(1)).dropna()
    eq        = (1 + rets).cumprod() * initial_capital

    max_dd, _ = _max_drawdown(eq)
    return BenchmarkResult(
        name="SMA-200 Trend",
        total_return=float(eq.iloc[-1] / eq.iloc[0] - 1) if len(eq) > 0 else 0.0,
        cagr=_cagr(eq),
        sharpe=_sharpe(rets),
        max_drawdown=max_dd,
        equity_curve=eq,
    )


def _benchmark_random_entry(
    ohlcv: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_capital: float,
    n_rebalances: int,
    n_seeds: int = 100,
) -> BenchmarkResult:
    """
    Random entry: sceglie casualmente quando essere investito al 95%
    o allo 0%, con la stessa frequenza di ribilanciamento della strategia.
    Media su n_seeds simulazioni.
    """
    close_oos = ohlcv["close"].loc[start:end]
    rets_raw  = close_oos.pct_change().dropna()
    T         = len(rets_raw)

    all_terminal: list[float] = []

    for seed in range(n_seeds):
        rng          = np.random.default_rng(seed)
        change_pts   = sorted(rng.choice(T, size=min(n_rebalances, T), replace=False))
        cp_set       = set(change_pts)
        alloc        = np.zeros(T)
        current_al   = 0.0

        for i in range(T):
            if i in cp_set:
                current_al = rng.choice([0.0, 0.95])
            alloc[i] = current_al

        sim_eq = float(((1 + rets_raw.values * alloc).cumprod())[-1]) * initial_capital
        all_terminal.append(sim_eq)

    mean_terminal = float(np.mean(all_terminal))
    total_ret     = mean_terminal / initial_capital - 1
    n_years       = max(T / 252, 1)

    return BenchmarkResult(
        name=f"Random Entry (n={n_seeds})",
        total_return=total_ret,
        cagr=float((1 + total_ret) ** (1 / n_years) - 1),
        sharpe=0.0,
        max_drawdown=0.0,
        equity_curve=pd.Series(dtype=float),
    )


# ──────────────────────────────────────────────────────────────────────────────
# ANALYZER PRINCIPALE
# ──────────────────────────────────────────────────────────────────────────────

class PerformanceAnalyzer:
    """
    Calcola e formatta tutte le metriche di performance a partire da
    un BacktestResult.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    def analyze(
        self,
        equity: pd.Series,
        returns: pd.Series,
        regimes: pd.Series,
        probs: pd.Series,
        allocation: pd.Series,
        n_rebalances: int,
        ohlcv: pd.DataFrame | None = None,
        run_benchmarks: bool = False,
        initial_capital: float = 100_000.0,
    ) -> PerformanceMetrics:
        """
        Calcola tutte le metriche.

        Args:
            equity:          NAV giornaliero
            returns:         Rendimenti giornalieri
            regimes:         Etichetta regime per ogni barra
            probs:           Confidenza del regime
            allocation:      Allocazione effettiva (può superare 1.0 con leva)
            n_rebalances:    Numero totale di ribilanciamenti
            ohlcv:           OHLCV completo (per benchmark e SMA)
            run_benchmarks:  Se True calcola anche i benchmark
            initial_capital: Capitale iniziale
        """
        if len(equity) == 0:
            logger.warning("Equity curve vuota — nessuna metrica calcolata.")
            return PerformanceMetrics(
                total_return=0.0, cagr=0.0, sharpe=0.0, sortino=0.0, calmar=0.0,
                max_drawdown_pct=0.0, max_drawdown_days=0,
                worst_day=0.0, worst_week=0.0, worst_month=0.0,
                n_rebalances=0, avg_allocation=0.0,
                equity_curve=equity, daily_returns=returns,
            )

        total_ret       = float(equity.iloc[-1] / equity.iloc[0] - 1)
        cagr            = _cagr(equity)
        sharpe          = _sharpe(returns)
        sortino         = _sortino(returns)
        max_dd, dd_days = _max_drawdown(equity)
        calmar          = _calmar(cagr, max_dd)

        worst_day     = float(returns.min()) if len(returns) > 0 else 0.0
        worst_week    = _worst_n_bars(returns, 5)
        worst_month   = _worst_n_bars(returns, 21)
        max_cons_loss = _max_consecutive_losses(returns)
        longest_uw    = _longest_underwater(equity)
        avg_alloc     = float(allocation.mean()) if len(allocation) > 0 else 0.0

        regime_bd = _compute_regime_breakdown(returns, regimes, allocation)
        conf_bd   = _compute_confidence_buckets(returns, probs)

        benchmarks: list[BenchmarkResult] = []
        if run_benchmarks and ohlcv is not None and len(equity) > 0:
            start = equity.index[0]
            end   = equity.index[-1]
            benchmarks = [
                _benchmark_buy_hold(ohlcv, start, end, initial_capital),
                _benchmark_sma200(ohlcv, start, end, initial_capital),
                _benchmark_random_entry(
                    ohlcv, start, end, initial_capital,
                    n_rebalances=n_rebalances,
                ),
            ]

        return PerformanceMetrics(
            total_return=total_ret,
            cagr=cagr,
            sharpe=sharpe,
            sortino=sortino,
            calmar=calmar,
            max_drawdown_pct=max_dd,
            max_drawdown_days=dd_days,
            worst_day=worst_day,
            worst_week=worst_week,
            worst_month=worst_month,
            n_rebalances=n_rebalances,
            avg_allocation=avg_alloc,
            equity_curve=equity,
            daily_returns=returns,
            regime_breakdown=regime_bd,
            confidence_buckets=conf_bd,
            benchmarks=benchmarks,
            max_consecutive_losses=max_cons_loss,
            longest_time_underwater=longest_uw,
            n_bars=len(equity),
            start_date=equity.index[0] if len(equity) > 0 else None,
            end_date=equity.index[-1] if len(equity) > 0 else None,
        )

    # ──────────────────────────────────────────────────────────────────────
    # OUTPUT TESTUALE (Rich o plain)
    # ──────────────────────────────────────────────────────────────────────

    def print_summary(self, metrics: PerformanceMetrics) -> None:
        """Stampa il riepilogo della performance. Usa Rich se disponibile."""
        if _RICH:
            self._print_rich(metrics)
        else:
            self._print_plain(metrics)

    def _print_rich(self, metrics: PerformanceMetrics) -> None:
        console = Console()

        t = Table(title="Performance Summary", show_header=True, header_style="bold cyan")
        t.add_column("Metrica",  style="white", min_width=28)
        t.add_column("Valore",   style="bold yellow", justify="right")

        dd_str = (
            f"{metrics.max_drawdown_pct:.1%} "
            f"({metrics.max_drawdown_days}gg)"
        )
        rows = [
            ("Periodo",              f"{metrics.start_date.date() if metrics.start_date else '-'} → "
                                     f"{metrics.end_date.date() if metrics.end_date else '-'}"),
            ("Barre",                f"{metrics.n_bars}"),
            ("Return totale",        f"{metrics.total_return:.2%}"),
            ("CAGR",                 f"{metrics.cagr:.2%}"),
            ("Sharpe",               f"{metrics.sharpe:.2f}"),
            ("Sortino",              f"{metrics.sortino:.2f}"),
            ("Calmar",               f"{metrics.calmar:.2f}"),
            ("Max Drawdown",         dd_str),
            ("Worst Day",            f"{metrics.worst_day:.2%}"),
            ("Worst Week",           f"{metrics.worst_week:.2%}"),
            ("Worst Month",          f"{metrics.worst_month:.2%}"),
            ("Ribilanciamenti",      f"{metrics.n_rebalances}"),
            ("Allocazione media",    f"{metrics.avg_allocation:.1%}"),
            ("Max consec. perdite",  f"{metrics.max_consecutive_losses}"),
            ("Max barre underwater", f"{metrics.longest_time_underwater}"),
        ]
        for k, v in rows:
            t.add_row(k, v)
        console.print(t)

        if metrics.regime_breakdown:
            tr = Table(title="Regime Breakdown", show_header=True, header_style="bold magenta")
            tr.add_column("Regime",    min_width=20)
            tr.add_column("Barre",     justify="right")
            tr.add_column("CAGR ann.", justify="right")
            tr.add_column("Vol ann.",  justify="right")
            tr.add_column("Sharpe",    justify="right")
            tr.add_column("Alloc avg", justify="right")

            for rb in metrics.regime_breakdown:
                tr.add_row(
                    rb.regime_name,
                    str(rb.n_bars),
                    f"{rb.annualized_return:.2%}",
                    f"{rb.annualized_vol:.2%}",
                    f"{rb.sharpe:.2f}",
                    f"{rb.avg_allocation:.1%}",
                )
            console.print(tr)

        if metrics.confidence_buckets:
            tc = Table(title="Confidence Buckets", show_header=True, header_style="bold blue")
            tc.add_column("Bucket",   min_width=10)
            tc.add_column("Barre",    justify="right")
            tc.add_column("Avg ret",  justify="right")
            tc.add_column("Sharpe",   justify="right")
            tc.add_column("Win rate", justify="right")

            for cb in metrics.confidence_buckets:
                if cb.n_bars == 0:
                    continue
                tc.add_row(
                    cb.label,
                    str(cb.n_bars),
                    f"{cb.avg_return:.4%}",
                    f"{cb.sharpe:.2f}",
                    f"{cb.win_rate:.1%}",
                )
            console.print(tc)

        if metrics.benchmarks:
            tb = Table(title="Benchmark", show_header=True, header_style="bold green")
            tb.add_column("Strategia", min_width=24)
            tb.add_column("Return",    justify="right")
            tb.add_column("CAGR",      justify="right")
            tb.add_column("Sharpe",    justify="right")
            tb.add_column("Max DD",    justify="right")

            for bm in metrics.benchmarks:
                tb.add_row(
                    bm.name,
                    f"{bm.total_return:.2%}",
                    f"{bm.cagr:.2%}",
                    f"{bm.sharpe:.2f}",
                    f"{bm.max_drawdown:.2%}",
                )
            console.print(tb)

    def _print_plain(self, metrics: PerformanceMetrics) -> None:
        print("=" * 50)
        print("PERFORMANCE SUMMARY")
        print("=" * 50)
        print(f"Return totale:    {metrics.total_return:.2%}")
        print(f"CAGR:             {metrics.cagr:.2%}")
        print(f"Sharpe:           {metrics.sharpe:.2f}")
        print(f"Sortino:          {metrics.sortino:.2f}")
        print(f"Calmar:           {metrics.calmar:.2f}")
        print(f"Max Drawdown:     {metrics.max_drawdown_pct:.2%} ({metrics.max_drawdown_days}gg)")
        print(f"Worst Day:        {metrics.worst_day:.2%}")
        print(f"Worst Week:       {metrics.worst_week:.2%}")
        print(f"Worst Month:      {metrics.worst_month:.2%}")
        print(f"Ribilanciamenti:  {metrics.n_rebalances}")
        print(f"Alloc media:      {metrics.avg_allocation:.1%}")
        print("=" * 50)

    # ──────────────────────────────────────────────────────────────────────
    # EXPORT CSV
    # ──────────────────────────────────────────────────────────────────────

    def export_csv(
        self,
        metrics: PerformanceMetrics,
        regimes: pd.Series,
        probs: pd.Series,
        allocation: pd.Series,
        trade_log: list,
        output_dir: str | Path = "./results",
    ) -> dict[str, Path]:
        """
        Esporta i dati di backtest in CSV.

        Returns:
            dict {nome_chiave: Path del file salvato}
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        saved: dict[str, Path] = {}

        # equity_curve.csv
        eq_df = pd.DataFrame({
            "equity":     metrics.equity_curve,
            "return":     metrics.daily_returns,
            "regime":     regimes,
            "prob":       probs,
            "allocation": allocation,
        })
        p = out / "equity_curve.csv"
        eq_df.to_csv(p)
        saved["equity_curve"] = p

        # trade_log.csv
        if trade_log:
            trade_df = pd.DataFrame([
                {
                    "date":          t.date,
                    "symbol":        t.symbol,
                    "action":        t.action,
                    "shares_delta":  t.shares_delta,
                    "exec_price":    t.exec_price,
                    "slippage_cost": t.slippage_cost,
                    "alloc_before":  t.allocation_before,
                    "alloc_after":   t.allocation_after,
                    "regime":        t.regime,
                    "regime_prob":   t.regime_probability,
                    "strategy":      t.strategy_name,
                }
                for t in trade_log
            ])
            p = out / "trade_log.csv"
            trade_df.to_csv(p, index=False)
            saved["trade_log"] = p

        # regime_history.csv
        rh_df = pd.DataFrame({
            "regime":      regimes,
            "probability": probs,
            "allocation":  allocation,
        })
        p = out / "regime_history.csv"
        rh_df.to_csv(p)
        saved["regime_history"] = p

        # benchmark_comparison.csv
        if metrics.benchmarks:
            bm_df = pd.DataFrame([
                {
                    "name":         bm.name,
                    "total_return": bm.total_return,
                    "cagr":         bm.cagr,
                    "sharpe":       bm.sharpe,
                    "max_drawdown": bm.max_drawdown,
                }
                for bm in metrics.benchmarks
            ])
            p = out / "benchmark_comparison.csv"
            bm_df.to_csv(p, index=False)
            saved["benchmark_comparison"] = p

        logger.info(f"CSV esportati in {out}/: {list(saved.keys())}")
        return saved
