"""
Feature Engineering — indicatori tecnici puri per il modello HMM.

Tutte le funzioni sono pure e senza look-ahead bias: ogni valore al tempo T
dipende esclusivamente da dati disponibili fino a T incluso.
ATR, RSI e ADX usano Wilder's smoothing (EWM con alpha=1/period, adjust=False).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ZSCORE_LOOKBACK = 252

# Ordine dei feature: log_ret_1 deve essere all'indice 0
# (l'HMM usa la colonna 0 per ordinare i regimi per rendimento medio)
FEATURE_COLUMNS = [
    "log_ret_1",       # rendimento log 1 barra
    "log_ret_5",       # rendimento log 5 barre
    "log_ret_20",      # rendimento log 20 barre
    "realized_vol_20", # volatilità realizzata 20 barre (annualizzata)
    "vol_ratio",       # vol 5-barre / vol 20-barre
    "volume_zscore",   # volume normalizzato (z-score vs media 50-barre)
    "volume_trend",    # pendenza SMA volume 10 barre
    "adx_14",          # ADX 14 periodi
    "sma50_slope",     # pendenza SMA 50 normalizzata per prezzo
    "rsi_14",          # RSI 14 periodi
    "dist_sma200",     # distanza dalla SMA 200 come % del prezzo
    "roc_10",          # rate of change 10 barre
    "roc_20",          # rate of change 20 barre
    "atr_norm",        # ATR 14 normalizzato per prezzo
]


# ──────────────────────────────────────────────────────────────────────────────
# RETURNS
# ──────────────────────────────────────────────────────────────────────────────

def log_returns(close: pd.Series, period: int = 1) -> pd.Series:
    """Rendimenti logaritmici su `period` barre."""
    return np.log(close / close.shift(period))


# ──────────────────────────────────────────────────────────────────────────────
# VOLATILITY
# ──────────────────────────────────────────────────────────────────────────────

def realized_vol(log_ret: pd.Series, window: int = 20) -> pd.Series:
    """Volatilità realizzata annualizzata (std mobile dei log return)."""
    return log_ret.rolling(window).std() * np.sqrt(252)


def vol_ratio(log_ret: pd.Series, fast: int = 5, slow: int = 20) -> pd.Series:
    """
    Rapporto tra volatilità a breve e lungo termine.
    >1 indica volatilità in espansione, <1 in contrazione.
    """
    vol_f = log_ret.rolling(fast).std()
    vol_s = log_ret.rolling(slow).std()
    return vol_f / vol_s.replace(0.0, np.nan)


# ──────────────────────────────────────────────────────────────────────────────
# VOLUME
# ──────────────────────────────────────────────────────────────────────────────

def volume_zscore(volume: pd.Series, window: int = 50) -> pd.Series:
    """Z-score del volume rispetto alla media mobile su `window` barre."""
    mu = volume.rolling(window).mean()
    sigma = volume.rolling(window).std()
    return (volume - mu) / sigma.replace(0.0, np.nan)


def volume_trend(volume: pd.Series, sma_window: int = 10) -> pd.Series:
    """
    Pendenza della SMA(sma_window) del volume, normalizzata per la SMA stessa.
    Positivo = volume in crescita, negativo = in calo.
    """
    sma = volume.rolling(sma_window).mean()
    slope = _rolling_linear_slope(sma, window=sma_window)
    return slope / sma.replace(0.0, np.nan)


# ──────────────────────────────────────────────────────────────────────────────
# TREND
# ──────────────────────────────────────────────────────────────────────────────

def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Average Directional Index — misura la forza del trend (non la direzione).
    Usa Wilder's smoothing (EWM alpha=1/period, adjust=False).
    """
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    up   = high - high.shift(1)
    down = low.shift(1) - low

    plus_dm  = pd.Series(np.where((up > down) & (up > 0),   up,   0.0), index=close.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=close.index)

    alpha   = 1.0 / period
    atr_s   = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_s  = plus_dm.ewm(alpha=alpha, adjust=False).mean()
    minus_s = minus_dm.ewm(alpha=alpha, adjust=False).mean()

    safe_atr = atr_s.replace(0.0, np.nan)
    plus_di  = 100.0 * plus_s  / safe_atr
    minus_di = 100.0 * minus_s / safe_atr

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, adjust=False).mean()


def sma_slope(close: pd.Series, window: int = 50) -> pd.Series:
    """
    Variazione giornaliera della SMA(window), normalizzata per il prezzo.
    Cattura la direzione e la velocità del trend di medio periodo.
    """
    sma = close.rolling(window).mean()
    return sma.diff(1) / close.replace(0.0, np.nan)


# ──────────────────────────────────────────────────────────────────────────────
# MEAN REVERSION
# ──────────────────────────────────────────────────────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI con Wilder's smoothing. Range [0, 100].
    >70 = ipercomprato, <30 = ipervenduto.
    """
    delta = close.diff(1)
    alpha = 1.0 / period
    avg_gain = delta.clip(lower=0.0).ewm(alpha=alpha, adjust=False).mean()
    avg_loss = (-delta).clip(lower=0.0).ewm(alpha=alpha, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def dist_from_sma(close: pd.Series, window: int = 200) -> pd.Series:
    """
    Distanza del prezzo dalla SMA(window), normalizzata per il prezzo.
    Positivo = sopra la media (forza), negativo = sotto (debolezza).
    """
    sma = close.rolling(window).mean()
    return (close - sma) / close.replace(0.0, np.nan)


# ──────────────────────────────────────────────────────────────────────────────
# MOMENTUM
# ──────────────────────────────────────────────────────────────────────────────

def rate_of_change(close: pd.Series, period: int = 10) -> pd.Series:
    """Rate of change su `period` barre, espresso in percentuale."""
    return (close / close.shift(period) - 1.0) * 100.0


# ──────────────────────────────────────────────────────────────────────────────
# RANGE
# ──────────────────────────────────────────────────────────────────────────────

def normalized_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """
    ATR(period) normalizzato per il prezzo di chiusura.
    Misura la volatilità intradía indipendentemente dal livello di prezzo.
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    alpha = 1.0 / period
    atr14 = tr.ewm(alpha=alpha, adjust=False).mean()
    return atr14 / close.replace(0.0, np.nan)


# ──────────────────────────────────────────────────────────────────────────────
# STANDARDIZZAZIONE
# ──────────────────────────────────────────────────────────────────────────────

def rolling_zscore(series: pd.Series, window: int = ZSCORE_LOOKBACK) -> pd.Series:
    """
    Rolling z-score: ogni valore è standardizzato usando solo dati storici.
    min_periods = window//2 per ridurre i NaN nel periodo di warm-up iniziale.
    """
    mu    = series.rolling(window, min_periods=window // 2).mean()
    sigma = series.rolling(window, min_periods=window // 2).std()
    return (series - mu) / sigma.replace(0.0, np.nan)


# ──────────────────────────────────────────────────────────────────────────────
# UTILITY INTERNE
# ──────────────────────────────────────────────────────────────────────────────

def _rolling_linear_slope(series: pd.Series, window: int) -> pd.Series:
    """
    Pendenza della regressione lineare sulle ultime `window` osservazioni.
    X è centrato per ridurre la correlazione numerica tra slope e intercetta.
    """
    x = np.arange(window, dtype=float)
    x -= x.mean()
    ss_xx = float((x ** 2).sum())

    def _slope(y: np.ndarray) -> float:
        if np.isnan(y).any():
            return np.nan
        return float(np.dot(x, y - y.mean()) / ss_xx)

    return series.rolling(window).apply(_slope, raw=True)


# ──────────────────────────────────────────────────────────────────────────────
# CLASSE PRINCIPALE
# ──────────────────────────────────────────────────────────────────────────────

class FeatureEngineer:
    """
    Orchestrates the feature pipeline: OHLCV → 14 z-scored features for the HMM.
    All computations are causal (no look-ahead bias).
    """

    def __init__(self, zscore_window: int = ZSCORE_LOOKBACK) -> None:
        self.zscore_window = zscore_window

    def compute(self, ohlcv: pd.DataFrame) -> pd.DataFrame:
        """
        Calcola tutti i feature dall'OHLCV e li standardizza con rolling z-score.

        Args:
            ohlcv: DataFrame con colonne [open, high, low, close, volume],
                   indice datetime ordinato in modo crescente.

        Returns:
            DataFrame con FEATURE_COLUMNS (14 colonne), righe NaN rimosse.
            Pronto per essere passato a HMMEngine.fit() o predict_*.
        """
        close  = ohlcv["close"]
        high   = ohlcv["high"]
        low    = ohlcv["low"]
        volume = ohlcv["volume"]

        lr = log_returns(close, 1)

        raw = pd.DataFrame({
            "log_ret_1":       lr,
            "log_ret_5":       log_returns(close, 5),
            "log_ret_20":      log_returns(close, 20),
            "realized_vol_20": realized_vol(lr, 20),
            "vol_ratio":       vol_ratio(lr, fast=5, slow=20),
            "volume_zscore":   volume_zscore(volume, window=50),
            "volume_trend":    volume_trend(volume, sma_window=10),
            "adx_14":          adx(high, low, close, period=14),
            "sma50_slope":     sma_slope(close, window=50),
            "rsi_14":          rsi(close, period=14),
            "dist_sma200":     dist_from_sma(close, window=200),
            "roc_10":          rate_of_change(close, period=10),
            "roc_20":          rate_of_change(close, period=20),
            "atr_norm":        normalized_atr(high, low, close, period=14),
        }, index=ohlcv.index)

        standardized = raw.apply(
            lambda col: rolling_zscore(col, window=self.zscore_window)
        )
        standardized.columns = FEATURE_COLUMNS
        return standardized.dropna()
