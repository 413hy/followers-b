from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_quant.copy_trading.codex_audit import CodexSystemAuditor
from ai_quant.copy_trading.codex_model import (
    CODEX_INTERVENTION_MODEL,
    CODEX_INTERVENTION_REASONING_EFFORT,
    codex_model_arguments,
)
from ai_quant.copy_trading.codex_repair import CodexSystemRepairer
from ai_quant.services.copy_codex_audit import (
    _RECENT_LEADER_POLL_FAILURES_SQL,
    _RECENT_SIGNAL_ERRORS_SQL,
    _audit_fault_evidence_changed,
    _audit_with_publication_fence,
    _pause_new_entries_justified,
    _reconciliation_status,
)

NOW = datetime(2026, 7, 19, 1, 39, tzinfo=UTC)


def test_completed_codex_audit_prevents_repeat_signal_error_alerts() -> None:
    assert "audit.findings->'reviewed_signal_ids'" in _RECENT_SIGNAL_ERRORS_SQL
    assert "audit.findings ? 'codex'" in _RECENT_SIGNAL_ERRORS_SQL
    assert "NOT (audit.findings ? 'reviewed_signal_ids')" in _RECENT_SIGNAL_ERRORS_SQL


def test_codex_audit_reads_exact_latest_failure_for_each_active_leader() -> None:
    assert "state IN ('OBSERVE_ONLY','ACTIVE','DRAINING')" in _RECENT_LEADER_POLL_FAILURES_SQL
    assert "ORDER BY occurred_at DESC,poll_event_id DESC LIMIT 1" in (
        _RECENT_LEADER_POLL_FAILURES_SQL
    )
    assert "reason_codes" in _RECENT_LEADER_POLL_FAILURES_SQL
    assert "failure_count_last_10_minutes" in _RECENT_LEADER_POLL_FAILURES_SQL


def test_audit_publication_fence_detects_recovered_leader_poll() -> None:
    failed = {
        "latest_poll_failures": 1,
        "maximum_poll_age_seconds": 3,
        "oldest_pending_notification_seconds": 0,
        "latest_watchdog_state": "HEALTHY",
        "service_states": {"aiq-copy-poller.service": "active"},
        "recent_leader_poll_failures": [
            {
                "leader_id": "5075281354358777856",
                "state": "CONTRACT_DRIFT",
                "reason_codes": ["COPY_ORDER_IDENTITY_AMBIGUOUS"],
            }
        ],
    }
    recovered = {
        **failed,
        "latest_poll_failures": 0,
        "recent_leader_poll_failures": [],
    }

    assert _audit_fault_evidence_changed(failed, recovered)
    assert not _audit_fault_evidence_changed(recovered, dict(recovered))


def test_audit_publication_fence_ignores_only_age_progression() -> None:
    before = {
        "maximum_poll_age_seconds": 3,
        "latest_watchdog_age_seconds": 10,
        "oldest_pending_notification_seconds": 4,
        "daily_selection_age_hours": 1,
        "latest_watchdog_state": "HEALTHY",
        "service_states": {"aiq-copy-poller.service": "active"},
    }
    after = {
        **before,
        "maximum_poll_age_seconds": 18,
        "latest_watchdog_age_seconds": 25,
        "oldest_pending_notification_seconds": 19,
        "daily_selection_age_hours": 1.01,
    }

    assert not _audit_fault_evidence_changed(before, after)


def test_audit_publication_fence_reviews_recovered_snapshot_before_publish() -> None:
    failed = {
        "sanitized": {
            "latest_poll_failures": 1,
            "maximum_poll_age_seconds": 3,
            "oldest_pending_notification_seconds": 0,
            "recent_leader_poll_failures": [{"leader_id": "5075281354358777856"}],
        },
        "facts": "failed",
    }
    recovered = {
        "sanitized": {
            **failed["sanitized"],
            "latest_poll_failures": 0,
            "recent_leader_poll_failures": [],
        },
        "facts": "recovered",
    }
    reviewed: list[dict[str, Any]] = []

    class FakeAuditor:
        def audit(self, facts: dict[str, Any]) -> Any:
            reviewed.append(dict(facts))
            return SimpleNamespace(document={"status": "HEALTHY"})

    result, current, sanitized = _audit_with_publication_fence(
        FakeAuditor(),  # type: ignore[arg-type]
        failed,
        lambda: recovered,
    )

    assert len(reviewed) == 2
    assert reviewed[0]["latest_poll_failures"] == 1
    assert reviewed[1]["latest_poll_failures"] == 0
    assert result.document["status"] == "HEALTHY"
    assert current["facts"] == "recovered"
    assert sanitized["recent_leader_poll_failures"] == []


def test_codex_intervention_uses_explicit_frontier_model_policy() -> None:
    assert CODEX_INTERVENTION_MODEL == "gpt-5.6-sol"
    assert CODEX_INTERVENTION_REASONING_EFFORT == "high"
    assert codex_model_arguments() == (
        "--model",
        "gpt-5.6-sol",
        "--config",
        'model_reasoning_effort="high"',
    )


def test_fresh_partial_fill_is_inside_reconciliation_grace() -> None:
    requires_reconciliation, grace_active = _reconciliation_status(
        decision_state="UNCERTAIN",
        submission_state="PARTIALLY_FILLED",
        decision_occurred_at=NOW - timedelta(seconds=20),
        submission_occurred_at=NOW - timedelta(seconds=20),
        now=NOW,
    )

    assert not requires_reconciliation
    assert grace_active


def test_overdue_partial_fill_requires_reconciliation() -> None:
    requires_reconciliation, grace_active = _reconciliation_status(
        decision_state="UNCERTAIN",
        submission_state="PARTIALLY_FILLED",
        decision_occurred_at=NOW - timedelta(minutes=3),
        submission_occurred_at=NOW - timedelta(minutes=3),
        now=NOW,
    )

    assert requires_reconciliation
    assert not grace_active


def test_codex_pause_gate_rejects_model_pause_for_fresh_partial_only() -> None:
    facts = SimpleNamespace(
        stale_poll_seconds=10,
        oldest_pending_notification_seconds=0,
        history_gap_failures=0,
        uncertain_signals=0,
        failed_signals_last_hour=0,
        overdue_pending_entries=0,
        latest_watchdog_state="HEALTHY",
    )

    justified = _pause_new_entries_justified(
        facts=facts,
        service_states={"aiq-copy-poller.service": "active"},
        recent_signal_errors=[
            {
                "requires_reconciliation": False,
                "reconciliation_grace_active": True,
            }
        ],
    )

    assert not justified


def test_hourly_audit_passes_model_policy_despite_ignoring_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> None:
        captured.extend(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(
                {
                    "status": "HEALTHY",
                    "summary": "healthy",
                    "findings": [],
                    "recommended_actions": ["NO_ACTION"],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    auditor = CodexSystemAuditor(
        schema_path=Path("contracts/copy-system-audit.schema.json"),
        work_root=tmp_path,
    )

    auditor.audit({"state": "HEALTHY"})

    assert "--ignore-user-config" in captured
    assert captured[captured.index("--model") + 1] == "gpt-5.6-sol"
    assert captured[captured.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert "recent_leader_poll_failures" in captured[-1]
    assert "COPY_ORDER_IDENTITY_AMBIGUOUS" in captured[-1]
    assert "Simplified Chinese" in captured[-1]


def test_automatic_repair_uses_high_model_and_workspace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> None:
        captured.extend(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(
                {
                    "status": "NO_CHANGE",
                    "summary": "operational incident already resolved",
                    "root_cause": "no persistent code defect",
                    "changed_files": [],
                    "tests_run": ["inspection only"],
                    "follow_up_required": False,
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    repairer = CodexSystemRepairer(
        schema_path=Path("contracts/copy-system-repair.schema.json"),
        repository_root=Path.cwd(),
        work_root=tmp_path,
    )

    repairer.repair({"incident": "sanitized"})

    assert "--ignore-user-config" in captured
    assert captured[captured.index("--model") + 1] == "gpt-5.6-sol"
    assert captured[captured.index("--sandbox") + 1] == "workspace-write"
    assert "Simplified Chinese" in captured[-1]
