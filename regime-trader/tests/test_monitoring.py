"""
Test per il package monitoring: logger JSON, alert manager, dashboard.
"""
import json
import logging
from datetime import datetime

import pytest

from monitoring.logger import (
    JsonFormatter,
    setup_logging,
    set_log_context,
    get_logger,
    log_trade,
    _reset_for_tests,
    LOGGER_TRADES,
)
from monitoring.alerts import AlertManager, AlertType, AlertSeverity, Alert
from monitoring.dashboard import (
    LiveDashboard,
    DashboardState,
    _risk_bar,
    dashboard_state_from_dict,
)
from broker.position_tracker import Position, PortfolioSnapshot
from core.hmm_engine import RegimeState


# ──────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_logging():
    """Resetta lo stato del modulo logger prima e dopo ogni test."""
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def regime_state() -> RegimeState:
    return RegimeState(
        label="BULL",
        state_id=0,
        probability=0.72,
        state_probabilities=[0.72, 0.20, 0.08],
        timestamp=datetime.now(),
        is_confirmed=True,
        consecutive_bars=14,
    )


@pytest.fixture
def snapshot() -> PortfolioSnapshot:
    pos = Position(
        symbol="SPY", qty=10.0, avg_entry_price=514.0, current_price=520.30,
        market_value=5203.0, unrealized_pnl=63.0, unrealized_pnl_pct=0.012,
        side="long", opened_at=datetime.now(), stop_level=508.0,
        regime_at_entry="BULL", current_regime="BULL", holding_bars=3,
    )
    return PortfolioSnapshot(
        total_value=105230.0,
        cash=50000.0,
        positions={"SPY": pos},
        total_unrealized_pnl=63.0,
        timestamp=datetime.now(),
    )


# ──────────────────────────────────────────────────────────────────────────────
# LOGGER
# ──────────────────────────────────────────────────────────────────────────────

class TestJsonFormatter:
    def test_output_is_valid_json(self) -> None:
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="regime-trader", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )
        result = fmt.format(record)
        parsed = json.loads(result)
        assert parsed["message"] == "test message"
        assert parsed["level"] == "INFO"
        assert "timestamp" in parsed

    def test_includes_global_context(self) -> None:
        set_log_context(regime="BULL", probability=0.72, equity=105230.0, positions=1, daily_pnl=340.0)
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="regime-trader", level=logging.INFO, pathname="", lineno=0,
            msg="ciao", args=(), exc_info=None,
        )
        parsed = json.loads(fmt.format(record))
        assert parsed["regime"] == "BULL"
        assert parsed["probability"] == 0.72
        assert parsed["equity"] == 105230.0
        assert parsed["positions"] == 1
        assert parsed["daily_pnl"] == 340.0

    def test_extra_context_merged(self) -> None:
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        record.context = {"symbol": "SPY", "shares": 10}
        parsed = json.loads(fmt.format(record))
        assert parsed["symbol"] == "SPY"
        assert parsed["shares"] == 10

    def test_context_cannot_override_reserved_fields(self) -> None:
        """Una chiave 'level' nel context non deve sovrascrivere il livello di log."""
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="x", level=logging.CRITICAL, pathname="", lineno=0,
            msg="m", args=(), exc_info=None,
        )
        record.context = {"level": "DAILY_REDUCE", "message": "fake"}
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "CRITICAL"        # campo di sistema preservato
        assert parsed["message"] == "m"             # non sovrascritto
        assert parsed["ctx_level"] == "DAILY_REDUCE"  # valore del context rinominato
        assert parsed["ctx_message"] == "fake"


class TestSetupLogging:
    def test_creates_log_files(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        get_logger().info("avvio")
        log_trade("ordine eseguito", symbol="SPY")

        assert (tmp_path / "main.log").exists()
        assert (tmp_path / "trades.log").exists()
        assert (tmp_path / "alerts.log").exists()
        assert (tmp_path / "regime.log").exists()

    def test_idempotent(self, tmp_path) -> None:
        l1 = setup_logging(log_dir=tmp_path, console=False)
        n_handlers = len(l1.handlers)
        l2 = setup_logging(log_dir=tmp_path, console=False)
        assert len(l2.handlers) == n_handlers  # nessun handler duplicato

    def test_trade_log_is_json(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        log_trade("fill", symbol="SPY", price=450.0)
        content = (tmp_path / "trades.log").read_text().strip()
        parsed = json.loads(content.splitlines()[-1])
        assert parsed["symbol"] == "SPY"
        assert parsed["price"] == 450.0


# ──────────────────────────────────────────────────────────────────────────────
# ALERTS
# ──────────────────────────────────────────────────────────────────────────────

class TestAlertManager:
    def test_trigger_sends_first_alert(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        mgr = AlertManager(rate_limit_minutes=15)
        sent = mgr.regime_change("BEAR", "BULL", 0.80)
        assert sent is True

    def test_rate_limit_blocks_second_same_type(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        mgr = AlertManager(rate_limit_minutes=15)
        assert mgr.regime_change("BEAR", "BULL", 0.80) is True
        assert mgr.regime_change("BULL", "NEUTRAL", 0.65) is False  # bloccato

    def test_different_types_not_rate_limited(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        mgr = AlertManager(rate_limit_minutes=15)
        assert mgr.regime_change("BEAR", "BULL", 0.80) is True
        assert mgr.circuit_breaker("DAILY_HALT", 0.03) is True  # tipo diverso → passa

    def test_reset_rate_limits(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        mgr = AlertManager(rate_limit_minutes=15)
        mgr.regime_change("BEAR", "BULL", 0.80)
        mgr.reset_rate_limits()
        assert mgr.regime_change("BULL", "BEAR", 0.60) is True

    def test_default_severity_circuit_breaker_is_critical(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        mgr = AlertManager()
        mgr.circuit_breaker("PEAK_HALT", 0.10)
        assert mgr.get_history()[-1].severity == AlertSeverity.CRITICAL

    def test_history_records_alerts(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        mgr = AlertManager()
        mgr.regime_change("A", "B", 0.7)
        mgr.circuit_breaker("DAILY_HALT", 0.03)
        history = mgr.get_history()
        assert len(history) == 2
        assert history[0].alert_type == AlertType.REGIME_CHANGE

    def test_webhook_called_when_configured(self, tmp_path, monkeypatch) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        calls = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=5: calls.append(req) or _FakeResp(),
        )
        mgr = AlertManager(webhook_url="https://hooks.example.com/x")
        mgr.circuit_breaker("DAILY_HALT", 0.03)
        assert len(calls) == 1

    def test_alert_logged_to_file(self, tmp_path) -> None:
        setup_logging(log_dir=tmp_path, console=False)
        mgr = AlertManager()
        mgr.circuit_breaker("DAILY_HALT", 0.03)
        content = (tmp_path / "alerts.log").read_text().strip()
        parsed = json.loads(content.splitlines()[-1])
        assert parsed["alert_type"] == "circuit_breaker"


class _FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b""


# ──────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────────────────────────────────────

class TestRiskBar:
    def test_green_when_low(self) -> None:
        bar = _risk_bar(0.003, 0.03)
        assert "✅" in bar.plain

    def test_yellow_when_medium(self) -> None:
        bar = _risk_bar(0.02, 0.03)  # 66% → giallo
        assert "⚠️" in bar.plain

    def test_red_when_high(self) -> None:
        bar = _risk_bar(0.029, 0.03)  # 96% → rosso
        assert "🚨" in bar.plain

    def test_handles_zero_limit(self) -> None:
        bar = _risk_bar(0.0, 0.0)  # nessuna divisione per zero
        assert bar is not None


class TestDashboardStateFromDict:
    def test_reconstructs_rich_snapshot(self) -> None:
        data = {
            "peak_equity": 100000.0,
            "circuit_breaker": "NORMAL",
            "positions": {
                "SPY": {
                    "qty": 10.0, "avg_entry_price": 514.0, "current_price": 520.30,
                    "unrealized_pnl": 63.0, "unrealized_pnl_pct": 0.012, "side": "long",
                    "stop_level": 508.0, "regime_at_entry": "BULL",
                    "current_regime": "BULL", "holding_bars": 3, "opened_at": None,
                }
            },
            "dashboard": {
                "regime": "BULL", "probability": 0.72, "is_confirmed": True,
                "consecutive_bars": 14, "state_probabilities": [0.72, 0.20, 0.08],
                "flicker_rate": 1, "flicker_window": 20,
                "equity": 105230.0, "daily_pnl": 340.0, "daily_pnl_pct": 0.0032,
                "allocation_pct": 0.95, "leverage": 1.25,
                "daily_dd": 0.003, "daily_dd_limit": 0.03,
                "peak_dd": 0.012, "peak_dd_limit": 0.10,
                "circuit_breaker": "NORMAL", "hmm_age_str": "2d ago",
                "trading_mode": "PAPER",
                "recent_signals": [{"time": "14:30", "symbol": "SPY", "action": "LONG 95%", "regime": "BULL"}],
                "considered_signals": [{
                    "symbol": "SPY", "status": "IN TARGET", "price": 520.30,
                    "price_change_pct": 0.004, "current_weight": 0.05,
                    "target_weight": 0.15, "active_stop": 508.0,
                }],
                "events": [{"time": "14:35", "kind": "STOP", "symbol": "SPY", "detail": "stop @ 508"}],
                "symbols": ["SPY", "QQQ"],
            },
        }
        state = dashboard_state_from_dict(data)
        assert state.regime_state.label == "BULL"
        assert state.regime_state.probability == 0.72
        assert state.allocation_pct == 0.95
        assert state.leverage == 1.25
        assert state.snapshot.total_value == 105230.0
        assert "SPY" in state.snapshot.positions
        assert state.snapshot.positions["SPY"].stop_level == 508.0
        assert state.considered_signals[0]["status"] == "IN TARGET"
        assert state.events[0]["kind"] == "STOP"
        assert state.symbols == ["SPY", "QQQ"]
        # Deve renderizzare senza errori
        LiveDashboard().render_once(state)

    def test_tolerates_old_snapshot_without_dashboard(self) -> None:
        """Snapshot vecchio (senza sezione 'dashboard') usa i fallback."""
        data = {"peak_equity": 100000.0, "circuit_breaker": "NORMAL", "positions": {}}
        state = dashboard_state_from_dict(data)
        assert state.regime_state.label == "UNKNOWN"
        assert state.circuit_breaker == "NORMAL"
        LiveDashboard().render_once(state)


class TestLiveDashboard:
    def test_render_once_does_not_crash(self, regime_state, snapshot) -> None:
        dash = LiveDashboard(refresh_seconds=5)
        state = DashboardState(
            snapshot=snapshot,
            regime_state=regime_state,
            allocation_pct=0.95,
            leverage=1.25,
            flicker_rate=1,
            daily_pnl=340.0,
            daily_pnl_pct=0.0032,
            daily_dd=0.003,
            peak_dd=0.012,
            circuit_breaker="NORMAL",
            api_latency_ms=23,
            hmm_age_str="2d ago",
            trading_mode="PAPER",
            recent_signals=[{"time": "14:30", "symbol": "SPY", "action": "Rebalance 60%→95%", "regime": "Low vol"}],
        )
        # Non deve sollevare eccezioni
        dash.render_once(state)

    def test_render_with_no_positions(self, regime_state) -> None:
        empty_snap = PortfolioSnapshot(total_value=100000.0, cash=100000.0, positions={})
        dash = LiveDashboard()
        state = DashboardState(snapshot=empty_snap, regime_state=regime_state)
        dash.render_once(state)  # non deve crashare

    def test_start_stop_lifecycle(self, regime_state, snapshot) -> None:
        dash = LiveDashboard(refresh_seconds=5)
        state = DashboardState(snapshot=snapshot, regime_state=regime_state)
        dash.start()
        dash.update(state)
        dash.stop()
        assert dash._live is None
