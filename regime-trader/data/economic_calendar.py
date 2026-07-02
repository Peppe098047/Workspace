"""
Economic Calendar — blackout sugli annunci macro ad alto impatto (Forex Factory).

PERCHÉ: attorno a CPI, NFP, decisioni FOMC il mercato fa movimenti violenti e
imprevedibili in pochi secondi. Comprare 30 secondi prima del dato significa
farsi spazzare lo stop dal caos dell'annuncio, non dal trend. Il bot quindi
NON apre nuove posizioni nella finestra attorno a questi eventi (stop, trailing
e square-off restano pienamente attivi, come nel blackout d'apertura).

FONTE: feed JSON gratuito del calendario settimanale di Forex Factory
(https://nfs.faireconomy.media/ff_calendar_thisweek.json) — nessuna API key.
Campi: title, country, date (ISO con offset), impact, forecast, previous.
Il feed viene scaricato all'avvio e salvato in cache su disco: se il download
fallisce si usa la cache; se manca anche quella, il filtro è disattivo (fail-open
con warning — il bot continua a operare come prima del calendario).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("regime-trader.calendar")

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
DEFAULT_CACHE_PATH = Path("data/cache/ff_calendar.json")


@dataclass
class CalendarEvent:
    """Un annuncio macro rilevante."""
    title: str
    currency: str
    impact: str
    when: datetime          # timezone-aware (UTC)

    def __str__(self) -> str:
        return f"{self.title} ({self.currency}, {self.impact}) @ {self.when:%H:%M} UTC"


class EconomicCalendar:
    """
    Scarica il calendario, filtra gli eventi rilevanti e risponde alla domanda:
    "siamo dentro la finestra di blackout di un annuncio macro?"

    Config (sezione `calendar` di settings.yaml):
      enabled:                 attiva/disattiva (default false)
      blackout_minutes_before: minuti di blackout PRIMA dell'annuncio (default 15)
      blackout_minutes_after:  minuti di blackout DOPO l'annuncio (default 15)
      currencies:              valute rilevanti (default [USD])
      impacts:                 livelli di impatto rilevanti (default [High])
    """

    def __init__(self, config: dict, cache_path: Path | None = None) -> None:
        cfg = config.get("calendar", {}) or {}
        self.enabled        = bool(cfg.get("enabled", False))
        self.before_minutes = float(cfg.get("blackout_minutes_before", 15))
        self.after_minutes  = float(cfg.get("blackout_minutes_after", 15))
        self.currencies     = {c.upper() for c in cfg.get("currencies", ["USD"])}
        self.impacts        = {i.capitalize() for i in cfg.get("impacts", ["High"])}
        self.cache_path     = Path(cache_path or DEFAULT_CACHE_PATH)
        self._events: list[CalendarEvent] = []
        self._loaded = False

    # ──────────────────────────────────────────────────────────────────────
    # Caricamento
    # ──────────────────────────────────────────────────────────────────────

    def load(self) -> int:
        """
        Scarica il feed (con fallback alla cache su disco) e filtra gli eventi.
        Ritorna il numero di eventi rilevanti caricati. Non solleva mai.
        """
        if not self.enabled:
            return 0

        raw = self._download() or self._read_cache()
        if raw is None:
            logger.warning(
                "Calendario macro non disponibile (né feed né cache): "
                "filtro eventi DISATTIVO per questa sessione."
            )
            self._events = []
            self._loaded = False
            return 0

        self._events = self._parse(raw)
        self._loaded = True
        logger.info(
            "Calendario macro: %d eventi rilevanti (%s, impatto %s) questa settimana.",
            len(self._events),
            "/".join(sorted(self.currencies)),
            "/".join(sorted(self.impacts)),
        )
        return len(self._events)

    def _download(self) -> Optional[list]:
        try:
            # Il server rifiuta lo User-Agent di default di Python (403):
            # serve un'intestazione da browser.
            req = urllib.request.Request(
                FEED_URL,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) regime-trader/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.load(resp)
            # Aggiorna la cache per le sessioni future
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(raw), encoding="utf-8")
            except Exception as exc:
                logger.debug("Cache calendario non scritta: %s", exc)
            return raw
        except Exception as exc:
            logger.warning("Download calendario fallito: %s — provo la cache.", exc)
            return None

    def _read_cache(self) -> Optional[list]:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Cache calendario illeggibile: %s", exc)
        return None

    def _parse(self, raw: list) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for item in raw or []:
            try:
                if str(item.get("country", "")).upper() not in self.currencies:
                    continue
                if str(item.get("impact", "")).capitalize() not in self.impacts:
                    continue
                when = datetime.fromisoformat(item["date"])
                if when.tzinfo is None:      # difesa: il feed include l'offset
                    when = when.replace(tzinfo=timezone.utc)
                events.append(CalendarEvent(
                    title=str(item.get("title", "?")),
                    currency=str(item.get("country", "?")).upper(),
                    impact=str(item.get("impact", "?")),
                    when=when.astimezone(timezone.utc),
                ))
            except Exception:
                continue   # un evento malformato non deve rompere il calendario
        events.sort(key=lambda e: e.when)
        return events

    # ──────────────────────────────────────────────────────────────────────
    # Query
    # ──────────────────────────────────────────────────────────────────────

    def in_blackout(self, now: Optional[datetime] = None) -> Optional[CalendarEvent]:
        """
        Ritorna l'evento che impone il blackout in questo momento, None se nessuno.
        Finestra: [when − before_minutes, when + after_minutes].
        """
        if not self.enabled or not self._events:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        for ev in self._events:
            start = ev.when - timedelta(minutes=self.before_minutes)
            end   = ev.when + timedelta(minutes=self.after_minutes)
            if start <= now <= end:
                return ev
            if start > now:     # eventi ordinati: oltre questo sono tutti futuri
                break
        return None

    def next_event(self, now: Optional[datetime] = None) -> Optional[CalendarEvent]:
        """Il prossimo evento rilevante (per log e dashboard)."""
        if not self._events:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        for ev in self._events:
            if ev.when >= now:
                return ev
        return None
