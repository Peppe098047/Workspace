"""
Alert Manager — notifiche per eventi critici del sistema di trading.

Trigger supportati (AlertType):
  - REGIME_CHANGE      cambio di regime confermato
  - CIRCUIT_BREAKER    attivazione di un circuit breaker
  - LARGE_PNL          P&L giornaliero oltre una soglia (positivo o negativo)
  - DATA_FEED_DOWN     interruzione del feed dati WebSocket
  - API_LOST           perdita di connessione con Alpaca
  - HMM_RETRAINED      retraining del modello HMM completato
  - FLICKER_EXCEEDED   flicker rate del regime sopra soglia

Canali di delivery:
  - console (sempre)
  - log file (alerts.log via logger dedicato)
  - email (opzionale, via SMTP)
  - webhook (opzionale, Slack/Discord)
  - Telegram (opzionale: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)

Rate limit: 1 alert per tipo di evento ogni `rate_limit_minutes` (default 15).
"""

from __future__ import annotations

import json
import smtplib
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from enum import Enum
from typing import Optional

from monitoring.logger import LOGGER_ALERTS, get_logger


class AlertSeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


class AlertType(str, Enum):
    REGIME_CHANGE    = "regime_change"
    CIRCUIT_BREAKER  = "circuit_breaker"
    LARGE_PNL        = "large_pnl"
    DATA_FEED_DOWN   = "data_feed_down"
    API_LOST         = "api_lost"
    HMM_RETRAINED    = "hmm_retrained"
    FLICKER_EXCEEDED = "flicker_exceeded"
    SESSION_SUMMARY  = "session_summary"
    TRADE            = "trade"


# Severità di default associata a ciascun tipo di evento
_DEFAULT_SEVERITY: dict[AlertType, AlertSeverity] = {
    AlertType.REGIME_CHANGE:    AlertSeverity.INFO,
    AlertType.CIRCUIT_BREAKER:  AlertSeverity.CRITICAL,
    AlertType.LARGE_PNL:        AlertSeverity.WARNING,
    AlertType.DATA_FEED_DOWN:   AlertSeverity.CRITICAL,
    AlertType.API_LOST:         AlertSeverity.CRITICAL,
    AlertType.HMM_RETRAINED:    AlertSeverity.INFO,
    AlertType.FLICKER_EXCEEDED: AlertSeverity.WARNING,
    AlertType.SESSION_SUMMARY:  AlertSeverity.INFO,
    AlertType.TRADE:            AlertSeverity.INFO,
}

# Icone per gli eventi di trading (notifiche Telegram/webhook)
_TRADE_ICON: dict[str, str] = {
    "BUY":   "🟢",
    "SELL":  "🔴",
    "CLOSE": "🔵",
    "STOP":  "🛡",
}

# Icone per severità (console / webhook)
_SEVERITY_ICON: dict[AlertSeverity, str] = {
    AlertSeverity.INFO:     "ℹ️",
    AlertSeverity.WARNING:  "⚠️",
    AlertSeverity.CRITICAL: "🚨",
}


@dataclass
class Alert:
    """Rappresenta un singolo evento di alert."""
    alert_type: AlertType
    severity:   AlertSeverity
    title:      str
    message:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    context:    dict = field(default_factory=dict)

    def format_line(self) -> str:
        icon = _SEVERITY_ICON.get(self.severity, "")
        ts   = self.timestamp.strftime("%H:%M:%S")
        return f"{icon} [{ts}] {self.title}: {self.message}"


class AlertManager:
    """
    Gestisce trigger, delivery multi-canale e rate limiting degli alert.
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        email_to: Optional[str] = None,
        smtp_config: Optional[dict] = None,
        rate_limit_minutes: int = 15,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
    ) -> None:
        """
        Args:
            webhook_url:        URL webhook (Slack/Discord) — None per disabilitare
            email_to:           Indirizzo email destinatario — None per disabilitare
            smtp_config:        {host, port, user, password, from} per l'invio email
            rate_limit_minutes: Cooldown minimo per tipo di evento
            telegram_token:     Token del bot Telegram (da @BotFather) — None per disabilitare
            telegram_chat_id:   Chat ID destinatario dei messaggi Telegram
        """
        self.webhook_url        = webhook_url
        self.email_to           = email_to
        self.smtp_config        = smtp_config or {}
        self.rate_limit_minutes = rate_limit_minutes
        self.telegram_token     = telegram_token
        self.telegram_chat_id   = telegram_chat_id

        self._logger    = get_logger(LOGGER_ALERTS)
        self._last_sent: dict[AlertType, datetime] = {}
        self._history:   list[Alert] = []

    # ─── Trigger di alto livello (uno per evento) ──────────────────────────

    def regime_change(self, old_regime: str, new_regime: str, probability: float) -> bool:
        return self.trigger(
            AlertType.REGIME_CHANGE,
            title="Cambio di regime",
            message=f"{old_regime} → {new_regime} (conf {probability:.0%})",
            context={"old": old_regime, "new": new_regime, "probability": probability},
        )

    def circuit_breaker(self, level: str, drawdown: float) -> bool:
        return self.trigger(
            AlertType.CIRCUIT_BREAKER,
            title="Circuit breaker attivato",
            message=f"Livello {level} — drawdown {drawdown:.1%}",
            context={"level": level, "drawdown": drawdown},
        )

    def large_pnl(self, daily_pnl: float, daily_pnl_pct: float) -> bool:
        direction = "guadagno" if daily_pnl >= 0 else "perdita"
        return self.trigger(
            AlertType.LARGE_PNL,
            title=f"P&L giornaliero rilevante ({direction})",
            message=f"${daily_pnl:+,.2f} ({daily_pnl_pct:+.2%})",
            context={"daily_pnl": daily_pnl, "daily_pnl_pct": daily_pnl_pct},
        )

    def data_feed_down(self, detail: str = "") -> bool:
        return self.trigger(
            AlertType.DATA_FEED_DOWN,
            title="Feed dati interrotto",
            message=detail or "Nessuna barra ricevuta entro il timeout previsto.",
        )

    def api_lost(self, detail: str = "") -> bool:
        return self.trigger(
            AlertType.API_LOST,
            title="Connessione API persa",
            message=detail or "Impossibile raggiungere Alpaca.",
        )

    def hmm_retrained(self, n_states: int, bic: float) -> bool:
        return self.trigger(
            AlertType.HMM_RETRAINED,
            title="HMM riaddestrato",
            message=f"{n_states} stati — BIC {bic:.1f}",
            context={"n_states": n_states, "bic": bic},
        )

    def flicker_exceeded(self, flicker_rate: float, threshold: float) -> bool:
        return self.trigger(
            AlertType.FLICKER_EXCEEDED,
            title="Flicker rate elevato",
            message=f"{flicker_rate:.0f} cambi (soglia {threshold:.0f}) — modalità incertezza",
            context={"flicker_rate": flicker_rate, "threshold": threshold},
        )

    def trade_event(self, kind: str, symbol: str, detail: str = "") -> bool:
        """
        Notifica un evento di trading (BUY/SELL/CLOSE/STOP).
        NON soggetto a rate limit: i fill consecutivi devono arrivare tutti.
        """
        icon = _TRADE_ICON.get(kind.upper(), "•")
        return self.trigger(
            AlertType.TRADE,
            title=f"{icon} {kind.upper()} {symbol}",
            message=detail or "—",
            context={"kind": kind, "symbol": symbol},
        )

    def session_summary(
        self,
        pnl: float,
        pnl_pct: float,
        equity: float,
        n_orders: int,
        duration: str,
        clean_shutdown: bool,
    ) -> bool:
        """Riepilogo di fine sessione (inviato allo shutdown del bot)."""
        shutdown_str = "shutdown pulito" if clean_shutdown else "⚠️ ARRESTO DA ERRORE"
        return self.trigger(
            AlertType.SESSION_SUMMARY,
            title="Sessione terminata",
            message=(
                f"P&L {pnl:+,.2f}$ ({pnl_pct:+.2f}%) | equity ${equity:,.2f} | "
                f"{n_orders} ordini | durata {duration} | {shutdown_str}"
            ),
            context={"pnl": pnl, "pnl_pct": pnl_pct, "equity": equity,
                     "orders": n_orders, "clean": clean_shutdown},
        )

    # ─── Motore di trigger ─────────────────────────────────────────────────

    def trigger(
        self,
        alert_type: AlertType,
        title: str,
        message: str,
        severity: Optional[AlertSeverity] = None,
        context: Optional[dict] = None,
    ) -> bool:
        """
        Costruisce e invia un alert se non bloccato dal rate limit.

        Returns:
            True se inviato, False se soppresso dal cooldown.
        """
        severity = severity or _DEFAULT_SEVERITY.get(alert_type, AlertSeverity.INFO)
        alert = Alert(
            alert_type=alert_type,
            severity=severity,
            title=title,
            message=message,
            context=context or {},
        )
        return self.send(alert)

    def send(self, alert: Alert) -> bool:
        """Invia un alert su tutti i canali configurati, salvo rate limit."""
        if self._is_rate_limited(alert.alert_type):
            return False

        self._last_sent[alert.alert_type] = alert.timestamp
        self._history.append(alert)

        # Canale 1: console
        self._send_console(alert)
        # Canale 2: log file dedicato
        self._send_log(alert)
        # Canale 3: webhook (opzionale)
        if self.webhook_url:
            self._send_webhook(alert)
        # Canale 4: email (opzionale, solo WARNING/CRITICAL)
        if self.email_to and alert.severity != AlertSeverity.INFO:
            self._send_email(alert)
        # Canale 5: Telegram (opzionale)
        if self.telegram_token and self.telegram_chat_id:
            self._send_telegram(alert)

        return True

    # ─── Canali di delivery ────────────────────────────────────────────────

    def _send_console(self, alert: Alert) -> None:
        print(alert.format_line(), file=sys.stderr)

    def _send_log(self, alert: Alert) -> None:
        log_fn = {
            AlertSeverity.INFO:     self._logger.info,
            AlertSeverity.WARNING:  self._logger.warning,
            AlertSeverity.CRITICAL: self._logger.critical,
        }[alert.severity]
        log_fn(
            f"{alert.title}: {alert.message}",
            extra={"context": {"alert_type": alert.alert_type.value, **alert.context}},
        )

    def _send_webhook(self, alert: Alert) -> None:
        """Invia l'alert al webhook (formato Slack/Discord compatibile)."""
        icon = _SEVERITY_ICON.get(alert.severity, "")
        payload = {
            "text": f"{icon} *{alert.title}*\n{alert.message}",
            "username": "regime-trader",
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req  = urllib.request.Request(
                self.webhook_url, data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            self._logger.warning("Invio webhook fallito: %s", exc)

    def _send_telegram(self, alert: Alert) -> None:
        """Invia l'alert al bot Telegram (API sendMessage, zero dipendenze)."""
        icon = _SEVERITY_ICON.get(alert.severity, "")
        text = f"{icon} {alert.title}\n{alert.message}"
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": text}
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            self._logger.warning("Invio Telegram fallito: %s", exc)

    def _send_email(self, alert: Alert) -> None:
        """Invia l'alert via SMTP usando smtp_config."""
        cfg = self.smtp_config
        if not all(k in cfg for k in ("host", "port", "from")):
            self._logger.warning("smtp_config incompleto, email non inviata.")
            return

        msg = EmailMessage()
        msg["Subject"] = f"[regime-trader] {alert.severity.value.upper()}: {alert.title}"
        msg["From"]    = cfg["from"]
        msg["To"]      = self.email_to
        msg.set_content(
            f"{alert.title}\n\n{alert.message}\n\n"
            f"Timestamp: {alert.timestamp.isoformat()}\n"
            f"Context: {json.dumps(alert.context, indent=2, default=str)}"
        )
        try:
            with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=10) as server:
                if cfg.get("use_tls", True):
                    server.starttls()
                if cfg.get("user") and cfg.get("password"):
                    server.login(cfg["user"], cfg["password"])
                server.send_message(msg)
        except Exception as exc:
            self._logger.warning("Invio email fallito: %s", exc)

    # ─── Rate limiting ─────────────────────────────────────────────────────

    def _is_rate_limited(self, alert_type: AlertType) -> bool:
        """True se un alert dello stesso tipo è stato inviato dentro il cooldown."""
        if alert_type == AlertType.TRADE:
            return False   # i fill consecutivi devono arrivare TUTTI
        last = self._last_sent.get(alert_type)
        if last is None:
            return False
        cooldown = timedelta(minutes=self.rate_limit_minutes)
        return (datetime.now() - last) < cooldown

    # ─── Query ─────────────────────────────────────────────────────────────

    def get_history(self, limit: int = 20) -> list[Alert]:
        """Restituisce gli ultimi `limit` alert inviati."""
        return self._history[-limit:]

    def reset_rate_limits(self) -> None:
        """Azzera tutti i cooldown (utile per i test)."""
        self._last_sent.clear()
