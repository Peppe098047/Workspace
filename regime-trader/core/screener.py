"""
Liquidity Screener — seleziona i titoli più liquidi da un universo ampio.

PERCHÉ LA LIQUIDITÀ: per un bot intraday l'unico costo reale (Alpaca è
commission-free) è lo SPREAD bid/ask. Lo spread è inversamente proporzionale
alla liquidità: un titolo che scambia $1 miliardo al giorno ha spread ~0,01%,
uno che scambia $50M può superare lo 0,1%. Dal backtest del 2026-06-09:
con spread 0,05% l'intraday orario è già in PERDITA netta. Selezionare solo
titoli molto liquidi non è un'ottimizzazione, è una condizione di sopravvivenza.

Metrica usata: DOLLAR VOLUME medio (prezzo × volume) sugli ultimi N giorni.
È più significativo del volume puro: 10M di azioni a $5 valgono meno
di 2M di azioni a $500.

Lo screening avviene ALL'AVVIO della sessione (il bot intraday riparte ogni
giorno): cambiare l'universo a metà sessione richiederebbe la rinegoziazione
delle sottoscrizioni WebSocket e non vale la complessità.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger("regime-trader.screener")


@dataclass
class ScreenedSymbol:
    """Risultato dello screening per un singolo titolo."""
    symbol: str
    avg_dollar_volume: float   # media (close × volume) sugli ultimi N giorni
    last_price: float
    n_days: int                # giorni di dati effettivamente disponibili


class LiquidityScreener:
    """
    Filtra un universo ampio e restituisce i top-N titoli per dollar volume.

    Config (sezione `screener` di settings.yaml):
      enabled:            attiva/disattiva lo screening (default false)
      universe:           lista ampia di ticker candidati
      top_n:              quanti titoli selezionare (default 10)
      lookback_days:      giorni di storia daily per le medie (default 20)
      min_price:          prezzo minimo (default 10$ — sotto, gli spread % esplodono)
      min_dollar_volume:  dollar volume medio minimo (default $200M/giorno)
      always_include:     ticker sempre inclusi (default [SPY] — termometro HMM)
    """

    def __init__(self, config: dict) -> None:
        cfg = config.get("screener", {}) or {}
        self.enabled           = bool(cfg.get("enabled", False))
        self.universe          = list(cfg.get("universe", []) or [])
        self.top_n             = int(cfg.get("top_n", 10))
        self.lookback_days     = int(cfg.get("lookback_days", 20))
        self.min_price         = float(cfg.get("min_price", 10.0))
        self.min_dollar_volume = float(cfg.get("min_dollar_volume", 200_000_000))
        self.always_include    = list(cfg.get("always_include", ["SPY"]))

    # ──────────────────────────────────────────────────────────────────────
    # API principale
    # ──────────────────────────────────────────────────────────────────────

    def select_symbols(self, client, fallback: list[str]) -> list[str]:
        """
        Ritorna la lista operativa dei simboli per la sessione.

        Il primo simbolo è SEMPRE il primo di `always_include` (SPY): è il
        riferimento per l'HMM e per la relative strength del ranking.
        In caso di errore o screener disattivato ritorna `fallback` invariato.
        """
        if not self.enabled or not self.universe:
            return fallback

        try:
            results = self.screen(client)
        except Exception as exc:
            logger.error("Screener fallito (%s): uso la lista simboli di fallback.", exc)
            return fallback

        if not results:
            logger.warning("Screener: nessun titolo supera i filtri — uso il fallback.")
            return fallback

        # I titoli "always include" stanno in testa, senza duplicati
        chosen: list[str] = list(self.always_include)
        for r in results:
            if r.symbol not in chosen:
                chosen.append(r.symbol)
            if len(chosen) >= max(self.top_n, len(self.always_include)):
                break

        logger.info(
            "Screener: %d/%d titoli selezionati: %s",
            len(chosen), len(self.universe), ", ".join(chosen),
        )
        return chosen

    def screen(self, client) -> list[ScreenedSymbol]:
        """
        Scarica le barre daily dell'universo e ordina per dollar volume medio.

        Ritorna solo i titoli che superano i filtri (prezzo minimo e
        dollar volume minimo), ordinati dal più liquido al meno liquido.
        """
        # Finestra daily: lookback + margine per weekend/festivi
        start = (datetime.now() - timedelta(days=self.lookback_days * 2 + 10)).strftime("%Y-%m-%d")
        bars_by_symbol = client.get_bars(self.universe, "1Day", start=start)

        results: list[ScreenedSymbol] = []
        for sym in self.universe:
            bars = bars_by_symbol.get(sym) or []
            metric = self._evaluate(sym, bars)
            if metric is not None:
                results.append(metric)

        results.sort(key=lambda r: r.avg_dollar_volume, reverse=True)
        return results

    # ──────────────────────────────────────────────────────────────────────
    # Helper interni
    # ──────────────────────────────────────────────────────────────────────

    def _evaluate(self, symbol: str, bars: list) -> ScreenedSymbol | None:
        """Calcola il dollar volume medio e applica i filtri. None = scartato."""
        recent = bars[-self.lookback_days:]
        if len(recent) < max(5, self.lookback_days // 2):
            logger.debug("Screener: %s scartato (solo %d giorni di dati).", symbol, len(recent))
            return None

        closes  = [float(getattr(b, "close", 0.0) or 0.0) for b in recent]
        volumes = [float(getattr(b, "volume", 0.0) or 0.0) for b in recent]
        dollar_volumes = [c * v for c, v in zip(closes, volumes)]

        last_price = closes[-1]
        avg_dv     = sum(dollar_volumes) / len(dollar_volumes)

        if last_price < self.min_price:
            logger.debug("Screener: %s scartato (prezzo %.2f < %.2f).", symbol, last_price, self.min_price)
            return None
        if avg_dv < self.min_dollar_volume:
            logger.debug(
                "Screener: %s scartato (dollar volume $%.0fM < $%.0fM).",
                symbol, avg_dv / 1e6, self.min_dollar_volume / 1e6,
            )
            return None

        return ScreenedSymbol(
            symbol=symbol,
            avg_dollar_volume=avg_dv,
            last_price=last_price,
            n_days=len(recent),
        )
