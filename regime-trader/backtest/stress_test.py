"""
Stress Tester — Monte Carlo e scenari estremi.

Tre categorie di stress test:
1. Crash injection:     -5% ÷ -15% in un singolo giorno, in 10 punti casuali (100 sim.)
2. Gap risk:            gap overnight 2-5× ATR, in 10 punti casuali (50 sim.)
3. Regime misclassification: allocazioni rimescolate per verificare la robustezza

L'approccio è "post-hoc": prende la equity curve OOS già simulata e inietta
gli shock sui rendimenti, senza riaddestrare l'HMM. Rapido, sufficiente per
valutare la sensibilità a eventi estremi.

Circuit breaker: se equity scende >10% dal picco → trading sospeso (alloc=0).
Il test traccia quante simulazioni attivano il circuit breaker.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
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
class MonteCarloStats:
    """Statistiche aggregate di N simulazioni Monte Carlo."""
    n_simulations: int
    mean_total_return: float
    std_total_return: float
    mean_max_drawdown: float
    worst_max_drawdown: float
    pct_circuit_breaker: float       # % sim. con max_dd > threshold
    percentile_5_return: float       # VAR al 5% sull'intero periodo
    all_terminal_values: np.ndarray  # NAV terminale di ogni sim. (per istogramma)


@dataclass
class StressScenario:
    """Risultato di un singolo scenario di stress."""
    name: str
    description: str
    stats: MonteCarloStats
    worst_equity_curve: pd.Series    # peggior curva tra le N sim.


@dataclass
class StressResult:
    """Contenitore di tutti gli scenari di stress."""
    scenarios: list[StressScenario] = field(default_factory=list)
    initial_capital: float = 100_000.0
    circuit_breaker_threshold: float = 0.10    # max drawdown dal picco


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY INTERNE
# ──────────────────────────────────────────────────────────────────────────────

def _apply_circuit_breaker(
    returns: np.ndarray,
    threshold: float,
    initial: float = 100_000.0,
) -> tuple[np.ndarray, bool]:
    """
    Simula il circuit breaker: se equity < (picco * (1 - threshold))
    azzera i rendimenti successivi (allocazione 0).

    Returns: (returns modificati, True se circuit breaker scattato)
    """
    out      = returns.copy().astype(float)
    equity   = initial
    hwm      = initial
    breaker  = False

    for i, r in enumerate(out):
        equity *= (1 + r)
        hwm     = max(hwm, equity)
        if equity < hwm * (1 - threshold):
            out[i+1:] = 0.0    # chiude tutte le posizioni
            breaker   = True
            break

    return out, breaker


def _max_drawdown_from_returns(returns: np.ndarray, initial: float) -> float:
    """Max drawdown da array di rendimenti. Restituisce valore negativo."""
    equity = initial * np.cumprod(1 + returns)
    hwm    = np.maximum.accumulate(equity)
    dd     = equity / hwm - 1
    return float(dd.min()) if len(dd) > 0 else 0.0


def _wilder_atr(ohlcv: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR dal DataFrame OHLCV. Usato per dimensionare i gap."""
    h    = ohlcv["high"]
    lo   = ohlcv["low"]
    prev = ohlcv["close"].shift(1)
    tr   = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ──────────────────────────────────────────────────────────────────────────────
# STRESS TESTER
# ──────────────────────────────────────────────────────────────────────────────

class StressTester:
    """
    Esegue scenari di stress test post-hoc sulla serie di rendimenti OOS.

    Workflow:
        tester = StressTester(config)
        result = tester.run(returns_oos, ohlcv_oos)
        tester.print_summary(result)
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.cb_threshold = float(
            self.config.get("max_dd_from_peak", 0.10)
        )
        self.initial_capital = float(
            self.config.get("initial_capital", 100_000.0)
        )

    def run(
        self,
        returns_oos: pd.Series,
        ohlcv_oos: pd.DataFrame,
        n_crash_sim: int = 100,
        n_gap_sim: int = 50,
        n_crash_points: int = 10,
        n_gap_points: int = 10,
    ) -> StressResult:
        """
        Esegue tutti gli scenari di stress.

        Args:
            returns_oos:     Rendimenti giornalieri OOS dalla strategia
            ohlcv_oos:       OHLCV del periodo OOS (per ATR dei gap)
            n_crash_sim:     Simulazioni per crash injection
            n_gap_sim:       Simulazioni per gap risk
            n_crash_points:  Numero di crash iniettati per simulazione
            n_gap_points:    Numero di gap iniettati per simulazione
        """
        result = StressResult(
            initial_capital=self.initial_capital,
            circuit_breaker_threshold=self.cb_threshold,
        )

        crash_sc = self.run_crash_injection(
            returns_oos, n_sim=n_crash_sim, n_points=n_crash_points
        )
        result.scenarios.append(crash_sc)

        gap_sc = self.run_gap_risk(
            returns_oos, ohlcv_oos, n_sim=n_gap_sim, n_points=n_gap_points
        )
        result.scenarios.append(gap_sc)

        misclass_sc = self.run_regime_misclassification(returns_oos)
        result.scenarios.append(misclass_sc)

        return result

    # ──────────────────────────────────────────────────────────────────────
    # SCENARIO 1: CRASH INJECTION
    # ──────────────────────────────────────────────────────────────────────

    def run_crash_injection(
        self,
        base_returns: pd.Series,
        n_sim: int = 100,
        n_points: int = 10,
        min_crash: float = -0.05,
        max_crash: float = -0.15,
    ) -> StressScenario:
        """
        Inietta crash di -5% ÷ -15% in n_points barre casuali, n_sim volte.

        La strategia viene simulata con circuit breaker attivo.
        """
        T     = len(base_returns)
        arr   = base_returns.values.astype(float)
        init  = self.initial_capital

        terminal_vals = np.zeros(n_sim)
        max_dds       = np.zeros(n_sim)
        cb_fired      = 0
        worst_eq      = None
        worst_dd      = 0.0

        for s in range(n_sim):
            rng         = np.random.default_rng(s)
            sim_r       = arr.copy()
            crash_pts   = rng.choice(T, size=min(n_points, T), replace=False)
            crash_mags  = rng.uniform(min_crash, max_crash, size=len(crash_pts))

            for idx, mag in zip(crash_pts, crash_mags):
                sim_r[idx] = sim_r[idx] + mag    # sovrappone lo shock

            sim_r, fired = _apply_circuit_breaker(sim_r, self.cb_threshold, init)
            if fired:
                cb_fired += 1

            equity         = init * np.cumprod(1 + sim_r)
            terminal_vals[s] = equity[-1]
            dd             = _max_drawdown_from_returns(sim_r, init)
            max_dds[s]     = dd

            if dd < worst_dd:
                worst_dd = dd
                worst_eq = pd.Series(equity, index=base_returns.index[:len(equity)])

        stats = MonteCarloStats(
            n_simulations=n_sim,
            mean_total_return=float(np.mean(terminal_vals / init - 1)),
            std_total_return=float(np.std(terminal_vals / init - 1)),
            mean_max_drawdown=float(np.mean(max_dds)),
            worst_max_drawdown=float(np.min(max_dds)),
            pct_circuit_breaker=cb_fired / n_sim * 100,
            percentile_5_return=float(np.percentile(terminal_vals / init - 1, 5)),
            all_terminal_values=terminal_vals,
        )

        return StressScenario(
            name="Crash Injection",
            description=(
                f"{n_sim} sim., {n_points} crash/sim. da "
                f"{min_crash:.0%} a {max_crash:.0%}"
            ),
            stats=stats,
            worst_equity_curve=worst_eq if worst_eq is not None else pd.Series(dtype=float),
        )

    # ──────────────────────────────────────────────────────────────────────
    # SCENARIO 2: GAP RISK
    # ──────────────────────────────────────────────────────────────────────

    def run_gap_risk(
        self,
        base_returns: pd.Series,
        ohlcv_oos: pd.DataFrame,
        n_sim: int = 50,
        n_points: int = 10,
        min_gap_atr: float = 2.0,
        max_gap_atr: float = 5.0,
    ) -> StressScenario:
        """
        Inietta gap overnight di 2-5× ATR in n_points barre casuali.

        Il gap viene aggiunto (negativo) al rendimento della barra colpita.
        """
        T    = len(base_returns)
        arr  = base_returns.values.astype(float)
        init = self.initial_capital

        # Calcola ATR per dimensionare i gap
        atr      = _wilder_atr(ohlcv_oos).reindex(base_returns.index).fillna(0.02)
        close    = ohlcv_oos["close"].reindex(base_returns.index).ffill().fillna(100.0)
        atr_norm = (atr / close).values    # ATR come % del prezzo

        terminal_vals = np.zeros(n_sim)
        max_dds       = np.zeros(n_sim)
        cb_fired      = 0
        worst_eq      = None
        worst_dd      = 0.0

        for s in range(n_sim):
            rng      = np.random.default_rng(s + 1000)
            sim_r    = arr.copy()
            gap_pts  = rng.choice(T, size=min(n_points, T), replace=False)
            gap_mags = rng.uniform(min_gap_atr, max_gap_atr, size=len(gap_pts))

            for idx, mult in zip(gap_pts, gap_mags):
                # Gap negativo (overnight gap verso il basso)
                gap_pct      = -mult * atr_norm[idx]
                sim_r[idx]  += gap_pct

            sim_r, fired = _apply_circuit_breaker(sim_r, self.cb_threshold, init)
            if fired:
                cb_fired += 1

            equity           = init * np.cumprod(1 + sim_r)
            terminal_vals[s] = equity[-1]
            dd               = _max_drawdown_from_returns(sim_r, init)
            max_dds[s]       = dd

            if dd < worst_dd:
                worst_dd = dd
                worst_eq = pd.Series(equity, index=base_returns.index[:len(equity)])

        stats = MonteCarloStats(
            n_simulations=n_sim,
            mean_total_return=float(np.mean(terminal_vals / init - 1)),
            std_total_return=float(np.std(terminal_vals / init - 1)),
            mean_max_drawdown=float(np.mean(max_dds)),
            worst_max_drawdown=float(np.min(max_dds)),
            pct_circuit_breaker=cb_fired / n_sim * 100,
            percentile_5_return=float(np.percentile(terminal_vals / init - 1, 5)),
            all_terminal_values=terminal_vals,
        )

        return StressScenario(
            name="Gap Risk",
            description=(
                f"{n_sim} sim., {n_points} gap/sim. da "
                f"{min_gap_atr:.0f}× a {max_gap_atr:.0f}× ATR"
            ),
            stats=stats,
            worst_equity_curve=worst_eq if worst_eq is not None else pd.Series(dtype=float),
        )

    # ──────────────────────────────────────────────────────────────────────
    # SCENARIO 3: REGIME MISCLASSIFICATION
    # ──────────────────────────────────────────────────────────────────────

    def run_regime_misclassification(
        self,
        base_returns: pd.Series,
        n_permutations: int = 50,
    ) -> StressScenario:
        """
        Permuta casualmente l'ordine dei rendimenti OOS per simulare
        il caso in cui la strategia abbia classificato i regimi in modo errato
        (ordine degli scambi mescolato, ma stessa distribuzione).

        Verifica che il sizing basato sui regimi abbia valore reale vs. casuale.
        """
        arr  = base_returns.values.astype(float)
        init = self.initial_capital
        T    = len(arr)

        terminal_vals = np.zeros(n_permutations)
        max_dds       = np.zeros(n_permutations)
        cb_fired      = 0
        worst_eq      = None
        worst_dd      = 0.0

        for s in range(n_permutations):
            rng   = np.random.default_rng(s + 2000)
            perm  = rng.permutation(arr)

            perm, fired = _apply_circuit_breaker(perm, self.cb_threshold, init)
            if fired:
                cb_fired += 1

            equity           = init * np.cumprod(1 + perm)
            terminal_vals[s] = equity[-1]
            dd               = _max_drawdown_from_returns(perm, init)
            max_dds[s]       = dd

            if dd < worst_dd:
                worst_dd = dd
                worst_eq = pd.Series(equity, index=base_returns.index[:len(equity)])

        stats = MonteCarloStats(
            n_simulations=n_permutations,
            mean_total_return=float(np.mean(terminal_vals / init - 1)),
            std_total_return=float(np.std(terminal_vals / init - 1)),
            mean_max_drawdown=float(np.mean(max_dds)),
            worst_max_drawdown=float(np.min(max_dds)),
            pct_circuit_breaker=cb_fired / n_permutations * 100,
            percentile_5_return=float(np.percentile(terminal_vals / init - 1, 5)),
            all_terminal_values=terminal_vals,
        )

        return StressScenario(
            name="Regime Misclassification",
            description=f"{n_permutations} permutazioni casuali dei rendimenti OOS",
            stats=stats,
            worst_equity_curve=worst_eq if worst_eq is not None else pd.Series(dtype=float),
        )

    # ──────────────────────────────────────────────────────────────────────
    # OUTPUT
    # ──────────────────────────────────────────────────────────────────────

    def print_summary(self, result: StressResult) -> None:
        """Stampa il riepilogo degli stress test. Usa Rich se disponibile."""
        if _RICH:
            self._print_rich(result)
        else:
            self._print_plain(result)

    def _print_rich(self, result: StressResult) -> None:
        console = Console()
        t = Table(
            title=f"Stress Test Summary (circuit breaker: -{result.circuit_breaker_threshold:.0%})",
            show_header=True,
            header_style="bold red",
        )
        t.add_column("Scenario",           min_width=28)
        t.add_column("N sim.",             justify="right")
        t.add_column("Return medio",       justify="right")
        t.add_column("Std return",         justify="right")
        t.add_column("Max DD medio",       justify="right")
        t.add_column("Worst Max DD",       justify="right")
        t.add_column("5th pct return",     justify="right")
        t.add_column("Circuit breaker %",  justify="right")

        for sc in result.scenarios:
            st = sc.stats
            cb_style = "bold red" if st.pct_circuit_breaker > 50 else "white"
            t.add_row(
                sc.name,
                str(st.n_simulations),
                f"{st.mean_total_return:.2%}",
                f"{st.std_total_return:.2%}",
                f"{st.mean_max_drawdown:.2%}",
                f"{st.worst_max_drawdown:.2%}",
                f"{st.percentile_5_return:.2%}",
                f"[{cb_style}]{st.pct_circuit_breaker:.1f}%[/{cb_style}]",
            )

        console.print(t)

    def _print_plain(self, result: StressResult) -> None:
        print("=" * 70)
        print(f"STRESS TEST (circuit breaker: -{result.circuit_breaker_threshold:.0%})")
        print("=" * 70)
        for sc in result.scenarios:
            st = sc.stats
            print(f"\n{sc.name}: {sc.description}")
            print(f"  Return medio:     {st.mean_total_return:.2%}")
            print(f"  Max DD medio:     {st.mean_max_drawdown:.2%}")
            print(f"  Worst Max DD:     {st.worst_max_drawdown:.2%}")
            print(f"  5th pct return:   {st.percentile_5_return:.2%}")
            print(f"  Circuit breaker:  {st.pct_circuit_breaker:.1f}% delle sim.")
        print("=" * 70)
