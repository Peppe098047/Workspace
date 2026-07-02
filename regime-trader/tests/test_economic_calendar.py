"""
Test per il calendario macro (data/economic_calendar.py): parsing del feed
Forex Factory, finestre di blackout, fallback su cache e fail-open.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from data.economic_calendar import EconomicCalendar, FEED_URL

CONFIG = {
    "calendar": {
        "enabled": True,
        "blackout_minutes_before": 15,
        "blackout_minutes_after": 15,
        "currencies": ["USD"],
        "impacts": ["High"],
    }
}

# Feed sintetico in formato Forex Factory (date ISO con offset ET)
FEED = [
    {"title": "CPI m/m", "country": "USD", "impact": "High",
     "date": "2026-06-11T08:30:00-04:00", "forecast": "0.2%", "previous": "0.3%"},
    {"title": "FOMC Statement", "country": "USD", "impact": "High",
     "date": "2026-06-11T14:00:00-04:00", "forecast": "", "previous": ""},
    {"title": "German ZEW", "country": "EUR", "impact": "High",          # valuta sbagliata
     "date": "2026-06-11T05:00:00-04:00", "forecast": "", "previous": ""},
    {"title": "Crude Oil Inventories", "country": "USD", "impact": "Medium",  # impatto basso
     "date": "2026-06-11T10:30:00-04:00", "forecast": "", "previous": ""},
]


def _calendar(tmp_path, feed=FEED) -> EconomicCalendar:
    cal = EconomicCalendar(CONFIG, cache_path=tmp_path / "ff.json")
    with patch.object(cal, "_download", return_value=feed):
        cal.load()
    return cal


class TestParsing:

    def test_filters_currency_and_impact(self, tmp_path) -> None:
        cal = _calendar(tmp_path)
        titles = [e.title for e in cal._events]
        assert titles == ["CPI m/m", "FOMC Statement"]   # EUR e Medium esclusi

    def test_dates_converted_to_utc(self, tmp_path) -> None:
        cal = _calendar(tmp_path)
        cpi = cal._events[0]
        # 08:30 ET (-04:00) = 12:30 UTC
        assert cpi.when == datetime(2026, 6, 11, 12, 30, tzinfo=timezone.utc)

    def test_malformed_event_skipped(self, tmp_path) -> None:
        feed = FEED + [{"title": "rotto", "country": "USD", "impact": "High", "date": "non-una-data"}]
        cal = _calendar(tmp_path, feed=feed)
        assert all(e.title != "rotto" for e in cal._events)


class TestBlackoutWindow:

    @pytest.mark.parametrize("now_utc,expected", [
        (datetime(2026, 6, 11, 12, 14, tzinfo=timezone.utc), None),        # 16 min prima
        (datetime(2026, 6, 11, 12, 16, tzinfo=timezone.utc), "CPI m/m"),   # 14 min prima
        (datetime(2026, 6, 11, 12, 30, tzinfo=timezone.utc), "CPI m/m"),   # all'annuncio
        (datetime(2026, 6, 11, 12, 44, tzinfo=timezone.utc), "CPI m/m"),   # 14 min dopo
        (datetime(2026, 6, 11, 12, 46, tzinfo=timezone.utc), None),        # 16 min dopo
        (datetime(2026, 6, 11, 17, 55, tzinfo=timezone.utc), "FOMC Statement"),  # pre-FOMC
    ])
    def test_window_boundaries(self, tmp_path, now_utc, expected) -> None:
        cal = _calendar(tmp_path)
        ev = cal.in_blackout(now=now_utc)
        assert (ev.title if ev else None) == expected

    def test_next_event(self, tmp_path) -> None:
        cal = _calendar(tmp_path)
        nxt = cal.next_event(now=datetime(2026, 6, 11, 13, 0, tzinfo=timezone.utc))
        assert nxt.title == "FOMC Statement"

    def test_disabled_never_blackout(self, tmp_path) -> None:
        config = {"calendar": {**CONFIG["calendar"], "enabled": False}}
        cal = EconomicCalendar(config, cache_path=tmp_path / "ff.json")
        cal.load()
        assert cal.in_blackout(now=datetime(2026, 6, 11, 12, 30, tzinfo=timezone.utc)) is None


class TestResilience:

    def test_fallback_to_cache(self, tmp_path) -> None:
        """Download giù → si usa la cache scritta in una sessione precedente."""
        cache = tmp_path / "ff.json"
        cache.write_text(json.dumps(FEED))
        cal = EconomicCalendar(CONFIG, cache_path=cache)

        with patch("data.economic_calendar.urllib.request.urlopen",
                   side_effect=OSError("rete giù")):
            n = cal.load()

        assert n == 2   # caricati dalla cache

    def test_fail_open_without_feed_and_cache(self, tmp_path) -> None:
        """Né feed né cache → filtro spento, il bot continua a operare."""
        cal = EconomicCalendar(CONFIG, cache_path=tmp_path / "assente.json")

        with patch("data.economic_calendar.urllib.request.urlopen",
                   side_effect=OSError("rete giù")):
            n = cal.load()

        assert n == 0
        assert cal.in_blackout(now=datetime(2026, 6, 11, 12, 30, tzinfo=timezone.utc)) is None

    def test_download_writes_cache(self, tmp_path) -> None:
        cal = _calendar(tmp_path)
        # _download è mockato in _calendar, quindi la cache la scrive load()?
        # No: la cache la scrive _download reale. Qui verifichiamo il percorso reale:
        cal2 = EconomicCalendar(CONFIG, cache_path=tmp_path / "ff2.json")

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(FEED).encode()

        with patch("data.economic_calendar.urllib.request.urlopen", return_value=_Resp()):
            n = cal2.load()

        assert n == 2
        assert (tmp_path / "ff2.json").exists()
