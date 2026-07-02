"""
main.py — entry point del regime-trader.

Modalità di esecuzione:
  python main.py trade                          # live/paper trading
  python main.py trade --dry-run               # pipeline completa, nessun ordine
  python main.py trade --train-only            # addestra HMM ed esce
  python main.py backtest --symbols SPY --start 2019-01-01 --end 2024-12-31
  python main.py backtest --compare --stress-test
  python main.py stress   --symbols SPY --start 2020-01-01 --end 2024-12-31
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import signal as signal_module
import sys
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

from data.feature_engineering import FeatureEngineer
from backtest.backtester import WalkForwardBacktester
from backtest.performance import PerformanceAnalyzer
from backtest.stress_test import StressTester

logger = logging.getLogger("regime-trader")

_MODEL_PATH    = Path("models/hmm_model.pkl")
_SNAPSHOT_PATH = Path("state_snapshot.json")
_MODEL_MAX_AGE = timedelta(days=7)


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG E LOGGING
# ──────────────────────────────────────────────────────────────────────────────

def load_config(config_path: Path = Path("config/settings.yaml")) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


# ──────────────────────────────────────────────────────────────────────────────
# TRADING SESSION
# ──────────────────────────────────────────────────────────────────────────────

class TradingSession:
    """
    Incapsula tutto lo stato e la logica del loop di trading live.

    Flusso:
      start()
        ├─ _startup()        connette tutto, allena HMM, recovery snapshot
        ├─ _run_main_loop()  event loop sulle barre WebSocket
        └─ _shutdown_seq()   salva stato, chiude connessioni
    """

    def __init__(self, config: dict, args: argparse.Namespace) -> None:
        self.config    = config
        self.args      = args
        self.dry_run   = getattr(args, "dry_run", False)
        self.symbols   = config.get("broker", {}).get("symbols", ["SPY"])
        self.timeframe = config.get("broker", {}).get("timeframe", "1Day")
        # Intraday: chiude tutto a fine giornata (flat overnight, zero gap risk)
        self.intraday  = bool(config.get("broker", {}).get("intraday", False))
        self.square_off_minutes = int(config.get("broker", {}).get("square_off_minutes", 15))

        # Componenti (inizializzati in _startup)
        self.alpaca     = None
        self.data_feed  = None
        self.hmm        = None
        self.orchestrator = None
        self.risk_mgr   = None
        self.tracker    = None
        self.executor   = None
        self.ranker     = None
        self.calendar   = None
        self.fe         = FeatureEngineer()

        # Stato live
        self._bars_cache:  dict[str, pd.DataFrame] = {}
        self._bar_queue:   queue.Queue = queue.Queue()
        self._shutdown_ev: threading.Event = threading.Event()

        self._session_start     = datetime.now()
        self._peak_equity       = 0.0
        self._start_equity      = 0.0
        self._daily_start_equity = 0.0
        self._weekly_start_equity = 0.0
        self._last_pipeline_ts: Optional[datetime] = None  # evita doppi run per la stessa barra
        self._last_retrain_date: Optional[date] = None
        self._last_processed_date: Optional[date] = None
        self._trade_log: list[dict] = []
        self._dash_data: dict = {}        # dati live per la dashboard a refresh
        self._recent_signals: list[dict] = []  # ultimi segnali per il pannello dashboard
        self._events: list[dict] = []     # registro eventi (apertura/chiusura/stop) per la dashboard
        self.alerts = None                # AlertManager (inizializzato in _startup)
        self._last_regime: Optional[str] = None  # per rilevare i cambi di regime
        self._last_regime_state = None    # ultimo RegimeState (per la manutenzione stop)
        self._last_ranking_dropped: list[str] = []  # esclusi dal ranking nell'ultimo ciclo
        self._regime_changes: list[dict] = []  # storico cambi di regime per il report finale
        self._log_capture = None          # cattura WARNING+ per il report di sessione
        self._last_pipeline_hour: Optional[datetime] = None  # gate orario sulle barre-minuto
        self._reentry_block: dict[str, datetime] = {}  # cooldown anti-churn post-vendita
        self._clean_shutdown = False      # True solo su spegnimento volontario (non crash)
        self._squared_off_date: Optional[date] = None  # giorno in cui si è già fatto lo square-off

    # ─── Avvio ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        try:
            self._startup()
            if getattr(self.args, "train_only", False):
                logger.info("--train-only: modello salvato, uscita.")
                return
            self._install_signal_handlers()
            self._run_main_loop()
            # Uscita normale dal loop (shutdown event) = spegnimento volontario
            self._clean_shutdown = True
        except KeyboardInterrupt:
            logger.info("Interruzione utente (Ctrl+C).")
            self._clean_shutdown = True
        except Exception as exc:
            logger.critical("Errore non gestito: %s", exc, exc_info=True)
            self._alert(f"ERRORE CRITICO: {exc}")
            # Crash: NON liquidare (le posizioni restano protette dagli stop sul broker)
            self._clean_shutdown = False
        finally:
            self._shutdown_seq()

    def _startup(self) -> None:
        import os
        import socket as _socket
        from broker.alpaca_client import AlpacaClient
        from broker.order_executor import OrderExecutor
        from broker.position_tracker import PositionTracker
        from core.risk_manager import RiskManager
        from data.market_data import MarketDataFeed
        from monitoring.logger import setup_logging
        from monitoring.alerts import AlertManager

        # Timeout globale sui socket bloccanti: senza, una chiamata REST/urllib
        # sincrona può restare appesa all'infinito su un socket semi-aperto quando
        # la rete si degrada, congelando il main loop (HANG del 2026-06-15, quando
        # cadde anche il keepalive del websocket). Rete di sicurezza che copre
        # Alpaca REST (requests) e urllib (calendario/orologio). NON tocca il
        # websocket dello stream: usa socket asyncio non-bloccanti.
        net_timeout = float(self.config.get("broker", {}).get("network_timeout_seconds", 30))
        if net_timeout > 0:
            _socket.setdefaulttimeout(net_timeout)

        # Logging JSON strutturato su file (main/trades/alerts/regime.log) + console
        setup_logging(level=logging.DEBUG if getattr(self.args, "verbose", False) else logging.INFO)

        # Cattura WARNING+ in memoria per il report di fine sessione
        from monitoring.session_report import SessionLogCapture
        self._log_capture = SessionLogCapture()
        self._log_capture.install()

        # Alert manager: webhook/email/Telegram opzionali da .env
        self.alerts = AlertManager(
            webhook_url=os.environ.get("ALERT_WEBHOOK_URL"),
            email_to=os.environ.get("ALERT_EMAIL"),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            rate_limit_minutes=int(self.config.get("monitoring", {}).get("alert_rate_limit_minutes", 15)),
        )
        if self.alerts.telegram_token and self.alerts.telegram_chat_id:
            logger.info("Notifiche Telegram attive.")

        logger.info("=== REGIME TRADER — AVVIO ===")
        mode = "DRY-RUN" if self.dry_run else ("PAPER" if self.config.get("broker", {}).get("paper_trading", True) else "LIVE")
        logger.info("Modalità: %s", mode)

        # 1. Connessione Alpaca
        self.alpaca = AlpacaClient.from_env()
        account = self.alpaca.get_account()
        self._start_equity        = account["equity"]
        self._peak_equity         = account["equity"]
        self._daily_start_equity  = account["equity"]
        self._weekly_start_equity = account["equity"]
        logger.info("Account: equity=$%.2f | buying_power=$%.2f", account["equity"], account["buying_power"])

        # 2. Verifica ore di mercato (saltata in dry-run e train-only)
        if not self.dry_run and not getattr(self.args, "train_only", False):
            clock = self.alpaca.get_clock()
            if not clock["is_open"]:
                logger.warning(
                    "Mercato chiuso. Prossima apertura: %s",
                    clock.get("next_open", "sconosciuto"),
                )
                if not getattr(self.args, "wait_for_open", False):
                    logger.info("Usa --wait-for-open per attendere l'apertura. Uscita.")
                    sys.exit(0)
                self._wait_for_market_open(clock)

        # 2.5 Screener di liquidità: seleziona dall'universo ampio i titoli più
        # liquidi (spread più bassi = unico costo reale dell'intraday).
        # Gira solo all'avvio: il bot intraday riparte comunque ogni giorno.
        from core.screener import LiquidityScreener
        screener = LiquidityScreener(self.config)
        if screener.enabled:
            self.symbols = screener.select_symbols(self.alpaca, fallback=self.symbols)
            logger.info("Simboli operativi dopo screening: %s", ", ".join(self.symbols))

        # 2.55 Calendario macro: blackout ingressi attorno a CPI/NFP/FOMC
        from data.economic_calendar import EconomicCalendar
        self.calendar = EconomicCalendar(self.config)
        if self.calendar.enabled:
            self.calendar.load()
            nxt = self.calendar.next_event()
            if nxt:
                logger.info("Prossimo evento macro rilevante: %s", nxt)

        # 2.6 Ranking momentum: in pipeline sceglie QUALI titoli comprare
        from core.ranking import MomentumRanker
        self.ranker = MomentumRanker(self.config)
        if self.ranker.enabled:
            logger.info(
                "Ranking momentum attivo: top-%d su %d barre (benchmark %s).",
                self.ranker.top_k, self.ranker.lookback_bars, self.ranker.benchmark,
            )

        # 3. Dati storici e HMM
        self.data_feed = MarketDataFeed(self.alpaca, self.timeframe)
        logger.info("Download storico per %d simboli…", len(self.symbols))
        # 1000 barre → ~748 feature dopo il warm-up 252 dello z-score (> MIN_TRAIN_BARS=504)
        self._bars_cache = self.data_feed.load_history(self.symbols, lookback_bars=1000)
        self.hmm = self._load_or_train_hmm()

        # 4. Risk manager
        self.risk_mgr = RiskManager(
            config=self.config.get("risk", {}),
            initial_capital=account["equity"],
        )
        self.risk_mgr.circuit_breaker.initialize(account["equity"], date.today())

        # 5. Position tracker
        self.tracker = PositionTracker(self.alpaca)
        self.tracker.sync()
        self.tracker.on_fill(self._on_tracker_fill_event)
        logger.info("Posizioni sincronizzate: %d aperte", len(self.tracker.get_all_positions()))

        # 6. Order executor
        self.executor = OrderExecutor(
            client=self.alpaca,
            on_fill=self._on_fill,
        )

        # 7. Strategy orchestrator
        from core.regime_strategies import StrategyOrchestrator
        self.orchestrator = StrategyOrchestrator(
            config=self.config.get("strategy", {}),
            regime_infos=self.hmm._regime_info,
        )

        # 8. Recovery da snapshot precedente
        self._load_snapshot()

        # 9. WebSocket data feed
        if not self.dry_run:
            self.data_feed.subscribe_bars(self.symbols, self._on_bar)
            self.tracker.start_stream()
        else:
            logger.info("DRY-RUN: WebSocket disabilitato, uso polling.")

        self._print_system_state(account)
        logger.info("Sistema online — in attesa barre.")

    # ─── Signal handlers OS ───────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        def _handle(signum, frame):
            logger.info("Segnale %s ricevuto — arresto ordinato…", signum)
            self._clean_shutdown = True   # spegnimento volontario → chiudi posizioni
            self._shutdown_ev.set()

        signal_module.signal(signal_module.SIGINT,  _handle)
        signal_module.signal(signal_module.SIGTERM, _handle)

    # ─── Main loop ────────────────────────────────────────────────────────────

    def _run_main_loop(self) -> None:
        poll_interval = 60 if self.timeframe == "1Day" else 300
        # Primo polling immediato (dry-run): il primo ciclo parte subito dopo
        # l'avvio, non dopo 5 minuti — utile per i collaudi brevi.
        last_poll = time.monotonic() - poll_interval

        # Manutenzione stop: tra una barra e l'altra aggiorna i trailing stop
        # coi prezzi correnti (il pipeline completo gira solo a barra chiusa).
        maint_minutes  = float(self.config.get("broker", {}).get("stop_refresh_minutes", 5))
        maint_interval = max(60.0, maint_minutes * 60.0)
        last_maint     = time.monotonic()

        while not self._shutdown_ev.is_set():
            # Modalità DRY-RUN: polling periodico invece di WebSocket
            if self.dry_run:
                now = time.monotonic()
                if now - last_poll >= poll_interval:
                    self._poll_new_bars()
                    last_poll = now
                time.sleep(5)
                continue

            # Controllo square-off serale (chiude tutto prima della chiusura mercato)
            self._check_eod_closeout()

            # Manutenzione periodica degli stop, anche senza nuove barre
            if time.monotonic() - last_maint >= maint_interval:
                self._run_stop_maintenance()
                last_maint = time.monotonic()

            # Modalità live: aspetta barra dal WebSocket
            try:
                sym, bar = self._bar_queue.get(timeout=min(120.0, maint_interval))
            except queue.Empty:
                # Timeout → controlla square-off e fai polling per barre perse
                self._check_eod_closeout()
                self._poll_new_bars()
                continue

            self._handle_bar_event(sym, bar)

    def _handle_bar_event(self, sym: str, bar: dict) -> None:
        """
        Processa un evento barra dal WebSocket.

        ATTENZIONE: lo stream Alpaca invia barre da 1 MINUTO, ma le decisioni
        vanno prese sulla barra del TIMEFRAME configurato (oraria). La pipeline
        scatta quindi solo quando il timestamp entra in una nuova ora — cioè
        quando la barra oraria precedente è completa. Senza questo gate la
        pipeline girava 60 volte l'ora (churn di ordini, visto il 2026-06-10).
        """
        ts_raw = bar.get("timestamp")
        if isinstance(ts_raw, datetime):
            bar_ts = ts_raw
        else:
            try:
                bar_ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now()
            except (ValueError, TypeError):
                bar_ts = datetime.now()

        # Lo stream Alpaca invia timestamp timezone-aware (UTC), ma il resto del bot
        # usa datetime NAIVE in ora locale (datetime.now(), incluso il polling e i
        # fallback qui sopra). Uniformiamo bar_ts a naive locale: confrontare un
        # datetime naive con uno aware solleva TypeError e fa cadere il bot
        # (crash del 2026-06-15, riga _last_pipeline_ts <= bar_ts).
        if bar_ts.tzinfo is not None:
            bar_ts = bar_ts.astimezone().replace(tzinfo=None)

        # Deduplicazione: stessa barra già processata?
        if self._last_pipeline_ts and bar_ts <= self._last_pipeline_ts:
            return
        self._last_pipeline_ts = bar_ts

        # Gate sul timeframe: pipeline solo alla prima barra-minuto di una nuova ora
        if self.timeframe != "1Min":
            bar_hour = bar_ts.replace(minute=0, second=0, microsecond=0)
            if self._last_pipeline_hour is not None and bar_hour <= self._last_pipeline_hour:
                return
            self._last_pipeline_hour = bar_hour

        bar_date = bar_ts.date()

        # Reset giornaliero contatori
        if self._last_processed_date != bar_date:
            self._on_new_day(bar_date)
        self._last_processed_date = bar_date

        # Cache aggiornata via REST con le barre orarie complete: lo stream
        # minuto NON va mischiato nella cache oraria (corromperebbe ATR/EMA)
        # e pd.concat ricreerebbe i DataFrame lasciando questa copia stantia.
        try:
            self._bars_cache = self.data_feed.update(self.symbols)
        except Exception as exc:
            logger.warning("Aggiornamento cache pre-pipeline fallito: %s — uso dati correnti.", exc)

        try:
            self._process_all_symbols(bar_ts)
        except Exception as exc:
            logger.error("Errore nel processare barra %s: %s", bar_ts, exc, exc_info=True)
            self._alert(f"Errore pipeline per {bar_ts}: {exc}")

    def _poll_new_bars(self) -> None:
        """
        Aggiornamento manuale della cache (fallback WebSocket down o dry-run).

        La pipeline scatta al PRIMO polling del giorno, a qualsiasi ora: il
        vecchio vincolo `hour >= 16` era un residuo della modalità daily e
        rendeva inutili i dry-run avviati all'apertura del mercato (15:30 IT).
        """
        try:
            self.data_feed.update(self.symbols)
            now = datetime.now()
            bar_date = now.date()
            if self._last_processed_date != bar_date:
                self._on_new_day(bar_date)
                self._last_processed_date = bar_date
                self._last_pipeline_ts = now
                self._process_all_symbols(now)
        except Exception as exc:
            logger.warning("Errore polling barre: %s", exc)

    def _on_bar(self, sym: str, bar: dict) -> None:
        """Callback WebSocket: riceve una nuova barra e la mette in coda."""
        self._bar_queue.put((sym, bar))

    def _on_new_day(self, new_date: date) -> None:
        """Operazioni di inizio giornata."""
        logger.info("=== Nuovo giorno di trading: %s ===", new_date)
        try:
            account = self.alpaca.get_account()
            equity  = account["equity"]
            self.risk_mgr.circuit_breaker.reset_daily(equity)
            self._daily_start_equity = equity
            if new_date.weekday() == 0:  # lunedì
                self.risk_mgr.circuit_breaker.reset_weekly(equity)
                self._weekly_start_equity = equity
        except Exception as exc:
            logger.warning("Errore reset giornaliero: %s", exc)

    def _check_eod_closeout(self) -> bool:
        """
        Square-off serale: se mancano <= square_off_minutes alla chiusura del
        mercato, chiude TUTTE le posizioni (flat overnight, zero gap risk).

        Returns:
            True se lo square-off è stato eseguito (o già fatto oggi), così il
            chiamante sa che non deve aprire nuove posizioni.
        """
        if not self.intraday or self.dry_run:
            return False

        today = date.today()
        if self._squared_off_date == today:
            return True  # già flat per oggi

        try:
            clock = self.alpaca.get_clock()
        except Exception as exc:
            logger.warning("Impossibile leggere l'orologio del mercato: %s", exc)
            return False

        if not clock.get("is_open") or not clock.get("next_close"):
            return False

        try:
            next_close = datetime.fromisoformat(clock["next_close"].replace("Z", "+00:00"))
            mins_to_close = (next_close - datetime.now(next_close.tzinfo)).total_seconds() / 60
        except Exception:
            return False

        if mins_to_close > self.square_off_minutes:
            return False

        # È ora di chiudere tutto
        positions = self.tracker.get_all_positions()
        if positions:
            logger.info(
                "SQUARE-OFF: chiusura di %d posizioni a %.0f min dalla chiusura mercato.",
                len(positions), mins_to_close,
            )
            try:
                results = self.executor.close_all_positions()
                logger.info("Square-off completato: %d ordini di chiusura inviati.", len(results))
                from monitoring.logger import log_trade
                log_trade("SQUARE-OFF serale", n_positions=len(positions), mins_to_close=round(mins_to_close, 1))
                for sym in positions:
                    self._log_event("CLOSE", sym, "square-off serale")
            except Exception as exc:
                logger.error("Errore durante lo square-off: %s", exc)
                if self.alerts:
                    self.alerts.api_lost(f"Square-off fallito: {exc}")
                return False
        else:
            logger.info("SQUARE-OFF: nessuna posizione da chiudere.")

        self._squared_off_date = today
        return True

    # ─── Pipeline principale ──────────────────────────────────────────────────

    def _process_all_symbols(self, ts: datetime) -> None:
        """
        Esegue la pipeline completa su tutti i simboli per la barra corrente.
        Ordine: HMM → strategia → risk → esecuzione → trailing stop → dashboard.
        """
        logger.debug("Pipeline avviata per %s", ts)

        # ── 1. Feature per il simbolo primario (per HMM) ──────────────────
        primary = self.symbols[0]
        if primary not in self._bars_cache or len(self._bars_cache[primary]) < 60:
            logger.warning("Dati insufficienti per %s, pipeline saltata.", primary)
            return

        if not self.dry_run and self.tracker:
            try:
                self.tracker.sync()
            except Exception as exc:
                logger.warning("Sync posizioni Alpaca fallito: %s — uso stato locale.", exc)

        try:
            features = self.fe.compute(self._bars_cache[primary])
        except Exception as exc:
            logger.error("Errore FeatureEngineer: %s — mantengo regime corrente.", exc)
            return

        # ── 2–4. HMM: predizione filtrata + stabilità ─────────────────────
        try:
            regime_state = self.hmm.predict_current_regime(features)
        except Exception as exc:
            logger.error("Errore HMM: %s — mantengo regime precedente.", exc)
            return

        # Memorizza lo stato regime per il ciclo di manutenzione stop
        self._last_regime_state = regime_state

        # ── 5. Flicker rate ────────────────────────────────────────────────
        is_flickering = self.hmm.is_flickering()

        # ── 5b. Logging strutturato + alert su cambio regime / flicker ─────
        from monitoring.logger import log_regime
        if self._last_regime and self._last_regime != regime_state.label:
            log_regime(
                f"Cambio regime {self._last_regime} → {regime_state.label}",
                old=self._last_regime, new=regime_state.label,
                probability=regime_state.probability,
            )
            if self.alerts:
                self.alerts.regime_change(self._last_regime, regime_state.label, regime_state.probability)
            self._regime_changes.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "old":  self._last_regime,
                "new":  regime_state.label,
                "probability": regime_state.probability,
            })
        self._last_regime = regime_state.label

        if is_flickering and self.alerts:
            self.alerts.flicker_exceeded(
                self.hmm.get_regime_flicker_rate(),
                self.config.get("hmm", {}).get("flicker_threshold", 4),
            )

        # ── 6. Aggiorna tracker con regime corrente ────────────────────────
        self.tracker.update_current_regime(regime_state.label)
        self.tracker.increment_holding_bars()

        # ── 7. StrategyOrchestrator: segnali per tutti i simboli ──────────
        try:
            signals = self.orchestrator.generate_signals(
                symbols      = self.symbols,
                bars         = self._bars_cache,
                regime_state = regime_state,
                is_flickering = is_flickering,
            )
        except Exception as exc:
            logger.error("Errore StrategyOrchestrator: %s", exc)
            signals = []

        # ── 7b. Ranking momentum: solo i top-K candidati possono ricevere BUY.
        # I titoli già in posizione restano selezionati (l'uscita è dei trailing
        # stop e dello square-off, non del ranking → evita churn e costi spread).
        if self.ranker and self.ranker.enabled and signals:
            held = set(self.tracker.get_all_positions().keys()) if self.tracker else set()
            selected = self.ranker.select(self.symbols, self._bars_cache, held=held)
            dropped = sorted(s.symbol for s in signals if s.symbol not in selected)
            if dropped:
                signals = [s for s in signals if s.symbol in selected]
                # Evento dashboard solo quando la lista degli esclusi cambia
                if dropped != self._last_ranking_dropped:
                    self._log_event("SKIP", ", ".join(dropped), "fuori dal ranking momentum top-K")
            self._last_ranking_dropped = dropped

        if not signals:
            logger.debug("Nessun segnale generato per %s.", ts)
        else:
            logger.info(
                "Regime=%s conf=%.1f%% | %d segnali | flicker=%s",
                regime_state.label, regime_state.probability * 100,
                len(signals), is_flickering,
            )

        # ── 8. Risk + esecuzione ───────────────────────────────────────────
        try:
            account = self.alpaca.get_account()
        except Exception as exc:
            logger.error("Impossibile leggere account Alpaca: %s — skip esecuzione.", exc)
            if self.alerts:
                self.alerts.api_lost(str(exc))
            account = {"equity": self._start_equity, "cash": 0.0, "buying_power": 0.0}

        # Aggiorna il contesto globale dei log JSON con lo stato corrente
        from monitoring.logger import set_log_context
        _equity = account.get("equity", self._start_equity)
        set_log_context(
            regime=regime_state.label,
            probability=regime_state.probability,
            equity=_equity,
            positions=len(self.tracker.get_all_positions()),
            daily_pnl=_equity - self._daily_start_equity,
        )

        portfolio_state = self._build_portfolio_state(account, regime_state, is_flickering)

        # Alert su circuit breaker attivo
        cb_level = self.risk_mgr.circuit_breaker.check()
        if cb_level.value != "NORMAL" and self.alerts:
            self.alerts.circuit_breaker(cb_level.value, portfolio_state.drawdown)

        current_weights = self.tracker.get_weights(account["equity"])
        target_weights  = {
            s.symbol: self._effective_target_weight(s)
            for s in signals
            if s.direction == "LONG"
        }

        # Mercato regolare chiuso (extended hours / notte / festivo): nessun nuovo
        # ingresso. Il bot è intraday e non deve aprire posizioni fuori sessione
        # (altrimenti resterebbero overnight). Stop/trailing/square-off restano attivi.
        if self.intraday and not self._market_open_now():
            logger.info("Mercato regolare chiuso: nessun nuovo ingresso (no extended hours/overnight).")
            if signals:
                self._log_event("SKIP", "*", "mercato chiuso: niente BUY")
        # Dopo lo square-off serale non si aprono nuove posizioni fino al giorno dopo
        elif self.intraday and self._squared_off_date == date.today():
            logger.info("Square-off già eseguito oggi: nessuna nuova posizione fino a domani.")
        elif signals and self._in_opening_blackout():
            # Primi minuti dall'apertura: spread larghi e movimenti falsi.
            # Niente BUY; stop, trailing e square-off restano pienamente attivi.
            minutes = self.config.get("broker", {}).get("no_entry_first_minutes", 30)
            logger.info("Blackout apertura: nessun ingresso nei primi %s minuti.", minutes)
            self._log_event("SKIP", "*", f"blackout apertura ({minutes} min): niente BUY")
        elif signals and self.calendar and (macro_ev := self.calendar.in_blackout()):
            # Annuncio macro ad alto impatto imminente/appena uscito: il mercato
            # fa movimenti violenti e imprevedibili → niente nuovi ingressi.
            logger.info("Blackout evento macro: %s — nessun ingresso.", macro_ev)
            self._log_event("SKIP", "*", f"evento macro: {macro_ev.title} — niente BUY")
        elif signals and self.orchestrator.needs_rebalance(current_weights, target_weights):
            equity   = account["equity"]
            risk_cfg = self.config.get("risk", {})
            # Tetto TOTALE = min(max_exposure, allocazione di regime): in CRASH
            # il 60% vale per tutto il portafoglio, non solo per singolo titolo.
            max_exp  = self._effective_max_exposure(signals)

            # Budget di esposizione: mai oltre il tetto sull'equity CORRENTE.
            # Sottrae l'esposizione già impegnata dalle posizioni aperte → niente margine.
            already_used = sum(p.market_value for p in self.tracker.get_all_positions().values())
            exposure_budget = max(0.0, equity * max_exp - already_used)
            logger.info(
                "Budget esposizione: $%.0f (tetto regime %.0f%% di $%.0f, già impegnato $%.0f)",
                exposure_budget, max_exp * 100, equity, already_used,
            )

            open_positions = len(self.tracker.get_all_positions())
            max_concurrent = int(risk_cfg.get("max_concurrent", 5))

            # Esegue dal segnale con size target più alto (priorità ai più convinti)
            for signal in sorted(signals, key=lambda s: s.position_size_pct, reverse=True):
                is_new_position = signal.symbol not in current_weights
                if is_new_position and open_positions >= max_concurrent:
                    logger.debug(
                        "Skip %s: limite posizioni già raggiunto (%d/%d).",
                        signal.symbol, open_positions, max_concurrent,
                    )
                    continue
                if is_new_position and self._reentry_blocked(signal.symbol):
                    until = self._reentry_block[signal.symbol]
                    self._log_event(
                        "SKIP", signal.symbol,
                        f"cooldown post-vendita fino alle {until.strftime('%H:%M')}",
                    )
                    continue

                current_weight = current_weights.get(signal.symbol, 0.0)
                adjusted_signal = self._signal_for_rebalance_delta(
                    signal=signal,
                    current_weight=current_weight,
                    equity=equity,
                    exposure_budget=exposure_budget,
                )
                if adjusted_signal is None:
                    logger.debug(
                        "Skip %s: già vicino al target operativo o budget insufficiente.",
                        signal.symbol,
                    )
                    continue
                spent = self._execute_signal(adjusted_signal, portfolio_state, equity, exposure_budget)
                exposure_budget = max(0.0, exposure_budget - spent)
        elif signals:
            logger.debug("Nessun ribilanciamento necessario (drift < soglia).")

        # ── 9. Trailing stop ───────────────────────────────────────────────
        if not self.dry_run:
            self._update_trailing_stops(regime_state)

        # ── 10. Dashboard + snapshot live ─────────────────────────────────
        self._collect_dashboard_data(regime_state, account, is_flickering, signals)
        self._print_dashboard(regime_state, account, is_flickering)
        self._save_snapshot(quiet=True)   # aggiorna il file per la dashboard live

        # ── 11. Retraining settimanale ────────────────────────────────────
        self._maybe_retrain_weekly()

    def _build_portfolio_state(self, account: dict, regime_state, is_flickering: bool):
        """Assembla PortfolioState dai dati live correnti."""
        from core.risk_manager import PortfolioState, PositionInfo

        positions = {
            sym: PositionInfo(
                symbol        = sym,
                shares        = pos.qty,
                entry_price   = pos.avg_entry_price,
                current_price = pos.current_price,
                stop_loss     = pos.stop_level,
                sector        = self.risk_mgr._sector_map.get(sym, "UNKNOWN"),
            )
            for sym, pos in self.tracker.get_all_positions().items()
        }

        equity = account["equity"]
        cb_level = self.risk_mgr.circuit_breaker.update(equity, date.today(), regime_state.label)
        self._peak_equity = max(self._peak_equity, equity)

        # Prezzo history per correlazione: prende close degli ultimi 60gg
        primary = self.symbols[0]
        price_hist = self._bars_cache.get(primary)
        if price_hist is not None:
            price_hist = price_hist.tail(60)[["close"]]

        return PortfolioState(
            equity          = equity,
            cash            = account["cash"],
            buying_power    = account["buying_power"],
            positions       = positions,
            daily_pnl       = equity - self._daily_start_equity,
            weekly_pnl      = equity - self._weekly_start_equity,
            peak_equity     = self._peak_equity,
            drawdown        = (self._peak_equity - equity) / self._peak_equity if self._peak_equity else 0.0,
            circuit_breaker_status = cb_level,
            flicker_rate    = float(self.hmm.get_regime_flicker_rate()),
            current_regime  = regime_state.label,
            current_date    = date.today(),
            price_history   = price_hist,
        )

    def _effective_target_weight(self, signal) -> float:
        """Target operativo dopo cap di leva e singola posizione."""
        risk_cfg     = self.config.get("risk", {})
        max_leverage = float(risk_cfg.get("max_leverage", 1.0))
        max_single   = float(risk_cfg.get("max_single_position", 0.15))

        leverage = min(float(getattr(signal, "leverage", 1.0)), max_leverage)
        raw_weight = float(signal.position_size_pct) * leverage
        return max(0.0, min(raw_weight, max_single))

    def _effective_max_exposure(self, signals) -> float:
        """
        Tetto di esposizione TOTALE del ciclo: il più basso tra risk.max_exposure
        e l'allocazione di regime della strategia.

        Senza questo cap, il 60% del regime CRASH varrebbe solo per singolo
        titolo (poi cappato al 15%) e con 5 posizioni il portafoglio salirebbe
        al 75%: l'allocazione di regime deve valere per il PORTAFOGLIO intero.
        In incertezza (sizing dimezzato) anche il tetto totale si dimezza.
        """
        risk_cfg     = self.config.get("risk", {})
        max_exp      = float(risk_cfg.get("max_exposure", 0.80))
        max_leverage = float(risk_cfg.get("max_leverage", 1.0))

        long_sigs = [s for s in signals if getattr(s, "direction", "") == "LONG"]
        if not long_sigs:
            return max_exp

        sig = long_sigs[0]   # l'allocazione di regime è uguale per tutti i segnali
        regime_cap = float(sig.position_size_pct) * min(float(sig.leverage), max_leverage)
        return max(0.0, min(max_exp, regime_cap))

    def _signal_for_rebalance_delta(
        self,
        signal,
        current_weight: float,
        equity: float,
        exposure_budget: float,
    ):
        """
        Converte un target di portafoglio in un ordine incrementale.

        Esempio: target operativo 15%, posizione già 14% → delta 1%.
        Se il delta è sotto la soglia minima, non manda ordini minuscoli.
        """
        risk_cfg = self.config.get("risk", {})
        min_pos_usd = float(risk_cfg.get("min_position_usd", 100.0))

        target_weight = self._effective_target_weight(signal)
        delta_weight = max(0.0, target_weight - current_weight)

        # Anti-churn: sotto l'1% di scarto dal target non si ribilancia.
        # Senza questa soglia il bot inviava top-up da 1 azione a ogni ciclo
        # appena il prezzo si muoveva (visto il 2026-06-10).
        if delta_weight < 0.01:
            return None

        order_value = min(delta_weight * equity, exposure_budget)
        min_order_value = max(min_pos_usd, float(signal.entry_price))

        if order_value < min_order_value or equity <= 0:
            return None

        return replace(
            signal,
            position_size_pct=order_value / equity,
            leverage=1.0,
        )

    def _execute_signal(self, signal, portfolio_state, equity: float,
                        exposure_budget: Optional[float] = None) -> float:
        """
        Valida con il risk manager ed esegue (o logga se dry-run).

        Args:
            exposure_budget: capitale ancora investibile in questo ciclo (USD).
                             Se fornito, l'ordine non lo supera → niente margine.

        Returns:
            Il capitale effettivamente impegnato (USD), 0.0 se nessun ordine.
        """
        from alpaca.trading.enums import OrderSide

        decision = self.risk_mgr.validate_signal(signal, portfolio_state)

        if not decision.approved:
            logger.info(
                "REJECTED %s: %s",
                signal.symbol, decision.rejection_reason,
            )
            self._log_event("REJECT", signal.symbol, decision.rejection_reason or "")
            return 0.0

        effective_signal = decision.modified_signal or signal
        if decision.modifications:
            logger.info("MODIFIED %s: %s", signal.symbol, " | ".join(decision.modifications))

        risk_cfg     = self.config.get("risk", {})
        max_leverage = float(risk_cfg.get("max_leverage", 1.0))
        max_single   = float(risk_cfg.get("max_single_position", 0.15))
        min_pos_usd  = float(risk_cfg.get("min_position_usd", 100.0))

        # NO MARGINE: la leva non supera mai max_leverage (1.0 = solo capitale proprio)
        leverage = min(effective_signal.leverage, max_leverage)
        # Cap singola posizione (% dell'equity corrente)
        size_pct = min(effective_signal.position_size_pct, max_single)

        target_value = equity * size_pct * leverage

        # Cap al budget di esposizione residuo del ciclo (impedisce di sforare max_exposure)
        if exposure_budget is not None:
            target_value = min(target_value, exposure_budget)

        if target_value < min_pos_usd:
            logger.info("Budget insufficiente per %s ($%.0f < $%.0f min), skip.",
                        signal.symbol, target_value, min_pos_usd)
            return 0.0

        shares = int(target_value / effective_signal.entry_price) if effective_signal.entry_price > 0 else 0

        if shares <= 0:
            logger.warning("Shares calcolate = 0 per %s, skip.", signal.symbol)
            return 0.0

        committed = shares * effective_signal.entry_price  # capitale impegnato

        side = OrderSide.BUY if effective_signal.direction == "LONG" else OrderSide.SELL
        trade_id = f"{effective_signal.symbol}-{datetime.now().strftime('%H%M%S')}"

        log_entry = {
            "timestamp":  datetime.now().isoformat(),
            "symbol":     effective_signal.symbol,
            "direction":  effective_signal.direction,
            "shares":     shares,
            "entry":      effective_signal.entry_price,
            "stop":       effective_signal.stop_loss,
            "regime":     effective_signal.regime_name,
            "dry_run":    self.dry_run,
        }
        # NB: l'append a _trade_log avviene SOLO quando l'ordine viene davvero
        # inviato (o simulato in dry-run): prima finivano nel report anche i
        # BUY skippati per wash-trade, gonfiando il conteggio "Ordini inviati".

        from monitoring.logger import log_trade

        if self.dry_run:
            self._trade_log.append(log_entry)
            logger.info(
                "[DRY-RUN] ORDER %s %s %d @ %.2f | stop=%.2f | size=%.1f%%",
                effective_signal.direction, effective_signal.symbol, shares,
                effective_signal.entry_price, effective_signal.stop_loss,
                effective_signal.position_size_pct * 100,
            )
            log_trade(
                f"[DRY-RUN] {effective_signal.direction} {effective_signal.symbol}",
                trade_id=trade_id, symbol=effective_signal.symbol,
                direction=effective_signal.direction, shares=shares,
                entry=effective_signal.entry_price, stop=effective_signal.stop_loss,
                regime=effective_signal.regime_name, dry_run=True,
            )
            return committed

        if side == OrderSide.BUY:
            try:
                if self.executor.has_open_stop_order(effective_signal.symbol):
                    reason = (
                        "stop SELL già aperto sul broker: skip BUY per evitare wash-trade Alpaca"
                    )
                    logger.warning("SKIP %s: %s", effective_signal.symbol, reason)
                    self._log_event("SKIP", effective_signal.symbol, reason)
                    return 0.0
            except Exception as exc:
                logger.warning(
                    "Impossibile verificare stop aperti per %s prima del BUY: %s",
                    effective_signal.symbol, exc,
                )
                return 0.0

        self._trade_log.append(log_entry)

        try:
            # Entry come ordine LIMIT/market (con retry a mercato)
            result = self.executor.submit_order(
                symbol      = effective_signal.symbol,
                shares      = shares,
                side        = side,
                entry_price = effective_signal.entry_price,
                trade_id    = trade_id,
            )
            logger.info(
                "ORDER %s → %s | qty=%.0f avg=%.2f",
                effective_signal.symbol, result.status.value,
                result.filled_qty, result.avg_fill_price,
            )
            log_trade(
                f"ORDER {effective_signal.symbol} {result.status.value}",
                trade_id=trade_id, order_id=result.order_id,
                symbol=effective_signal.symbol, side=result.side, shares=shares,
                status=result.status.value, filled_qty=result.filled_qty,
                avg_fill_price=result.avg_fill_price, stop=effective_signal.stop_loss,
                regime=effective_signal.regime_name,
            )
            self._log_event(
                "BUY", effective_signal.symbol,
                f"{shares} az @ {result.avg_fill_price or effective_signal.entry_price:.2f}",
            )

            # Protezione: piazza SUBITO lo stop loss sul broker (resta attivo anche a bot spento)
            if result.filled_qty > 0 and effective_signal.stop_loss and side == OrderSide.BUY:
                # Lo stop deve stare SOTTO il prezzo di fill: se il calcolo della
                # strategia lo mette sopra (in intraday capita), lo abbassiamo a -1%.
                fill_price = result.avg_fill_price or effective_signal.entry_price
                safe_stop  = effective_signal.stop_loss
                if fill_price > 0 and safe_stop >= fill_price:
                    safe_stop = round(fill_price * 0.99, 2)
                    logger.warning(
                        "Stop %s (%.2f) sopra il prezzo di fill (%.2f): abbassato a %.2f",
                        effective_signal.symbol, effective_signal.stop_loss, fill_price, safe_stop,
                    )
                self.tracker.set_stop_level(effective_signal.symbol, safe_stop)
                try:
                    self.executor.place_protective_stop(
                        symbol=effective_signal.symbol,
                        shares=int(result.filled_qty),
                        stop_price=safe_stop,
                    )
                    log_trade(
                        f"STOP {effective_signal.symbol} @ {safe_stop:.2f}",
                        symbol=effective_signal.symbol, stop=safe_stop,
                        shares=int(result.filled_qty),
                    )
                    self._log_event("STOP", effective_signal.symbol, f"stop @ {safe_stop:.2f}")
                except Exception as exc:
                    # Non è una perdita di connessione: solo lo stop non piazzato
                    logger.error("Impossibile piazzare stop protettivo %s: %s", effective_signal.symbol, exc)

            return committed

        except Exception as exc:
            logger.error("Errore esecuzione ordine %s: %s", effective_signal.symbol, exc)
            return 0.0

    def _on_fill(self, execution_result) -> None:
        """Callback chiamato da OrderExecutor dopo ogni fill confermato."""
        self.tracker.record_fill(
            symbol = execution_result.symbol,
            qty    = execution_result.filled_qty,
            price  = execution_result.avg_fill_price,
            side   = execution_result.side,
            fill_id = execution_result.order_id,
        )
        logger.info(
            "Fill confermato: %s %s %.0f @ %.2f",
            execution_result.side.upper(), execution_result.symbol,
            execution_result.filled_qty, execution_result.avg_fill_price,
        )
        from monitoring.logger import log_trade
        log_trade(
            f"FILL {execution_result.symbol}",
            trade_id=execution_result.trade_id, order_id=execution_result.order_id,
            symbol=execution_result.symbol, side=execution_result.side,
            filled_qty=execution_result.filled_qty,
            avg_fill_price=execution_result.avg_fill_price,
        )

    def _on_tracker_fill_event(self, symbol: str, qty: float, price: float, side: str) -> None:
        """Registra in dashboard le chiusure arrivate da Alpaca, inclusi stop broker."""
        if side.lower() != "sell":
            return

        # Cooldown anti-churn: dopo una vendita (stop incluso) niente nuovo
        # ingresso sul simbolo per N minuti. Senza, in mercato in discesa il
        # bot ricomprava 26 secondi dopo lo stop-out (visto il 2026-06-10).
        cooldown_min = float(self.config.get("risk", {}).get("reentry_cooldown_minutes", 30))
        if cooldown_min > 0:
            self._reentry_block[symbol] = datetime.now() + timedelta(minutes=cooldown_min)

        remaining = self.tracker.get_position(symbol)
        if remaining:
            self._log_event("SELL", symbol, f"-{qty:.0f} az @ {price:.2f} | residuo {remaining.qty:.0f}")
        else:
            self._log_event("CLOSE", symbol, f"{qty:.0f} az @ {price:.2f}")

    def _reentry_blocked(self, symbol: str) -> bool:
        """True se il simbolo è in cooldown dopo una vendita recente."""
        until = self._reentry_block.get(symbol)
        return bool(until and datetime.now() < until)

    def _in_opening_blackout(self, now_et: Optional[datetime] = None) -> bool:
        """
        True nei primi `no_entry_first_minutes` dopo l'apertura USA (9:30 ET).

        Statistica nota dell'intraday: la prima mezz'ora ha spread larghi e
        movimenti falsi — entrare lì significa pagare il prezzo peggiore della
        giornata. Il fuso ET (zoneinfo) gestisce da solo l'ora legale.

        Args:
            now_et: orario corrente in ET, iniettabile nei test.
        """
        minutes = float(self.config.get("broker", {}).get("no_entry_first_minutes", 0))
        if minutes <= 0:
            return False
        from zoneinfo import ZoneInfo
        if now_et is None:
            now_et = datetime.now(ZoneInfo("America/New_York"))
        open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        return open_et <= now_et < open_et + timedelta(minutes=minutes)

    def _market_open_now(self) -> bool:
        """
        True se la sessione REGOLARE è aperta adesso (clock Alpaca).

        Serve a impedire NUOVI ingressi fuori orario regolare. Senza questo
        controllo, se lo stream consegna una barra in extended hours / dopo lo
        square-off (il guard `_squared_off_date` si azzera a mezzanotte), il bot
        apriva posizioni a mercato chiuso e le teneva overnight — contro il
        principio intraday/no-gap (bug del 2026-06-17: 5 entrate a 00:01 tenute
        ~9h). Stop, trailing e square-off NON passano da qui e restano attivi.

        In dry-run non blocca. Su errore di lettura del clock ritorna False
        (conservativo: meglio saltare un ingresso che tradare a mercato chiuso).
        """
        if self.dry_run:
            return True
        try:
            clock = self.alpaca.get_clock()
            return bool(clock.get("is_open"))
        except Exception as exc:
            logger.warning(
                "Clock non leggibile per il gating ingressi: %s — niente BUY per sicurezza.", exc
            )
            return False

    # ─── Trailing stop ────────────────────────────────────────────────────────

    def _run_stop_maintenance(self) -> None:
        """
        Ciclo leggero tra una barra e l'altra: aggiorna i trailing stop con i
        prezzi CORRENTI del broker e ricrea eventuali stop mancanti.
        NON genera nuovi segnali (le decisioni restano sulla barra chiusa).
        """
        if self.dry_run or not self.tracker:
            return

        try:
            self.tracker.sync()
        except Exception as exc:
            logger.warning("Manutenzione stop: sync posizioni fallito: %s", exc)

        positions = self.tracker.get_all_positions()
        if not positions:
            return

        # Prezzo più recente per le sole posizioni aperte (bar all'ultimo minuto)
        live_prices = self._fetch_live_prices(list(positions.keys()))

        regime_state = self._last_regime_state
        if regime_state is None:
            # Nessuna barra processata ancora (es. recovery post-riavvio):
            # tratta come alta volatilità → stop più ampio, niente strette azzardate
            regime_state = SimpleNamespace(label="HIGH_VOL")

        logger.debug(
            "Manutenzione stop: %d posizioni, prezzi live per %s",
            len(positions), sorted(live_prices) or "nessuno",
        )
        self._update_trailing_stops(regime_state, live_prices=live_prices)
        self._save_snapshot(quiet=True)   # la dashboard vede gli stop aggiornati

    def _fetch_live_prices(self, symbols: list[str]) -> dict[str, float]:
        """Ultimo prezzo per simbolo dal broker (bar al minuto più recente)."""
        prices: dict[str, float] = {}
        if not symbols:
            return prices
        try:
            latest = self.alpaca.get_latest_bar(symbols)
            for sym, bar in (latest or {}).items():
                close = bar.get("close") if isinstance(bar, dict) else getattr(bar, "close", None)
                if close:
                    prices[sym] = float(close)
        except Exception as exc:
            logger.warning("Prezzi live non disponibili: %s", exc)
        return prices

    def _update_trailing_stops(self, regime_state, live_prices: Optional[dict] = None) -> None:
        """
        Stringe (mai allarga) lo stop di ogni posizione aperta in base al regime.
        Low vol → stop = EMA50 − 0.5×ATR14
        Mid vol → stop = EMA50 − 0.5×ATR14
        High vol → stop = EMA50 − 1.0×ATR14

        Args:
            live_prices: prezzi correnti per simbolo; se non forniti li scarica
                         dal broker. Il guard "stop sotto il prezzo" DEVE usare
                         il prezzo vivo: col close stantio della cache il bot
                         inviava stop sopra il mercato (errori 42210000 a raffica
                         il 2026-06-10).
        """
        from core.regime_strategies import _atr, _ema

        # Dopo lo square-off (o durante lo shutdown) gli stop NON vanno toccati:
        # la chiusura serale cancella gli stop per vendere le azioni; se il
        # trailing li "ripristina" sequestra le azioni e può bloccare la chiusura
        # (race vista il 2026-06-10 alle 21:45: stop SH ripiazzato dalla
        # manutenzione mentre lo square-off stava chiudendo la posizione).
        if self._shutdown_ev.is_set():
            return
        if self.intraday and self._squared_off_date == date.today():
            return

        if live_prices is None and not self.dry_run:
            live_prices = self._fetch_live_prices(list(self.tracker.get_all_positions()))

        label = regime_state.label.upper()
        atr_mult = 1.0 if "HIGH" in label or "BEAR" in label or "CRASH" in label else 0.5

        for sym, pos in self.tracker.get_all_positions().items():
            bars = self._bars_cache.get(sym)
            if bars is None or len(bars) < 60:
                continue
            try:
                price = float(bars["close"].iloc[-1])
                if live_prices and live_prices.get(sym, 0.0) > 0:
                    price = float(live_prices[sym])
                ema   = _ema(bars["close"])
                atr   = _atr(bars)
                # Lo stop deve SEMPRE stare sotto il prezzo (in crash EMA50 è
                # sopra il prezzo). Cap con lo stesso atr_mult del regime:
                # in high-vol la distanza minima è 1,0×ATR, non 0,5.
                new_stop = min(ema - atr_mult * atr, price - atr_mult * atr)
                # Solo se valido e sotto il prezzo corrente
                if new_stop >= price:
                    continue
                stop_price = round(new_stop, 2)
                updated = self.executor.modify_stop(sym, new_stop=stop_price, side="long")
                has_stop = False
                try:
                    has_stop = self.executor.has_open_stop_order(sym)
                except Exception as exc:
                    logger.warning("Impossibile verificare stop aperti per %s: %s", sym, exc)

                if not updated and not has_stop:
                    shares = int(pos.qty)
                    if shares > 0:
                        try:
                            self.executor.place_protective_stop(
                                symbol=sym,
                                shares=shares,
                                stop_price=stop_price,
                            )
                            logger.warning(
                                "Stop broker mancante per %s: ripiazzato a %.2f per %d azioni",
                                sym, stop_price, shares,
                            )
                        except Exception as exc:
                            logger.error(
                                "Stop broker mancante per %s ma ripristino fallito: %s",
                                sym, exc,
                            )
                self.tracker.set_stop_level(sym, stop_price)
            except Exception as exc:
                logger.warning("Errore trailing stop %s: %s", sym, exc)

    # ─── HMM load/train ───────────────────────────────────────────────────────

    def _load_or_train_hmm(self):
        from core.hmm_engine import HMMEngine

        hmm_config  = self.config.get("hmm", {})
        model_path  = _MODEL_PATH
        model_path.parent.mkdir(parents=True, exist_ok=True)

        # --train-only forza sempre il riaddestramento (ignora il modello esistente)
        if getattr(self.args, "train_only", False):
            logger.info("--train-only: riaddestramento forzato.")
            return self._train_hmm(hmm_config, model_path)

        # Carica modello se recente (< 7 giorni)
        if model_path.exists():
            age = datetime.now() - datetime.fromtimestamp(model_path.stat().st_mtime)
            if age < _MODEL_MAX_AGE:
                try:
                    engine = HMMEngine.load(model_path)
                    logger.info("Modello HMM caricato da %s (età: %s)", model_path, age)
                    return engine
                except Exception as exc:
                    logger.warning("Impossibile caricare modello: %s — retraining.", exc)

        return self._train_hmm(hmm_config, model_path)

    def _train_hmm(self, hmm_config: dict, model_path: Path):
        from core.hmm_engine import HMMEngine

        primary = self.symbols[0]
        bars    = self._bars_cache.get(primary)
        if bars is None or len(bars) < 300:
            raise RuntimeError(f"Dati insufficienti per addestrare HMM: {len(bars) if bars is not None else 0} barre")

        logger.info("Addestramento HMM su %d barre di %s…", len(bars), primary)
        features = self.fe.compute(bars)
        engine   = HMMEngine(config=hmm_config)
        engine.fit(features)
        engine.save(model_path)
        logger.info(
            "HMM addestrato: %d stati | BIC=%.1f | salvato in %s",
            engine.n_states, engine._best_bic, model_path,
        )
        self._last_retrain_date = date.today()
        return engine

    def _maybe_retrain_weekly(self) -> None:
        """Retraining settimanale dell'HMM (ogni domenica o se >7gg)."""
        today = date.today()
        if self._last_retrain_date is None:
            self._last_retrain_date = today
            return
        if (today - self._last_retrain_date).days < 7:
            return

        logger.info("Retraining settimanale HMM…")
        try:
            # Aggiorna i dati storici prima del retrain
            self._bars_cache = self.data_feed.update(self.symbols)
            hmm_config = self.config.get("hmm", {})
            self.hmm = self._train_hmm(hmm_config, _MODEL_PATH)
            self.orchestrator.update_regime_infos(self.hmm._regime_info)
            logger.info("Retraining completato. Nuovi stati: %d", self.hmm.n_states)
            if self.alerts:
                self.alerts.hmm_retrained(self.hmm.n_states, float(self.hmm._best_bic))
        except Exception as exc:
            logger.error("Retraining fallito: %s — mantengo modello precedente.", exc)

    # ─── Snapshot ─────────────────────────────────────────────────────────────

    def _save_snapshot(self, quiet: bool = False) -> None:
        """
        Salva lo stato della sessione in state_snapshot.json.

        Include sia i campi di recovery (equity, stop levels) sia i campi
        "live" per la dashboard a refresh automatico (self._dash_data).
        """
        positions_state = {}
        for sym, pos in self.tracker.get_all_positions().items():
            positions_state[sym] = {
                "qty":             pos.qty,
                "avg_entry_price": pos.avg_entry_price,
                "current_price":   pos.current_price,
                "unrealized_pnl":  pos.unrealized_pnl,
                "unrealized_pnl_pct": pos.unrealized_pnl_pct,
                "side":            pos.side,
                "stop_level":      pos.stop_level,
                "regime_at_entry": pos.regime_at_entry,
                "current_regime":  pos.current_regime,
                "holding_bars":    pos.holding_bars,
                "opened_at":       pos.opened_at.isoformat() if pos.opened_at else None,
            }

        snapshot = {
            "session_start":        self._session_start.isoformat(),
            "saved_at":             datetime.now().isoformat(),
            "start_equity":         self._start_equity,
            "peak_equity":          self._peak_equity,
            "daily_start_equity":   self._daily_start_equity,
            "weekly_start_equity":  self._weekly_start_equity,
            "last_retrain_date":    self._last_retrain_date.isoformat() if self._last_retrain_date else None,
            "circuit_breaker":      self.risk_mgr.circuit_breaker.check().value if self.risk_mgr else "NORMAL",
            "positions":            positions_state,
            "trade_count":          len(self._trade_log),
            # Sezione live per la dashboard (popolata ad ogni ciclo della pipeline)
            "dashboard":            self._dash_data,
        }

        try:
            _SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, default=str))
            if not quiet:
                logger.info("Snapshot salvato in %s.", _SNAPSHOT_PATH)
        except Exception as exc:
            logger.error("Impossibile salvare snapshot: %s", exc)

    def _load_snapshot(self) -> None:
        """Recupera lo stato della sessione precedente (se esiste)."""
        if not _SNAPSHOT_PATH.exists():
            return

        try:
            data = json.loads(_SNAPSHOT_PATH.read_text())
            saved_at = data.get("saved_at", "sconosciuto")
            logger.info("Recovery da snapshot del %s", saved_at)

            self._peak_equity = data.get("peak_equity", self._peak_equity)
            if data.get("last_retrain_date"):
                self._last_retrain_date = date.fromisoformat(data["last_retrain_date"])

            # Ripristina stop levels sulle posizioni già aperte
            for sym, pos_data in data.get("positions", {}).items():
                stop = pos_data.get("stop_level", 0.0)
                if stop > 0:
                    self.tracker.set_stop_level(sym, stop)
                    logger.info("Stop ripristinato: %s → %.2f", sym, stop)

        except Exception as exc:
            logger.warning("Impossibile caricare snapshot: %s — partenza pulita.", exc)

    # ─── Dashboard terminale ──────────────────────────────────────────────────

    def _log_event(self, kind: str, symbol: str, detail: str = "") -> None:
        """Registra un evento (BUY/SELL/STOP/CLOSE/REJECT) per la dashboard."""
        self._events.append({
            "time":   datetime.now().strftime("%H:%M:%S"),
            "kind":   kind,
            "symbol": symbol,
            "detail": detail,
        })
        self._events = self._events[-30:]   # tieni gli ultimi 30

        # Gli eventi di trading veri vanno anche su Telegram/webhook
        # (SKIP/REJECT esclusi: troppo rumore per una notifica push).
        if self.alerts and kind in ("BUY", "SELL", "CLOSE", "STOP"):
            try:
                self.alerts.trade_event(kind, symbol, detail)
            except Exception as exc:
                logger.debug("Notifica trade non inviata: %s", exc)

    def _collect_dashboard_data(self, regime_state, account: dict, is_flickering: bool, signals: list) -> None:
        """Assembla i dati ricchi per la dashboard live e li salva in self._dash_data."""
        equity   = account.get("equity", self._start_equity)
        daily_pnl = equity - self._daily_start_equity
        daily_pnl_pct = daily_pnl / self._daily_start_equity if self._daily_start_equity else 0.0
        daily_dd  = max(0.0, (self._daily_start_equity - equity) / self._daily_start_equity) if self._daily_start_equity else 0.0
        peak_dd   = max(0.0, (self._peak_equity - equity) / self._peak_equity) if self._peak_equity else 0.0

        # Allocazione/leva dal primo segnale LONG (rappresentativo del regime)
        long_sigs = [s for s in signals if s.direction == "LONG"]
        alloc = long_sigs[0].position_size_pct if long_sigs else 0.0
        lev   = long_sigs[0].leverage if long_sigs else 1.0
        positions = self.tracker.get_all_positions()
        current_weights = self.tracker.get_weights(equity)
        risk_cfg = self.config.get("risk", {})
        max_concurrent = int(risk_cfg.get("max_concurrent", 5))
        # Stesso tetto totale usato dal loop di esecuzione (regime cap incluso)
        max_exp = self._effective_max_exposure(long_sigs)
        exposure_budget = max(0.0, equity * max_exp - sum(p.market_value for p in positions.values()))

        # Aggiorna i segnali recenti (max 5) per il pannello dedicato
        for s in long_sigs:
            self._recent_signals.append({
                "time":   datetime.now().strftime("%H:%M"),
                "symbol": s.symbol,
                "action": f"{s.direction} {s.position_size_pct:.0%}",
                "regime": regime_state.label,
            })
        self._recent_signals = self._recent_signals[-5:]

        considered = [
            self._dashboard_signal_row(
                signal=s,
                equity=equity,
                current_weight=current_weights.get(s.symbol, 0.0),
                positions=positions,
                exposure_budget=exposure_budget,
                max_concurrent=max_concurrent,
            )
            for s in long_sigs
        ]

        flicker_window = int(self.config.get("hmm", {}).get("flicker_window", 20))
        mode = "DRY-RUN" if self.dry_run else ("LIVE" if not self.config.get("broker", {}).get("paper_trading", True) else "PAPER")

        self._dash_data = {
            "regime":            regime_state.label,
            "probability":       regime_state.probability,
            "is_confirmed":      regime_state.is_confirmed,
            "consecutive_bars":  regime_state.consecutive_bars,
            "state_probabilities": list(regime_state.state_probabilities),
            "flicker_rate":      float(self.hmm.get_regime_flicker_rate()),
            "flicker_window":    flicker_window,
            "equity":            equity,
            "daily_pnl":         daily_pnl,
            "daily_pnl_pct":     daily_pnl_pct,
            "allocation_pct":    alloc,
            "leverage":          lev,
            "daily_dd":          daily_dd,
            "daily_dd_limit":    float(risk_cfg.get("daily_dd_halt", 0.03)),
            "peak_dd":           peak_dd,
            "peak_dd_limit":     float(risk_cfg.get("max_dd_from_peak", 0.10)),
            "circuit_breaker":   self.risk_mgr.circuit_breaker.check().value,
            "data_feed_ok":      True,
            "api_ok":            "id" in account and account.get("id") is not None or "equity" in account,
            "hmm_age_str":       self._hmm_age_str(),
            "trading_mode":      mode,
            "recent_signals":    list(self._recent_signals),
            "considered_signals": considered,
            "events":            list(reversed(self._events)),  # più recenti in cima
            "symbols":           list(self.symbols),            # universo considerato dal bot
        }

    def _dashboard_signal_row(
        self,
        signal,
        equity: float,
        current_weight: float,
        positions: dict,
        exposure_budget: float,
        max_concurrent: int,
    ) -> dict:
        """Riga leggibile per la dashboard: cosa ha valutato il bot e perché."""
        target_weight = self._effective_target_weight(signal)
        delta_weight = max(0.0, target_weight - current_weight)
        pos = positions.get(signal.symbol)
        price, price_change_pct = self._dashboard_price_change(signal.symbol, signal.entry_price)

        if pos and delta_weight <= 0.005:
            status = "IN TARGET"
        elif pos:
            status = "AUMENTA"
        elif len(positions) >= max_concurrent:
            status = "SKIP MAX POS"
        elif exposure_budget <= 0:
            status = "SKIP BUDGET"
        else:
            status = "CANDIDATO"

        return {
            "time": datetime.now().strftime("%H:%M:%S"),
            "symbol": signal.symbol,
            "status": status,
            "direction": signal.direction,
            "price": price,
            "price_change_pct": price_change_pct,
            "current_weight": current_weight,
            "target_weight": target_weight,
            "delta_weight": delta_weight,
            "signal_stop": signal.stop_loss,
            "active_stop": pos.stop_level if pos else 0.0,
            "regime": signal.regime_name,
            "confidence": signal.confidence,
        }

    def _dashboard_price_change(self, symbol: str, fallback_price: float) -> tuple[float, float]:
        """Prezzo ultimo bar e variazione percentuale rispetto al bar precedente."""
        bars = self._bars_cache.get(symbol)
        if bars is None or "close" not in bars or len(bars) == 0:
            return fallback_price, 0.0

        price = float(bars["close"].iloc[-1])
        if len(bars) < 2:
            return price, 0.0

        prev = float(bars["close"].iloc[-2])
        change = (price - prev) / prev if prev > 0 else 0.0
        return price, change

    def _hmm_age_str(self) -> str:
        """Età del modello HMM in formato leggibile."""
        if not _MODEL_PATH.exists():
            return "?"
        age = datetime.now() - datetime.fromtimestamp(_MODEL_PATH.stat().st_mtime)
        days = age.days
        if days >= 1:
            return f"{days}d ago"
        hours = age.seconds // 3600
        return f"{hours}h ago" if hours >= 1 else "appena"

    def _print_system_state(self, account: dict) -> None:
        mode = "DRY-RUN" if self.dry_run else ("PAPER" if self.config.get("broker", {}).get("paper_trading", True) else "⚠️  LIVE")
        print("\n" + "═" * 65)
        print(f"  REGIME TRADER — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  [{mode}]")
        print("═" * 65)
        print(f"  Equity:        ${account['equity']:>12,.2f}")
        print(f"  Buying Power:  ${account['buying_power']:>12,.2f}")
        print(f"  Simboli:       {', '.join(self.symbols)}")
        print(f"  Timeframe:     {self.timeframe}")
        print(f"  HMM stati:     {self.hmm.n_states}")
        print(f"  Posizioni:     {len(self.tracker.get_all_positions())}")
        print("═" * 65 + "\n")

    def _print_dashboard(self, regime_state, account: dict, is_flickering: bool) -> None:
        cb = self.risk_mgr.circuit_breaker.check().value
        equity  = account["equity"]
        daily_pnl  = equity - self._daily_start_equity
        session_pnl = equity - self._start_equity
        drawdown    = (self._peak_equity - equity) / self._peak_equity * 100 if self._peak_equity else 0.0

        now = datetime.now().strftime("%H:%M:%S")
        flicker_str = " [FLICKER]" if is_flickering else ""

        print(
            f"\r[{now}] {regime_state.label}{flicker_str} "
            f"conf={regime_state.probability:.0%} "
            f"CB={cb} "
            f"equity=${equity:,.0f} "
            f"day={'+' if daily_pnl>=0 else ''}{daily_pnl:,.0f} "
            f"session={'+' if session_pnl>=0 else ''}{session_pnl:,.0f} "
            f"DD={drawdown:.1f}%  ",
            end="", flush=True,
        )

    # ─── Shutdown ─────────────────────────────────────────────────────────────

    def _shutdown_seq(self) -> None:
        print()  # newline dopo la riga dashboard
        logger.info("Arresto ordinato in corso…")

        # Salva snapshot PRIMA di chiudere connessioni
        if self.risk_mgr and self.tracker:
            self._save_snapshot()

        # Chiudi WebSocket (NON chiude posizioni)
        if self.data_feed:
            try:
                self.data_feed.stop_stream()
            except Exception:
                pass
        if self.tracker:
            try:
                self.tracker.stop_stream()
            except Exception:
                pass

        # Chiusura posizioni: solo su spegnimento VOLONTARIO (non su crash).
        # Su crash le posizioni restano aperte ma protette dagli stop sul broker.
        keep = getattr(self.args, "keep_positions", False)
        closed_positions = False
        if (not self.dry_run and self.executor and self._clean_shutdown
                and not keep and self.tracker and self.tracker.get_all_positions()):
            n = len(self.tracker.get_all_positions())
            logger.info("Spegnimento volontario: chiusura di %d posizioni…", n)
            closed_positions = self._close_positions_until_flat()
        elif keep and self._clean_shutdown:
            logger.info("--keep-positions: posizioni lasciate aperte (protette dagli stop sul broker).")
        elif not self._clean_shutdown and self.tracker and self.tracker.get_all_positions():
            logger.warning("Arresto da errore: posizioni LASCIATE APERTE e protette dagli stop sul broker.")

        # Cancel ordini pendenti (se non già fatto da close_all_positions)
        if self.executor and not self.dry_run and not closed_positions:
            try:
                cancelled = self.executor.cancel_all_pending()
                if cancelled:
                    logger.info("%d ordini pendenti cancellati.", cancelled)
            except Exception as exc:
                logger.warning("Errore cancellazione ordini: %s", exc)

        self._print_session_summary()
        self._write_session_report()
        logger.info("Sistema offline.")

    def _close_positions_until_flat(
        self,
        timeout_s: float = 60.0,
        retry_delay_s: float = 2.0,
    ) -> bool:
        """
        Chiude tutte le posizioni e verifica con Alpaca che il conto sia flat.

        Durante lo shutdown volontario è più importante essere davvero flat che
        limitarsi a inviare un singolo ordine di chiusura: eventuali stop/ordini
        disallineati possono lasciare quantità residue.
        """
        deadline = time.monotonic() + timeout_s
        attempt = 0

        while True:
            positions = self.tracker.get_all_positions()
            if not positions:
                logger.info("Shutdown: account flat verificato.")
                return True

            attempt += 1
            symbols = ", ".join(sorted(positions))
            logger.info(
                "Shutdown flat attempt %d: cancello ordini e chiudo %d posizioni (%s).",
                attempt, len(positions), symbols,
            )

            try:
                self.executor.cancel_all_pending()
            except Exception as exc:
                logger.warning("Shutdown: errore cancellazione ordini pendenti: %s", exc)

            try:
                results = self.executor.close_all_positions()
                logger.info(
                    "Shutdown flat attempt %d: inviati %d ordini di chiusura.",
                    attempt, len(results),
                )
            except Exception as exc:
                logger.error("Shutdown: errore chiusura posizioni: %s", exc)
                if self.alerts:
                    self.alerts.api_lost(f"Chiusura posizioni fallita: {exc}")

            if retry_delay_s > 0:
                time.sleep(retry_delay_s)

            try:
                self.tracker.sync()
            except Exception as exc:
                logger.warning("Shutdown: sync posizioni fallito: %s", exc)

            remaining = self.tracker.get_all_positions()
            if not remaining:
                logger.info("Shutdown: account flat verificato dopo %d tentativi.", attempt)
                return True

            if time.monotonic() >= deadline:
                logger.error(
                    "Shutdown: timeout, restano posizioni aperte: %s",
                    ", ".join(sorted(remaining)),
                )
                if self.alerts:
                    self.alerts.api_lost(
                        "Shutdown non flat: restano posizioni aperte "
                        + ", ".join(sorted(remaining))
                    )
                return False

    def _print_session_summary(self) -> None:
        duration = datetime.now() - self._session_start
        equity_final = self._start_equity  # approssimazione se alpaca è giù
        try:
            if self.alpaca:
                equity_final = self.alpaca.get_portfolio_value()
        except Exception:
            pass

        session_pnl = equity_final - self._start_equity
        pct = session_pnl / self._start_equity * 100 if self._start_equity else 0.0

        print("\n" + "─" * 55)
        print("  RIEPILOGO SESSIONE")
        print("─" * 55)
        print(f"  Durata:        {str(duration).split('.')[0]}")
        print(f"  Equity finale: ${equity_final:>12,.2f}")
        print(f"  P&L sessione:  ${session_pnl:>+12,.2f}  ({pct:+.2f}%)")
        print(f"  Trade eseguiti: {len(self._trade_log)}")
        print(f"  Picco equity:  ${self._peak_equity:>12,.2f}")
        print("─" * 55 + "\n")

    def _write_session_report(self) -> None:
        """
        Scrive il report markdown di fine sessione in reports/sessions/.
        Non deve MAI bloccare lo shutdown: ogni errore viene solo loggato.
        """
        try:
            from monitoring.session_report import SessionReporter

            final_eq = self._start_equity
            try:
                if self.alpaca:
                    final_eq = self.alpaca.get_portfolio_value()
            except Exception:
                pass

            open_positions: dict[str, dict] = {}
            if self.tracker:
                try:
                    for sym, pos in self.tracker.get_all_positions().items():
                        open_positions[sym] = {
                            "qty":             pos.qty,
                            "avg_entry_price": pos.avg_entry_price,
                            "stop_level":      pos.stop_level,
                        }
                except Exception:
                    pass

            mode = "DRY-RUN" if self.dry_run else (
                "PAPER" if self.config.get("broker", {}).get("paper_trading", True) else "LIVE"
            )

            path = SessionReporter().write({
                "session_start":  self._session_start,
                "session_end":    datetime.now(),
                "mode":           mode,
                "timeframe":      self.timeframe,
                "symbols":        list(self.symbols),
                "clean_shutdown": self._clean_shutdown,
                "start_equity":   self._start_equity,
                "final_equity":   final_eq,
                "peak_equity":    self._peak_equity,
                "trades":         list(self._trade_log),
                "events":         list(self._events),
                "regime_changes": list(self._regime_changes),
                "warnings":       list(self._log_capture.records) if self._log_capture else [],
                "open_positions": open_positions,
            })
            if path:
                print(f"  Report sessione: {path}\n")

            # Rigenera il trade journal aggregato (statistiche cross-sessione)
            try:
                from monitoring.trade_journal import TradeJournal
                journal = TradeJournal().update()
                if journal:
                    print(f"  Trade journal:   {journal}\n")
            except Exception as exc:
                logger.warning("Trade journal non aggiornato: %s", exc)

            # Riepilogo di fine sessione su Telegram/webhook (se configurati)
            if self.alerts:
                try:
                    duration = str(datetime.now() - self._session_start).split(".")[0]
                    pnl = final_eq - self._start_equity
                    pnl_pct = (pnl / self._start_equity * 100) if self._start_equity else 0.0
                    self.alerts.session_summary(
                        pnl=pnl, pnl_pct=pnl_pct, equity=final_eq,
                        n_orders=len(self._trade_log), duration=duration,
                        clean_shutdown=self._clean_shutdown,
                    )
                except Exception as exc:
                    logger.warning("Riepilogo sessione non notificato: %s", exc)
        except Exception as exc:
            logger.error("Report di sessione non scritto: %s", exc)

    # ─── Utilità ──────────────────────────────────────────────────────────────

    def _wait_for_market_open(self, clock: dict) -> None:
        next_open_str = clock.get("next_open")
        if not next_open_str:
            logger.info("Attesa 60 secondi poi ricontrollo.")
            time.sleep(60)
            return
        try:
            next_open = datetime.fromisoformat(next_open_str.replace("Z", "+00:00"))
            wait_secs = (next_open - datetime.now(next_open.tzinfo)).total_seconds()
            if wait_secs > 0:
                logger.info("Mercato chiuso. Attesa %.0f minuti fino all'apertura.", wait_secs / 60)
                time.sleep(min(wait_secs, 3600))
        except Exception:
            time.sleep(300)

    def _alert(self, message: str) -> None:
        """Logga un alert critico (estendibile a email/Slack/webhook)."""
        logger.critical("ALERT: %s", message)
        print(f"\n[ALERT] {message}", file=sys.stderr)


# ──────────────────────────────────────────────────────────────────────────────
# CARICAMENTO DATI (per backtest)
# ──────────────────────────────────────────────────────────────────────────────

def _load_ohlcv_alpaca(symbol: str, start: str, end: str, config: dict,
                       timeframe: str = "1Day") -> pd.DataFrame:
    """
    Scarica OHLCV storici via AlpacaClient (feed IEX gratuito), per qualsiasi timeframe.
    """
    import os
    from broker.alpaca_client import AlpacaClient

    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_SECRET_KEY"):
        raise EnvironmentError(
            "ALPACA_API_KEY e ALPACA_SECRET_KEY non trovati. Crea un file .env."
        )

    client = AlpacaClient.from_env()
    raw = client.get_bars([symbol], timeframe, start=start, end=end)
    bars = raw.get(symbol, [])
    if not bars:
        raise ValueError(f"Nessun dato ricevuto per {symbol} ({timeframe})")

    rows, ts = [], []
    for bar in bars:
        rows.append({
            "open": float(bar.open), "high": float(bar.high),
            "low": float(bar.low), "close": float(bar.close),
            "volume": float(bar.volume),
        })
        ts.append(bar.timestamp)
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(ts))
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    if timeframe == "1Day":
        df.index = df.index.normalize()
    return df[["open", "high", "low", "close", "volume"]]


def _load_ohlcv_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    date_cols = [c for c in df.columns if c.lower() in ("date", "datetime", "timestamp")]
    if not date_cols:
        raise ValueError(f"Colonna data non trovata in {csv_path}.")
    df = df.set_index(date_cols[0])
    df.index = pd.DatetimeIndex(df.index)
    df.columns = [c.lower() for c in df.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"Colonne mancanti nel CSV: {missing}")
    return df[["open", "high", "low", "close", "volume"]]


def _load_data(
    symbols: list[str],
    start: str,
    end: str,
    config: dict,
    csv_paths: list[str] | None = None,
    timeframe: str = "1Day",
) -> dict[str, pd.DataFrame]:
    data: dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols):
        if csv_paths and i < len(csv_paths):
            logger.info("Caricamento %s da CSV: %s", symbol, csv_paths[i])
            df = _load_ohlcv_csv(csv_paths[i])
        else:
            logger.info("Download %s da Alpaca (%s → %s, %s)…", symbol, start, end, timeframe)
            df = _load_ohlcv_alpaca(symbol, start, end, config, timeframe=timeframe)
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        logger.info("%s: %d barre caricate.", symbol, len(df))
        data[symbol] = df
    return data


def _synthetic_ohlcv(symbol: str, start: str, end: str, seed: int = 42) -> pd.DataFrame:
    dates = pd.bdate_range(start, end)
    n     = len(dates)
    rng   = np.random.default_rng(seed)
    sigmas  = np.where((np.arange(n) // 100) % 2 == 0, 0.005, 0.020)
    log_ret = rng.normal(0.0003, sigmas)
    close   = 400.0 * np.exp(np.cumsum(log_ret))
    return pd.DataFrame({
        "open":   close * (1 + rng.normal(0, 0.001, n)),
        "high":   close * (1 + np.abs(rng.normal(0, 0.004, n))),
        "low":    close * (1 - np.abs(rng.normal(0, 0.004, n))),
        "close":  close,
        "volume": rng.integers(5_000_000, 20_000_000, n).astype(float),
    }, index=dates)


# ──────────────────────────────────────────────────────────────────────────────
# COMANDI
# ──────────────────────────────────────────────────────────────────────────────

def run_trade(config: dict, args: argparse.Namespace) -> None:
    """Avvia il bot (live/paper) o esegue il dry-run / train-only."""
    session = TradingSession(config, args)
    session.start()


def run_dashboard(config: dict, args: argparse.Namespace) -> None:
    """
    Dashboard live: rilegge state_snapshot.json e ridisegna a refresh automatico.

    Con --once disegna un singolo frame ed esce (utile per script/CI).
    """
    # Modalità browser: avvia il server web e termina al Ctrl+C
    if getattr(args, "web", False):
        from monitoring.web_dashboard import serve
        serve(_SNAPSHOT_PATH, port=getattr(args, "port", 8787))
        return

    from monitoring.dashboard import LiveDashboard, dashboard_state_from_dict

    refresh = int(config.get("monitoring", {}).get("dashboard_refresh_seconds", 5))
    once    = getattr(args, "once", False)

    def _read_snapshot() -> Optional[dict]:
        if not _SNAPSHOT_PATH.exists():
            return None
        try:
            return json.loads(_SNAPSHOT_PATH.read_text())
        except Exception:
            return None

    dash = LiveDashboard(refresh_seconds=refresh)

    # Frame singolo
    if once:
        data = _read_snapshot()
        if data is None:
            print("Nessuno snapshot disponibile. Avvia prima il bot con './start.sh dry'.")
            return
        dash.render_once(dashboard_state_from_dict(data))
        return

    # Loop di refresh automatico
    print(f"Dashboard live (refresh ogni {refresh}s). Ctrl+C per uscire.\n")
    dash.start()
    try:
        last_saved = None
        while True:
            data = _read_snapshot()
            if data is None:
                dash.stop()
                print("In attesa dello snapshot… avvia il bot con './start.sh dry' o './start.sh live'.")
                time.sleep(refresh)
                dash.start()
            else:
                # Ridisegna solo se il file è cambiato (evita refresh inutili)
                saved_at = data.get("saved_at")
                if saved_at != last_saved:
                    dash.update(dashboard_state_from_dict(data))
                    last_saved = saved_at
                time.sleep(refresh)
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
        print("\nDashboard chiusa.")


def _print_cost_report(result, bt_conf: dict, timeframe: str) -> None:
    """
    Stampa l'impatto dei costi di spread/slippage: rendimento lordo vs netto.
    Il backtester applica slippage_pct ad ogni fill; qui ne sommiamo il costo.
    """
    trades = result.combined_trades or []
    total_cost = sum(getattr(t, "slippage_cost", 0.0) for t in trades)
    initial    = bt_conf.get("initial_capital", 100_000)
    slippage_pct = bt_conf.get("slippage_pct", 0.0005)

    final_equity = float(result.combined_equity.iloc[-1]) if len(result.combined_equity) else initial
    net_return   = (final_equity - initial) / initial
    # Rendimento "lordo" approssimato = netto + costi reinvestiti sul capitale iniziale
    gross_return = net_return + total_cost / initial

    print("\n" + "─" * 55)
    print("  IMPATTO COSTI (spread/slippage)")
    print("─" * 55)
    print(f"  Timeframe:            {timeframe}")
    print(f"  Costo per trade:      {slippage_pct*100:.3f}%  (spread+slippage simulati)")
    print(f"  Numero di trade:      {len(trades)}")
    print(f"  Costo totale:         ${total_cost:>12,.2f}")
    print(f"  Rendimento LORDO:     {gross_return*100:>+8.2f}%  (senza costi)")
    print(f"  Rendimento NETTO:     {net_return*100:>+8.2f}%  (dopo i costi)")
    print(f"  Erosione da costi:    {(gross_return-net_return)*100:>8.2f} punti %")
    print("─" * 55 + "\n")


def run_backtest(config: dict, args: argparse.Namespace) -> None:
    symbols   = args.symbols
    start     = args.start
    end       = args.end
    out_dir   = Path(args.output_dir)
    synthetic = getattr(args, "synthetic", False)
    # Timeframe: da --timeframe, altrimenti dalla config (1Hour per l'intraday)
    timeframe = getattr(args, "timeframe", None) or config.get("broker", {}).get("timeframe", "1Day")

    if synthetic:
        logger.info("Modalità sintetica: generazione dati artificiali.")
        data = {s: _synthetic_ohlcv(s, start, end) for s in symbols}
    else:
        csv_paths = getattr(args, "data_csv", None)
        try:
            data = _load_data(symbols, start, end, config, csv_paths, timeframe=timeframe)
        except EnvironmentError as e:
            logger.warning("%s\nFallback su dati sintetici.", e)
            data = {s: _synthetic_ohlcv(s, start, end) for s in symbols}

    if not data or all(len(v) == 0 for v in data.values()):
        logger.error("Nessun dato disponibile. Interrotto.")
        sys.exit(1)

    fe      = FeatureEngineer()
    bt_conf = dict(config.get("backtest", {}))
    bt_conf.setdefault("initial_capital", config.get("broker", {}).get("initial_capital", 100_000))

    # Override finestre walk-forward da CLI (necessario per l'intraday: IS >= 504)
    if getattr(args, "train_window", None):
        bt_conf["train_window"] = args.train_window
    if getattr(args, "test_window", None):
        bt_conf["test_window"] = args.test_window
    if getattr(args, "step_size", None):
        bt_conf["step_size"] = args.step_size
    if getattr(args, "slippage_pct", None) is not None:
        bt_conf["slippage_pct"] = args.slippage_pct

    backtester = WalkForwardBacktester(config=bt_conf, feature_engineer=fe)
    result     = backtester.run(
        data=data, symbols=symbols,
        hmm_config=config.get("hmm", {}),
        strategy_config=config.get("strategy", {}),
    )

    if result.combined_equity is None or len(result.combined_equity) == 0:
        logger.error("Backtest non ha prodotto risultati (dati insufficienti?).")
        sys.exit(1)

    logger.info("Backtest completato: %d fold(s), %d ribilanciamenti.", result.n_folds, result.n_total_rebalances)

    combined_regimes = result.combined_regimes if result.combined_regimes is not None else pd.Series(dtype=str)
    combined_probs   = pd.concat([f.regime_probabilities for f in result.folds]).sort_index()
    combined_probs   = combined_probs[~combined_probs.index.duplicated(keep="first")]
    combined_alloc   = pd.concat([f.allocation_series for f in result.folds]).sort_index()
    combined_alloc   = combined_alloc[~combined_alloc.index.duplicated(keep="first")]

    analyzer = PerformanceAnalyzer(config=config)
    metrics  = analyzer.analyze(
        equity=result.combined_equity,
        returns=result.combined_returns,
        regimes=combined_regimes,
        probs=combined_probs,
        allocation=combined_alloc,
        n_rebalances=result.n_total_rebalances,
        ohlcv=data[symbols[0]],
        run_benchmarks=getattr(args, "compare", False),
        initial_capital=bt_conf["initial_capital"],
    )
    analyzer.print_summary(metrics)

    # ── Report costi di spread/slippage (rendimento lordo vs netto) ──
    _print_cost_report(result, bt_conf, timeframe)

    saved = analyzer.export_csv(
        metrics=metrics,
        regimes=combined_regimes,
        probs=combined_probs,
        allocation=combined_alloc,
        trade_log=result.combined_trades,
        output_dir=out_dir,
    )
    logger.info("File salvati: %s", [str(p) for p in saved.values()])

    if getattr(args, "stress_test", False):
        logger.info("Avvio stress test…")
        tester = StressTester(config=config.get("risk", {}))
        stress = tester.run(
            returns_oos=result.combined_returns,
            ohlcv_oos=data[symbols[0]].loc[result.combined_returns.index[0]:],
        )
        tester.print_summary(stress)


def run_stress(config: dict, args: argparse.Namespace) -> None:
    symbols   = args.symbols
    start     = args.start
    end       = args.end
    synthetic = getattr(args, "synthetic", False)

    if synthetic:
        data = {s: _synthetic_ohlcv(s, start, end) for s in symbols}
    else:
        csv_paths = getattr(args, "data_csv", None)
        try:
            data = _load_data(symbols, start, end, config, csv_paths)
        except EnvironmentError:
            data = {s: _synthetic_ohlcv(s, start, end) for s in symbols}

    fe      = FeatureEngineer()
    bt_conf = config.get("backtest", {})
    bt_conf.setdefault("initial_capital", config.get("broker", {}).get("initial_capital", 100_000))

    backtester = WalkForwardBacktester(config=bt_conf, feature_engineer=fe)
    result     = backtester.run(
        data=data, symbols=symbols,
        hmm_config=config.get("hmm", {}),
        strategy_config=config.get("strategy", {}),
    )

    if result.combined_returns is None or len(result.combined_returns) == 0:
        logger.error("Nessun risultato dal backtest, stress test impossibile.")
        sys.exit(1)

    tester = StressTester(config=config.get("risk", {}))
    stress = tester.run(
        returns_oos=result.combined_returns,
        ohlcv_oos=data[symbols[0]],
    )
    tester.print_summary(stress)


# ──────────────────────────────────────────────────────────────────────────────
# PARSER
# ──────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regime Trader — HMM-based volatility allocation bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python main.py trade                                               # paper trading
  python main.py trade --dry-run                                    # test pipeline
  python main.py trade --train-only                                 # solo training HMM
  python main.py backtest --symbols SPY --start 2019-01-01 --end 2024-12-31
  python main.py backtest --symbols SPY QQQ --compare --stress-test
  python main.py stress   --symbols SPY --start 2020-01-01 --end 2024-12-31
  python main.py dashboard                                          # stato snapshot
        """,
    )

    parser.add_argument(
        "mode",
        choices=["trade", "backtest", "stress", "dashboard"],
        help="Modalità di esecuzione",
    )
    parser.add_argument("--config", type=Path, default=Path("config/settings.yaml"), metavar="FILE")
    parser.add_argument("--symbols", nargs="+", default=["SPY"], metavar="SYM")
    parser.add_argument("--start", default="2019-01-01", metavar="YYYY-MM-DD")
    parser.add_argument("--end",   default="2024-12-31", metavar="YYYY-MM-DD")
    parser.add_argument("--compare",     action="store_true", help="Confronta con benchmark")
    parser.add_argument("--stress-test", dest="stress_test", action="store_true",
                        help="Esegui stress test dopo il backtest")
    parser.add_argument("--output-dir",  dest="output_dir", default="results", metavar="DIR")
    parser.add_argument("--data-csv",    dest="data_csv", nargs="+", metavar="FILE")
    parser.add_argument("--synthetic",   action="store_true",
                        help="Usa dati sintetici (test offline)")
    parser.add_argument("--timeframe",   default=None, metavar="TF",
                        help="Timeframe barre per il backtest (es. 1Hour, 1Day). Default: dalla config")
    parser.add_argument("--train-window", dest="train_window", type=int, default=None,
                        help="Backtest: barre di training per fold (intraday: >=504, es. 756)")
    parser.add_argument("--test-window",  dest="test_window", type=int, default=None,
                        help="Backtest: barre di test per fold")
    parser.add_argument("--step",         dest="step_size", type=int, default=None,
                        help="Backtest: avanzamento finestra tra i fold")
    parser.add_argument("--slippage",     dest="slippage_pct", type=float, default=None,
                        help="Backtest: costo per trade in frazione (es. 0.0001 = 0.01%% spread realistico SPY)")
    parser.add_argument("--dry-run",     dest="dry_run", action="store_true",
                        help="Pipeline completa senza inviare ordini reali")
    parser.add_argument("--train-only",  dest="train_only", action="store_true",
                        help="Addestra HMM e salva il modello, poi esce")
    parser.add_argument("--wait-for-open", dest="wait_for_open", action="store_true",
                        help="Attendi l'apertura del mercato invece di uscire")
    parser.add_argument("--keep-positions", dest="keep_positions", action="store_true",
                        help="NON chiudere le posizioni allo spegnimento (lasciale protette dagli stop)")
    parser.add_argument("--once", action="store_true",
                        help="Dashboard: disegna un singolo frame ed esci (no refresh)")
    parser.add_argument("--web", action="store_true",
                        help="Dashboard: avvia la versione browser (server web locale)")
    parser.add_argument("--port", type=int, default=8787,
                        help="Porta del server web della dashboard (default: 8787)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log verboso (DEBUG)")

    return parser


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    parser = _build_parser()
    args   = parser.parse_args()

    _setup_logging(verbose=args.verbose)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.error("File di configurazione non trovato: %s", args.config)
        sys.exit(1)

    dispatch = {
        "trade":     run_trade,
        "backtest":  run_backtest,
        "stress":    run_stress,
        "dashboard": run_dashboard,
    }

    try:
        dispatch[args.mode](config, args)
    except KeyboardInterrupt:
        logger.info("Interruzione utente.")
        sys.exit(0)
    except Exception as exc:
        logger.critical("Errore fatale: %s", exc, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
