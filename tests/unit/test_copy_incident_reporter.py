from datetime import UTC, datetime, timedelta

from ai_quant.services.copy_codex_audit import _recent_incidents
from ai_quant.services.copy_incident_reporter import (
    _failure_explanation,
    _incident_id,
    _load_report,
    _redact,
    _render_telegram,
    _write_report,
)


def test_incident_id_deduplicates_restarts_within_fifteen_minutes() -> None:
    now = datetime(2026, 7, 17, 6, 2, tzinfo=UTC)
    unit = "aiq-copy-poller.service"

    assert _incident_id(unit, now) == _incident_id(unit, now + timedelta(minutes=12))
    assert _incident_id(unit, now) != _incident_id(unit, now + timedelta(minutes=15))


def test_incident_report_redacts_credentials_and_round_trips(tmp_path) -> None:
    raw_log = "postgresql://user:password@localhost/db api_key=abc token=xyz"
    redacted = _redact(raw_log)
    path = tmp_path / "incident.json"

    _write_report(path, {"journal_tail": redacted})

    assert "user:password" not in redacted
    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert _load_report(path) == {"journal_tail": redacted}


def test_incident_telegram_report_contains_recovery_context() -> None:
    text = _render_telegram(
        {
            "source_unit": "aiq-copy-poller.service",
            "unit_facts": {
                "ActiveState": "failed",
                "SubState": "failed",
                "Result": "exit-code",
                "NRestarts": "3",
            },
            "journal_tail": "copy_poll_dependency_error: database unavailable",
            "codex_wakeup_requested": True,
            "report_path": "/var/lib/ai-quant/evidence/copy-trading/incidents/test.json",
        }
    )

    assert "aiq-copy-poller.service" in text
    assert "database unavailable" in text
    assert "Codex 即时审查" in text
    assert "不会盲目重下" in text


def test_start_limit_failure_is_explained_as_a_start_request_limit() -> None:
    explanation = _failure_explanation({"Result": "start-limit-hit"})

    assert "启动请求" in explanation
    assert "不是 Binance、交易订单或数据库故障" in explanation


def test_codex_audit_receives_recent_sanitized_incident_summary(tmp_path) -> None:
    now = datetime(2026, 7, 17, 6, 30, tzinfo=UTC)
    _write_report(
        tmp_path / "recent.json",
        {
            "incident_id": "a" * 64,
            "last_occurred_at": (now - timedelta(minutes=5)).isoformat(),
            "source_unit": "aiq-copy-telegram.service",
            "occurrence_count": 2,
            "unit_facts": {"ActiveState": "failed", "Result": "exit-code"},
            "notification_status": "PENDING",
            "journal_tail": "first line\nlast bounded failure line",
        },
    )
    _write_report(
        tmp_path / "stale.json",
        {
            "last_occurred_at": (now - timedelta(hours=3)).isoformat(),
            "source_unit": "aiq-copy-poller.service",
        },
    )

    incidents = _recent_incidents(
        tmp_path,
        now=now,
        unit_facts_loader=lambda _unit: {"ActiveState": "active", "Result": "success"},
    )

    assert incidents == [
        {
            "incident_id": "a" * 64,
            "source_unit": "aiq-copy-telegram.service",
            "last_occurred_at": (now - timedelta(minutes=5)).isoformat(),
            "occurrence_count": 2,
            "historical_active_state": "failed",
            "historical_result": "exit-code",
            "current_active_state": "active",
            "current_result": "success",
            "resolved": True,
            "notification_status": "PENDING",
            "error_evidence": [],
            "last_log_line": "last bounded failure line",
        }
    ]


def test_codex_audit_marks_still_failed_incident_unresolved(tmp_path) -> None:
    now = datetime(2026, 7, 17, 6, 30, tzinfo=UTC)
    _write_report(
        tmp_path / "recent.json",
        {
            "incident_id": "b" * 64,
            "last_occurred_at": (now - timedelta(minutes=5)).isoformat(),
            "source_unit": "aiq-copy-poller.service",
            "unit_facts": {"ActiveState": "failed", "Result": "exit-code"},
            "journal_tail": "Traceback: bounded failure\nservice failed with exit-code",
        },
    )

    incidents = _recent_incidents(
        tmp_path,
        now=now,
        unit_facts_loader=lambda _unit: {"ActiveState": "failed", "Result": "exit-code"},
    )

    assert incidents[0]["resolved"] is False
    assert incidents[0]["current_active_state"] == "failed"
    assert incidents[0]["error_evidence"] == [
        "Traceback: bounded failure",
        "service failed with exit-code",
    ]


def test_codex_audit_treats_successful_oneshot_retry_as_recovering(tmp_path) -> None:
    now = datetime(2026, 7, 17, 6, 30, tzinfo=UTC)
    _write_report(
        tmp_path / "recent.json",
        {
            "incident_id": "a" * 64,
            "last_occurred_at": (now - timedelta(minutes=5)).isoformat(),
            "source_unit": "aiq-copy-codex-audit.service",
            "unit_facts": {"ActiveState": "failed", "Result": "exit-code"},
            "journal_tail": "previous bounded failure",
        },
    )

    incidents = _recent_incidents(
        tmp_path,
        now=now,
        unit_facts_loader=lambda _unit: {"ActiveState": "activating", "Result": "success"},
    )

    assert incidents[0]["resolved"] is True
    assert incidents[0]["incident_id"] == "a" * 64
