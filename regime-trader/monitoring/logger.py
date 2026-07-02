"""
Logger — logging strutturato JSON con rotazione su file.

Quattro stream separati, ciascuno con rotazione (10 MB, 30 backup):
  - main.log    → tutti gli eventi applicativi
  - trades.log  → solo esecuzioni di ordini e fill
  - alerts.log  → solo alert (regime change, circuit breaker, ecc.)
  - regime.log  → solo transizioni e predizioni del regime HMM

Ogni record JSON include il contesto di stato corrente quando disponibile:
  timestamp, regime, probability, equity, positions, daily_pnl.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Configurazione rotazione: 10 MB per file, 30 backup (~30 giorni di operatività)
_MAX_BYTES    = 10 * 1024 * 1024
_BACKUP_COUNT = 30
_LOG_DIR      = Path("logs")

# Nomi dei logger dedicati per ogni stream
LOGGER_MAIN   = "regime-trader"
LOGGER_TRADES = "regime-trader.trades"
LOGGER_ALERTS = "regime-trader.alerts"
LOGGER_REGIME = "regime-trader.regime"

_configured = False

# Stato globale del sistema iniettato in ogni record di log (aggiornato dal main loop)
_global_context: dict = {
    "regime":      None,
    "probability": None,
    "equity":      None,
    "positions":   None,
    "daily_pnl":   None,
}


def set_log_context(
    *,
    regime: Optional[str] = None,
    probability: Optional[float] = None,
    equity: Optional[float] = None,
    positions: Optional[int] = None,
    daily_pnl: Optional[float] = None,
) -> None:
    """
    Aggiorna il contesto globale iniettato in ogni record JSON.
    Chiamare dal main loop ad ogni barra per tenere i log allineati allo stato.
    """
    if regime is not None:
        _global_context["regime"] = regime
    if probability is not None:
        _global_context["probability"] = round(probability, 4)
    if equity is not None:
        _global_context["equity"] = round(equity, 2)
    if positions is not None:
        _global_context["positions"] = positions
    if daily_pnl is not None:
        _global_context["daily_pnl"] = round(daily_pnl, 2)


# Campi di sistema che il context non deve sovrascrivere
_RESERVED_KEYS = frozenset({"timestamp", "level", "logger", "message", "exception"})


class JsonFormatter(logging.Formatter):
    """Formatta ogni record come una riga JSON con il contesto di stato corrente."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
        }

        # Inietta il contesto globale di stato (solo i campi valorizzati)
        for key, value in _global_context.items():
            if value is not None:
                payload[key] = value

        # Campi extra via logger.info(..., extra={"context": {...}}).
        # I campi di sistema sono riservati: il context non può sovrascriverli.
        ctx = getattr(record, "context", None)
        if isinstance(ctx, dict):
            for key, value in ctx.items():
                if key in _RESERVED_KEYS:
                    payload[f"ctx_{key}"] = value   # rinomina per non perdere il dato
                else:
                    payload[key] = value

        # Traceback se presente
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _make_file_handler(path: Path, level: int) -> RotatingFileHandler:
    """Crea un RotatingFileHandler con formato JSON."""
    handler = RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    return handler


def setup_logging(
    log_dir: Path = _LOG_DIR,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """
    Configura i quattro stream di logging con rotazione JSON.

    Idempotente: chiamate successive non duplicano gli handler.

    Args:
        log_dir: Directory dei file di log (creata se non esiste)
        level:   Livello minimo di logging
        console: Se True aggiunge anche output colorato su terminale

    Returns:
        Il logger principale (regime-trader).
    """
    global _configured

    main_logger = logging.getLogger(LOGGER_MAIN)

    if _configured:
        return main_logger

    log_dir.mkdir(parents=True, exist_ok=True)

    # ── Logger principale ──────────────────────────────────────────────────
    main_logger.setLevel(level)
    main_logger.propagate = False
    main_logger.addHandler(_make_file_handler(log_dir / "main.log", level))

    if console:
        try:
            from rich.logging import RichHandler
            rich_handler = RichHandler(rich_tracebacks=True, markup=False)
            rich_handler.setLevel(level)
            main_logger.addHandler(rich_handler)
        except ImportError:
            stream = logging.StreamHandler(sys.stderr)
            stream.setLevel(level)
            stream.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
            )
            main_logger.addHandler(stream)

    # ── Logger dedicati (file proprio + propagazione a main per console) ───
    for logger_name, filename in (
        (LOGGER_TRADES, "trades.log"),
        (LOGGER_ALERTS, "alerts.log"),
        (LOGGER_REGIME, "regime.log"),
    ):
        dedicated = logging.getLogger(logger_name)
        dedicated.setLevel(level)
        dedicated.propagate = True  # propaga a main per averli anche in main.log/console
        dedicated.addHandler(_make_file_handler(log_dir / filename, level))

    _configured = True
    return main_logger


def get_logger(name: str = LOGGER_MAIN, level: int = logging.INFO) -> logging.Logger:
    """
    Restituisce un logger configurato. Se il sistema di logging non è ancora
    stato inizializzato, lo configura con i default.
    """
    if not _configured:
        setup_logging(level=level)
    return logging.getLogger(name)


def log_trade(message: str, **context) -> None:
    """Shortcut per loggare un evento di trade sul logger dedicato."""
    get_logger(LOGGER_TRADES).info(message, extra={"context": context})


def log_regime(message: str, **context) -> None:
    """Shortcut per loggare una transizione di regime sul logger dedicato."""
    get_logger(LOGGER_REGIME).info(message, extra={"context": context})


def _reset_for_tests() -> None:
    """Resetta lo stato del modulo (solo per i test)."""
    global _configured
    for name in (LOGGER_MAIN, LOGGER_TRADES, LOGGER_ALERTS, LOGGER_REGIME):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)
    for key in _global_context:
        _global_context[key] = None
    _configured = False
