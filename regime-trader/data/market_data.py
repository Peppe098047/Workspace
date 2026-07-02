"""
Market Data Feed — dati OHLCV storici e in tempo reale da Alpaca.

Gestione gap:
  - Weekend e festività: forward-fill dall'ultima barra disponibile
  - Trading halt intraday: barra replicata dall'ultima chiusura
  - Gap > 5 barre consecutive: loggato come warning (possibile dato mancante)

Cache incrementale: load_history() scarica tutto; update() aggiunge solo le barre
mancanti dall'ultima data in cache a oggi.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from broker.alpaca_client import AlpacaClient

logger = logging.getLogger("regime-trader.data")

_MAX_GAP_BARS = 5       # gap > 5 barre consecutive → warning
_OHLCV_COLS   = ["open", "high", "low", "close", "volume"]


class MarketDataFeed:
    """
    Fornisce dati OHLCV a tutti i componenti del sistema.

    Mantiene una cache per simbolo e scarica solo i dati mancanti
    rispetto all'ultimo fetch (update incrementale).
    """

    def __init__(self, client: AlpacaClient, timeframe: str = "1Day") -> None:
        self.client    = client
        self.timeframe = timeframe
        self._cache:   dict[str, pd.DataFrame] = {}
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_stop:   threading.Event = threading.Event()

    # ─── Fetch dati ───────────────────────────────────────────────────────────

    def load_history(
        self,
        symbols: list[str],
        lookback_bars: int,
    ) -> dict[str, pd.DataFrame]:
        """
        Scarica i dati storici per i simboli richiesti.
        Sovrascrive la cache esistente per quei simboli.
        """
        # Stima la data di inizio tenendo conto di weekend e festività (~1.4×)
        trading_days_factor = 1.4
        calendar_days = int(lookback_bars * trading_days_factor) + 10
        start = (date.today() - timedelta(days=calendar_days)).isoformat()

        raw = self.client.get_bars(
            symbols=symbols,
            timeframe=self.timeframe,
            start=start,
        )

        result: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            if sym not in raw or not raw[sym]:
                logger.warning("Nessun dato ricevuto per %s", sym)
                continue

            df = self._normalize_bars(raw[sym])
            df = self._fill_gaps(df)
            df = df.tail(lookback_bars)   # taglia al numero richiesto
            self._cache[sym] = df
            result[sym] = df
            logger.info("Caricato %s: %d barre (%s → %s)", sym, len(df), df.index[0].date(), df.index[-1].date())

        return result

    def update(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """
        Aggiorna la cache con le barre mancanti dall'ultima data nota.
        Se un simbolo non è in cache, esegue un load_history completo (252 barre).
        """
        result: dict[str, pd.DataFrame] = {}

        for sym in symbols:
            if sym not in self._cache:
                logger.info("%s non in cache — eseguo load_history(252)", sym)
                loaded = self.load_history([sym], lookback_bars=252)
                if sym in loaded:
                    result[sym] = loaded[sym]
                continue

            start, _ = self._bars_to_load(sym, lookback_bars=0)
            if not start:
                result[sym] = self._cache[sym]
                continue

            raw = self.client.get_bars(
                symbols=[sym],
                timeframe=self.timeframe,
                start=start,
            )
            if sym not in raw or not raw[sym]:
                result[sym] = self._cache[sym]
                continue

            new_bars = self._normalize_bars(raw[sym])
            combined = pd.concat([self._cache[sym], new_bars])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
            combined = self._fill_gaps(combined)
            self._cache[sym] = combined
            result[sym] = combined
            logger.info("Aggiornato %s: +%d barre → totale %d", sym, len(new_bars), len(combined))

        return result

    # ─── Lettura puntuale ─────────────────────────────────────────────────────

    def get(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._cache.get(symbol)

    def get_latest_price(self, symbol: str) -> Optional[float]:
        df = self._cache.get(symbol)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])

        # Fallback: chiama l'API per l'ultima barra
        try:
            bars = self.client.get_latest_bar([symbol])
            bar  = bars.get(symbol)
            if bar:
                return float(bar.close)
        except Exception as exc:
            logger.warning("get_latest_price(%s) fallito: %s", symbol, exc)
        return None

    def get_latest_bar(self, symbols: list[str]) -> dict[str, dict]:
        """Restituisce l'ultima barra per ciascun simbolo (REST, non WebSocket)."""
        raw = self.client.get_latest_bar(symbols)
        return {
            sym: {
                "open": float(bar.open), "high": float(bar.high),
                "low":  float(bar.low),  "close": float(bar.close),
                "volume": float(bar.volume),
                "timestamp": bar.timestamp.isoformat() if bar.timestamp else None,
            }
            for sym, bar in raw.items()
        }

    def get_latest_quote(self, symbols: list[str]) -> dict[str, dict]:
        """Restituisce bid/ask per ciascun simbolo (per verifica spread)."""
        raw = self.client.get_latest_quote(symbols)
        return {
            sym: {
                "bid_price": float(q.bid_price or 0),
                "ask_price": float(q.ask_price or 0),
                "bid_size":  float(q.bid_size or 0),
                "ask_size":  float(q.ask_size or 0),
                "spread":    float((q.ask_price or 0) - (q.bid_price or 0)),
                "spread_pct": (
                    float(((q.ask_price or 0) - (q.bid_price or 0)) / q.ask_price)
                    if q.ask_price else 0.0
                ),
            }
            for sym, q in raw.items()
        }

    def get_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        """Snapshot completo (barra + quote) per ciascun simbolo."""
        raw = self.client.get_snapshot(symbols)
        result: dict[str, dict] = {}
        for sym, snap in raw.items():
            result[sym] = {
                "latest_trade_price": float(snap.latest_trade.price) if snap.latest_trade else None,
                "latest_quote": {
                    "bid": float(snap.latest_quote.bid_price or 0) if snap.latest_quote else 0,
                    "ask": float(snap.latest_quote.ask_price or 0) if snap.latest_quote else 0,
                },
                "daily_bar": {
                    "open":   float(snap.daily_bar.open)  if snap.daily_bar else None,
                    "high":   float(snap.daily_bar.high)  if snap.daily_bar else None,
                    "low":    float(snap.daily_bar.low)   if snap.daily_bar else None,
                    "close":  float(snap.daily_bar.close) if snap.daily_bar else None,
                    "volume": float(snap.daily_bar.volume) if snap.daily_bar else None,
                },
            }
        return result

    # ─── WebSocket ────────────────────────────────────────────────────────────

    def subscribe_bars(
        self,
        symbols: list[str],
        callback: Callable[[str, dict], None],
    ) -> None:
        """
        Avvia una sottoscrizione WebSocket alle barre in tempo reale.
        callback(symbol, bar_dict) viene chiamata a ogni nuova barra.
        """
        self._stream_stop.clear()
        self._stream_thread = threading.Thread(
            target=self._run_bar_stream,
            args=(symbols, callback),
            daemon=True,
            name="alpaca-data-stream-bars",
        )
        self._stream_thread.start()
        logger.info("Sottoscrizione barre avviata per: %s", symbols)

    def subscribe_quotes(
        self,
        symbols: list[str],
        callback: Callable[[str, dict], None],
    ) -> None:
        """
        Avvia una sottoscrizione WebSocket alle quote in tempo reale.
        callback(symbol, quote_dict) viene chiamata a ogni nuova quote.
        Utile per verificare lo spread prima dell'esecuzione.
        """
        self._stream_stop.clear()
        self._stream_thread = threading.Thread(
            target=self._run_quote_stream,
            args=(symbols, callback),
            daemon=True,
            name="alpaca-data-stream-quotes",
        )
        self._stream_thread.start()
        logger.info("Sottoscrizione quote avviata per: %s", symbols)

    def stop_stream(self) -> None:
        """Ferma il WebSocket attivo."""
        self._stream_stop.set()
        if self._stream_thread:
            self._stream_thread.join(timeout=5)
        logger.info("Data stream fermato.")

    def _run_bar_stream(
        self,
        symbols: list[str],
        callback: Callable[[str, dict], None],
    ) -> None:
        from alpaca.data.live import StockDataStream

        stream = StockDataStream(
            api_key    = self.client._api_key,
            secret_key = self.client._secret_key,
        )

        async def _on_bar(bar) -> None:
            try:
                sym = bar.symbol
                bar_dict = {
                    "open":      float(bar.open),
                    "high":      float(bar.high),
                    "low":       float(bar.low),
                    "close":     float(bar.close),
                    "volume":    float(bar.volume),
                    "timestamp": bar.timestamp.isoformat() if bar.timestamp else None,
                }
                # Aggiorna la cache in-memory SOLO se la granularità coincide:
                # lo stream invia barre da 1 MINUTO; mischiarle in una cache
                # oraria/daily corrompe ATR/EMA. Inoltre pd.concat crea un nuovo
                # DataFrame: chi tiene un riferimento alla cache resta stantio
                # — i consumer devono ricaricare via update()/get().
                if sym in self._cache and self.timeframe == "1Min":
                    ts  = bar.timestamp or datetime.now()
                    row = pd.DataFrame([bar_dict[c] for c in _OHLCV_COLS],
                                       index=_OHLCV_COLS, columns=[ts]).T
                    row.index = pd.DatetimeIndex([ts])
                    self._cache[sym] = pd.concat([self._cache[sym], row])
                    self._cache[sym] = self._cache[sym][~self._cache[sym].index.duplicated(keep="last")]

                callback(sym, bar_dict)
            except Exception as exc:
                logger.error("Errore handler bar stream: %s", exc)

        stream.subscribe_bars(_on_bar, *symbols)

        async def _run():
            task = asyncio.create_task(stream._run_forever())
            while not self._stream_stop.is_set():
                await asyncio.sleep(1)
            task.cancel()

        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.error("Bar stream interrotto: %s", exc)

    def _run_quote_stream(
        self,
        symbols: list[str],
        callback: Callable[[str, dict], None],
    ) -> None:
        from alpaca.data.live import StockDataStream

        stream = StockDataStream(
            api_key    = self.client._api_key,
            secret_key = self.client._secret_key,
        )

        async def _on_quote(quote) -> None:
            try:
                sym = quote.symbol
                quote_dict = {
                    "bid_price":  float(quote.bid_price or 0),
                    "ask_price":  float(quote.ask_price or 0),
                    "bid_size":   float(quote.bid_size or 0),
                    "ask_size":   float(quote.ask_size or 0),
                    "spread":     float((quote.ask_price or 0) - (quote.bid_price or 0)),
                    "timestamp":  quote.timestamp.isoformat() if quote.timestamp else None,
                }
                callback(sym, quote_dict)
            except Exception as exc:
                logger.error("Errore handler quote stream: %s", exc)

        stream.subscribe_quotes(_on_quote, *symbols)

        async def _run():
            task = asyncio.create_task(stream._run_forever())
            while not self._stream_stop.is_set():
                await asyncio.sleep(1)
            task.cancel()

        try:
            asyncio.run(_run())
        except Exception as exc:
            logger.error("Quote stream interrotto: %s", exc)

    # ─── Utilità interne ──────────────────────────────────────────────────────

    def _normalize_bars(self, raw_bars) -> pd.DataFrame:
        """
        Converte la risposta API Alpaca in un DataFrame con schema standard:
        index=DatetimeIndex, colonne=[open, high, low, close, volume].
        """
        rows = []
        timestamps = []
        for bar in raw_bars:
            rows.append({
                "open":   float(bar.open),
                "high":   float(bar.high),
                "low":    float(bar.low),
                "close":  float(bar.close),
                "volume": float(bar.volume),
            })
            timestamps.append(bar.timestamp)

        if not rows:
            return pd.DataFrame(columns=_OHLCV_COLS)

        df = pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps))
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
        # Per barre giornaliere azzera l'ora (Alpaca le timestampa alle 04:00 UTC):
        # senza questo il reindex su bdate_range (mezzanotte) non combacia → tutto NaN.
        if self.timeframe == "1Day":
            df.index = df.index.normalize()
        df.index.name = "timestamp"
        return df[_OHLCV_COLS]

    def _fill_gaps(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Gestisce i gap nei dati (weekend, festività, trading halt):
        - Reindex sull'asse temporale con frequenza "business day"
        - Forward-fill per i valori OHLC mancanti
        - Volume = 0 per le barre sintetiche (segnala che non ci sono stati scambi)

        Solo per barre GIORNALIERE: per i timeframe intraday i gap notturni e di
        weekend sono normali e il reindex su giorni lavorativi distruggerebbe i dati.
        """
        if df.empty or self.timeframe != "1Day":
            return df

        full_idx = pd.bdate_range(start=df.index.min(), end=df.index.max())
        df = df.reindex(full_idx)

        # Controlla gap lunghi prima del fill
        missing_mask = df["close"].isna()
        if missing_mask.any():
            gap_lengths = (missing_mask.astype(int)
                          .groupby((~missing_mask).astype(int).cumsum())
                          .sum())
            max_gap = gap_lengths.max()
            if max_gap > _MAX_GAP_BARS:
                logger.warning(
                    "Gap di %d barre consecutive rilevato nei dati — possibile sospensione o errore",
                    max_gap,
                )

        # OHLC forward-fill; volume = 0 sulle barre sintetiche
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].ffill()
        df["volume"] = df["volume"].fillna(0.0)
        return df.dropna(subset=["close"])

    def _bars_to_load(
        self,
        symbol: str,
        lookback_bars: int,
    ) -> tuple[str, Optional[str]]:
        """
        Calcola start/end per un fetch incrementale.
        Se in cache: daily → parte dal giorno successivo all'ultima barra;
                     intraday → riparte dall'ultima barra stessa (il "+1 giorno"
                     chiederebbe dati dal FUTURO → Alpaca "end before start").
                     L'eventuale barra duplicata è gestita dal dedup in update().
        Se non in cache: usa lookback_bars per stimare la start date.
        """
        cached = self._cache.get(symbol)
        if cached is not None and not cached.empty:
            last_ts = cached.index[-1]
            if self.timeframe == "1Day":
                start_dt = last_ts + timedelta(days=1)
                return start_dt.strftime("%Y-%m-%d"), None
            # Intraday: timestamp completo dell'ultima barra (naive = UTC)
            return last_ts.isoformat(), None

        # Nessuna cache: calcola start in base al lookback
        calendar_days = int(lookback_bars * 1.4) + 10
        start = (date.today() - timedelta(days=calendar_days)).isoformat()
        return start, None

    def clear_cache(self, symbol: Optional[str] = None) -> None:
        if symbol:
            self._cache.pop(symbol, None)
        else:
            self._cache.clear()
